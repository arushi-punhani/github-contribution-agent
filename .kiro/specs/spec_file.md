# GitHub Contribution Agent

A web app where users upload their resume, and the app will exract their skills, and recomends open GitHub issues that they can contribute to based on their current level and guides them through it

## user flow
1. user uploads their resume as a PDF 
2. the app extracts their skills, which include languagues, experience, projects done, etc.
3. app searches a database of GitHub repos and issues, and finds issues of the correct difficulty level
4. app returns matching issues along with guidance on how to proceed to solve them

## difficulty levels
the issues will be matched to the users based on the skills and experience that they have, which will be extracted from the resume they provide
- beginner : "good first issue" labelled issues, these are to make new contributers familiar with the contribution process
- intermediate : these issues would require some familiarity with the project's codebase, core concepts, or specific tools or frameworks, they would need more technical understanding 
- advanced : these tasks will involve complex problem solving, deep understanding of the system architechture and a lot more coding, this can involve debugging complex bugs, or designing major new features or optimizing functionality

## Step 1 : Setup
We need to scrape 50 active GitHub repos and upload them as text files to S3, later we will use this for creating the Bedrock Knowledge Base
the script will
- call the GitHub API to fetch the repo metadata
- fetch issues with varying difficulty levels 
    beginner : "good first issue"
    intermidiate : "help wanted", "bug"
    advanced : "enhancement"
- save each repo as a text file that contains all its issues
- uploads the text file to S3 bucket : repo-knowledge-base

## Step 2: Resume Upload & Skill Extraction

### Lambda Function: upload_resume.py
Receives a base64 encoded PDF resume from the frontend,
extracts skills using Amazon Bedrock Claude 3.5 Haiku,
stores the results in DynamoDB, and returns them to the frontend.

### Input
- pdf_base64: base64 encoded PDF string

### Output
- session_id: unique UUID for this user session
- skills:
  - languages: list of programming languages
  - frameworks: list of frameworks
  - experience_level: beginner, intermediate, or advanced
  - domains: list of domains (web, ml, devops etc.)

### AWS Services Used
- Amazon S3: store the uploaded PDF
  bucket: repos-knowledge-base
- Amazon Bedrock: extract skills from resume
  model: anthropic.claude-3-5-haiku-20241022-v1:0
- Amazon DynamoDB: store skill profile with session_id
  table: nimbus-user-sessions

### Region
ap-south-1

## Step 3: Get Recommendations

### Lambda Function: get_recommendations.py
Receives a session_id from the frontend, fetches the user's extracted skill profile
from DynamoDB, queries the Bedrock Knowledge Base using RAG to find matching GitHub
issues, generates contribution guidance for each issue, and returns the top 5
recommendations to the frontend.

### Input
- `session_id`: UUID string from Step 2

### Processing Steps
1. Fetch skill profile from DynamoDB using `session_id`
   - Retrieve `languages`, `frameworks`, `experience_level`, `domains`
2. Build a RAG query from the skill profile
   - Query shape: "Find GitHub issues suitable for a {experience_level} developer
     with skills in {languages} and {frameworks} working in {domains}"
3. Query Bedrock Knowledge Base with the RAG query
   - Knowledge Base searches the pre-indexed S3 repo text files from Step 1
   - Retrieve top 5 most relevant issues
   - Filter results to match the user's `experience_level`:
     - Beginner → issues labeled "good first issue"
     - Intermediate → issues labeled "help wanted", "bug"
     - Advanced → issues labeled "enhancement"
4. For each of the 5 matching issues, call Bedrock again to generate guidance
5. Return all recommendations + guidance to frontend

### Output
List of 5 recommendations, each containing:
- `repo_name`: name of the GitHub repository
- `repo_url`: URL to the repository
- `issue_title`: title of the matched issue
- `issue_url`: direct URL to the issue
- `issue_number`: GitHub issue number
- `difficulty`: beginner / intermediate / advanced
- `matched_skills`: which of the user's skills are relevant to this issue
- `guidance`:
  - `summary`: plain English explanation of what the issue is asking (2-3 lines)
  - `steps`: ordered list of steps to approach solving the issue
  - `concepts_to_understand`: key concepts the user needs to know before starting
  - `gotchas`: things to watch out for while solving
  - `estimated_time`: rough time estimate based on experience level

### Bedrock Guidance Prompt Shape
```
The user is a {experience_level} developer with skills in {languages} and {frameworks}.
They want to solve this GitHub issue:

Repo: {repo_name}
Issue Title: {issue_title}
Issue Description: {issue_description}

Provide:
1. A plain English explanation of what this issue is asking (2-3 lines)
2. Step-by-step approach to solve it
3. Key concepts they need to understand before starting
4. Potential gotchas to watch out for
5. Estimated time to complete based on their experience level

Keep it practical and beginner-friendly. Avoid jargon where possible.
```

### AWS Services Used
- **Amazon DynamoDB**: fetch skill profile
  - table: `nimbus-user-sessions`
  - key: `session_id`
- **Amazon Bedrock Knowledge Base**: RAG query over pre-indexed GitHub repo data
  - Knowledge Base ID: `<to be filled after KB setup>`
  - model: `anthropic.claude-3-5-haiku-20241022-v1:0`
- **Amazon Bedrock**: generate per-issue guidance
  - model: `anthropic.claude-3-5-haiku-20241022-v1:0`

### Region
`ap-south-1`

### Error Handling
- `session_id` not found in DynamoDB → return 404 with message "Session not found, please upload your resume again"
- Knowledge Base returns no results → return 200 with empty list and message "No matching issues found for your skill profile"
- Bedrock guidance generation fails for one issue → skip that issue, still return remaining results
- Bedrock rate limit hit → retry with exponential backoff up to 3 times before failing

### DynamoDB Update
After recommendations are generated, update the session record with:
```
recommendations: {
  generated_at: timestamp,
  issues: [ list of 5 issue objects without guidance ],
  experience_level_used: "beginner" / "intermediate" / "advanced"
}
```
This allows the frontend to reload previous recommendations without re-querying Bedrock.

