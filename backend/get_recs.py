import json
import boto3
import time
from datetime import datetime, timezone

REGION = "ap-south-1"
DYNAMODB_TABLE = "nimbus-user-sessions"
KB_MODEL_ARN = "arn:aws:bedrock:ap-south-1::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0"
BEDROCK_MODEL_ID = "anthropic.claude-3-5-haiku-20241022-v1:0"
KNOWLEDGE_BASE_ID = "<to be filled after KB setup>"

DIFFICULTY_LABELS = {
    "beginner": ["good first issue"],
    "intermediate": ["help wanted", "bug"],
    "advanced": ["enhancement"],
}

dynamodb = boto3.resource("dynamodb", region_name=REGION)
bedrock_agent = boto3.client("bedrock-agent-runtime", region_name=REGION)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)


def lambda_handler(event, context):
    body = json.loads(event.get("body", "{}"))
    session_id = body.get("session_id")

    if not session_id:
        return _response(400, {"error": "session_id is required"})

    # Step 1: Fetch skill profile from DynamoDB
    table = dynamodb.Table(DYNAMODB_TABLE)
    result = table.get_item(Key={"session_id": session_id})
    item = result.get("Item")

    if not item:
        return _response(404, {"error": "Session not found, please upload your resume again"})

    skills = item.get("skills", {})
    languages = skills.get("languages", [])
    frameworks = skills.get("frameworks", [])
    experience_level = skills.get("experience_level", "beginner")
    domains = skills.get("domains", [])

    # Step 2: Build RAG query
    rag_query = (
        f"Find GitHub issues suitable for a {experience_level} developer "
        f"with skills in {', '.join(languages)} and {', '.join(frameworks)} "
        f"working in {', '.join(domains)}"
    )

    # Step 3: Query Bedrock Knowledge Base
    kb_results = _query_knowledge_base(rag_query, experience_level)

    if not kb_results:
        return _response(200, {
            "recommendations": [],
            "message": "No matching issues found for your skill profile"
        })

    # Step 4: Generate guidance for each issue
    recommendations = []
    for issue in kb_results[:5]:
        guidance = _generate_guidance(issue, experience_level, languages, frameworks)
        if guidance is None:
            continue  # skip issues where guidance generation failed
        recommendations.append({**issue, "guidance": guidance})

    # Step 5: Update DynamoDB with generated recommendations
    _update_session(table, session_id, recommendations, experience_level)

    return _response(200, {"recommendations": recommendations})


def _query_knowledge_base(query: str, experience_level: str) -> list:
    """Query Bedrock Knowledge Base and filter by difficulty labels."""
    labels = DIFFICULTY_LABELS.get(experience_level, [])

    try:
        resp = bedrock_agent.retrieve(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": 20}
            },
        )
    except Exception as e:
        print(f"Knowledge Base query failed: {e}")
        return []

    issues = []
    for hit in resp.get("retrievalResults", []):
        content = hit.get("content", {}).get("text", "")
        metadata = hit.get("location", {})
        parsed = _parse_issue_text(content, metadata)
        if parsed and any(lbl.lower() in [l.lower() for l in parsed.get("labels", [])] for lbl in labels):
            issues.append(parsed)
        if len(issues) == 5:
            break

    return issues


def _parse_issue_text(text: str, metadata: dict) -> dict | None:
    """Parse issue text blob into structured fields."""
    try:
        lines = text.strip().splitlines()
        data = {}
        for line in lines:
            if line.startswith("repo_name:"):
                data["repo_name"] = line.split(":", 1)[1].strip()
            elif line.startswith("repo_url:"):
                data["repo_url"] = line.split(":", 1)[1].strip()
            elif line.startswith("issue_title:"):
                data["issue_title"] = line.split(":", 1)[1].strip()
            elif line.startswith("issue_url:"):
                data["issue_url"] = line.split(":", 1)[1].strip()
            elif line.startswith("issue_number:"):
                data["issue_number"] = int(line.split(":", 1)[1].strip())
            elif line.startswith("labels:"):
                data["labels"] = [l.strip() for l in line.split(":", 1)[1].split(",")]
            elif line.startswith("issue_description:"):
                data["issue_description"] = line.split(":", 1)[1].strip()
        return data if "issue_title" in data else None
    except Exception:
        return None


def _generate_guidance(issue: dict, experience_level: str, languages: list, frameworks: list) -> dict | None:
    """Call Bedrock to generate contribution guidance for a single issue."""
    prompt = f"""The user is a {experience_level} developer with skills in {', '.join(languages)} and {', '.join(frameworks)}.
They want to solve this GitHub issue:

Repo: {issue.get('repo_name')}
Issue Title: {issue.get('issue_title')}
Issue Description: {issue.get('issue_description', 'N/A')}

Provide:
1. A plain English explanation of what this issue is asking (2-3 lines)
2. Step-by-step approach to solve it
3. Key concepts they need to understand before starting
4. Potential gotchas to watch out for
5. Estimated time to complete based on their experience level

Keep it practical and beginner-friendly. Avoid jargon where possible.
Respond in JSON with keys: summary, steps (list), concepts_to_understand (list), gotchas (list), estimated_time (string)."""

    for attempt in range(3):
        try:
            resp = bedrock.invoke_model(
                modelId=BEDROCK_MODEL_ID,
                contentType="application/json",
                accept="application/json",
                body=json.dumps({
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                }),
            )
            raw = json.loads(resp["body"].read())
            text = raw["content"][0]["text"]
            return json.loads(text)
        except bedrock.exceptions.ThrottlingException:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                print(f"Bedrock rate limit hit for issue {issue.get('issue_title')}, giving up.")
                return None
        except Exception as e:
            print(f"Guidance generation failed for issue {issue.get('issue_title')}: {e}")
            return None


def _update_session(table, session_id: str, recommendations: list, experience_level: str):
    """Persist generated recommendations back to DynamoDB."""
    issues_without_guidance = [
        {k: v for k, v in rec.items() if k != "guidance"} for rec in recommendations
    ]
    table.update_item(
        Key={"session_id": session_id},
        UpdateExpression="SET recommendations = :r",
        ExpressionAttributeValues={
            ":r": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "issues": issues_without_guidance,
                "experience_level_used": experience_level,
            }
        },
    )


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
