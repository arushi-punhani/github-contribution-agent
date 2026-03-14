"""
index_repos.py

Scrapes 50 active GitHub repos, fetches their issues across all
difficulty labels, saves each repo as a text file, and uploads to S3.

Required env vars:
  GITHUB_TOKEN  - GitHub personal access token (for higher rate limits)
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY (or use an IAM role / AWS profile)

Usage:
  pip install requests boto3
  python setup/index_repos.py
"""

import os
import time
import boto3
import requests

# ── Config ────────────────────────────────────────────────────────────────────

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
S3_BUCKET    = "repos-knowledge-base"
S3_REGION    = "ap-south-1"
REPO_COUNT   = 50
PER_PAGE     = 30   # GitHub max per page

# Difficulty label groups
LABEL_GROUPS = {
    "beginner":     ["good first issue"],
    "intermediate": ["help wanted", "bug"],
    "advanced":     ["enhancement"],
}

HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"

# ── Helpers ───────────────────────────────────────────────────────────────────

def gh_get(url: str, params: dict = None) -> dict | list:
    """GET a GitHub API endpoint, respecting rate limits."""
    while True:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait  = max(reset - int(time.time()), 1)
            print(f"  Rate limited – sleeping {wait}s …")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()


def fetch_top_repos(n: int) -> list[dict]:
    """Return the top-n most-starred repos (excluding forks)."""
    repos, page = [], 1
    while len(repos) < n:
        data = gh_get(
            "https://api.github.com/search/repositories",
            params={
                "q":        "stars:>10000 fork:false",
                "sort":     "stars",
                "order":    "desc",
                "per_page": PER_PAGE,
                "page":     page,
            },
        )
        items = data.get("items", [])
        if not items:
            break
        repos.extend(items)
        page += 1
    return repos[:n]


def fetch_issues_for_label(full_name: str, label: str) -> list[dict]:
    """Fetch all open issues for a repo with a specific label (paginated)."""
    issues, page = [], 1
    while True:
        data = gh_get(
            f"https://api.github.com/repos/{full_name}/issues",
            params={
                "labels":   label,
                "state":    "open",
                "per_page": PER_PAGE,
                "page":     page,
            },
        )
        if not data:
            break
        # Filter out pull requests (GitHub returns PRs in /issues)
        issues.extend([i for i in data if "pull_request" not in i])
        if len(data) < PER_PAGE:
            break
        page += 1
    return issues


def build_repo_text(repo: dict, issues_by_group: dict[str, list[dict]]) -> str:
    """Serialise repo metadata + issues into a plain-text document."""
    lines = [
        f"Repository: {repo['full_name']}",
        f"Description: {repo.get('description') or 'N/A'}",
        f"URL: {repo['html_url']}",
        f"Stars: {repo['stargazers_count']}",
        f"Language: {repo.get('language') or 'N/A'}",
        f"Topics: {', '.join(repo.get('topics', [])) or 'N/A'}",
        "",
    ]

    for difficulty, issues in issues_by_group.items():
        lines.append(f"=== {difficulty.upper()} ISSUES ({len(issues)}) ===")
        if not issues:
            lines.append("  (none found)")
        for issue in issues:
            lines.append(f"\n  #{issue['number']} – {issue['title']}")
            lines.append(f"  URL   : {issue['html_url']}")
            labels = ", ".join(lbl["name"] for lbl in issue.get("labels", []))
            lines.append(f"  Labels: {labels}")
            body = (issue.get("body") or "").strip()
            if body:
                # Truncate very long bodies
                preview = body[:500] + ("…" if len(body) > 500 else "")
                lines.append(f"  Body  : {preview}")
        lines.append("")

    return "\n".join(lines)


def upload_to_s3(s3_client, content: str, key: str) -> None:
    """Upload a UTF-8 string to S3."""
    s3_client.put_object(
        Bucket      = S3_BUCKET,
        Key         = key,
        Body        = content.encode("utf-8"),
        ContentType = "text/plain; charset=utf-8",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"Fetching top {REPO_COUNT} repos from GitHub …")
    repos = fetch_top_repos(REPO_COUNT)
    print(f"  Got {len(repos)} repos.\n")

    s3 = boto3.client("s3", region_name=S3_REGION)

    for idx, repo in enumerate(repos, start=1):
        full_name = repo["full_name"]
        print(f"[{idx}/{len(repos)}] {full_name}")

        issues_by_group: dict[str, list[dict]] = {}

        for difficulty, labels in LABEL_GROUPS.items():
            combined: list[dict] = []
            for label in labels:
                fetched = fetch_issues_for_label(full_name, label)
                print(f"    {difficulty} / '{label}': {len(fetched)} issues")
                combined.extend(fetched)
            # Deduplicate by issue number
            seen, unique = set(), []
            for issue in combined:
                if issue["number"] not in seen:
                    seen.add(issue["number"])
                    unique.append(issue)
            issues_by_group[difficulty] = unique

        text    = build_repo_text(repo, issues_by_group)
        s3_key  = f"repos/{full_name.replace('/', '_')}.txt"

        upload_to_s3(s3, text, s3_key)
        print(f"    Uploaded → s3://{S3_BUCKET}/{s3_key}\n")

    print("Done. All repos uploaded to S3.")


if __name__ == "__main__":
    main()
