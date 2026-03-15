"""
upload_resume.py  -  Lambda: Resume Upload & Skill Extraction

Flow:
  1. Receive base64-encoded PDF from the frontend
  2. Decode and upload the PDF to S3
  3. Send the PDF directly to Bedrock (Claude 3.5 Haiku) as a native document block
  4. Persist the skill profile in DynamoDB (table: nimbus-user-sessions)
  5. Return session_id + structured skill profile to the caller

Environment variables (set in Lambda config):
  AWS_REGION_OVERRIDE  - optional, defaults to ap-south-1
"""

import base64
import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

# -- Constants -----------------------------------------------------------------

REGION        = os.environ.get("AWS_REGION_OVERRIDE", "ap-south-1")
S3_BUCKET     = "repos-knowledge-base"
S3_PREFIX     = "resumes/"
DYNAMO_TABLE  = "nimbus-user-sessions"
BEDROCK_MODEL = "anthropic.claude-3-5-haiku-20241022-v1:0"

# -- AWS clients (initialised outside handler for Lambda container reuse) ------

s3       = boto3.client("s3",              region_name=REGION)
bedrock  = boto3.client("bedrock-runtime", region_name=REGION)
dynamodb = boto3.resource("dynamodb",      region_name=REGION)
table    = dynamodb.Table(DYNAMO_TABLE)

# -- Prompts -------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert technical recruiter and developer mentor.
Your job is to analyse a resume and return a structured JSON skill profile.
Return ONLY valid JSON - no markdown fences, no extra text."""

USER_PROMPT = """Analyse the attached resume and return a JSON object with exactly this shape:

{
  "languages":        ["<lang1>", ...],
  "frameworks":       ["<fw1>", ...],
  "domains":          ["<domain1>", ...],
  "experience_level": "<beginner|intermediate|advanced>",
  "level_reasoning":  "<one sentence explaining why you chose this level>"
}


### Experience level rules
Use ALL available context clues - do not rely on a single signal.

**beginner**
- Student in year 1-2 of a CS/engineering degree with only coursework or small personal projects
- Self-taught developer with < 1 year of hands-on coding
- No professional work experience; projects are tutorials or simple CRUD apps
- Knows 1-2 languages at a surface level

**intermediate**
- Student in year 3-4 / final year with meaningful personal or internship projects
- 1-3 years of professional experience (junior / associate titles)
- Has shipped real features; familiar with version control, testing, CI/CD basics
- Comfortable with at least one framework and one cloud service

**advanced**
- 4+ years of professional experience, or senior / lead / staff / principal titles
- Demonstrates system design, architecture decisions, performance optimisation, or mentoring
- Has led projects, designed APIs, or contributed to open-source at a significant level
- Deep expertise in multiple languages / frameworks / domains

### Student-specific signals (when no work experience is present)
- Year 1-2 + only coursework                             -> beginner
- Year 3-4 + internship OR substantial personal projects -> intermediate
- Final year / grad student + research / complex projects -> intermediate or advanced
- Technologies listed without any project context        -> lean beginner"""


# -- Helpers -------------------------------------------------------------------

def call_bedrock(pdf_b64: str) -> dict:
    """Send the PDF directly to Claude 3.5 Haiku as a native document block."""
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type":       "base64",
                            "media_type": "application/pdf",
                            "data":       pdf_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": USER_PROMPT,
                    },
                ],
            }
        ],
    })

    response = bedrock.invoke_model(
        modelId     = BEDROCK_MODEL,
        body        = body,
        contentType = "application/json",
        accept      = "application/json",
    )

    raw  = json.loads(response["body"].read())
    text = raw["content"][0]["text"].strip()

    # Strip accidental markdown fences if the model adds them
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]

    return json.loads(text)


def upload_pdf_to_s3(pdf_bytes: bytes, session_id: str) -> str:
    """Upload the raw PDF to S3 and return the S3 key."""
    key = f"{S3_PREFIX}{session_id}.pdf"
    s3.put_object(
        Bucket      = S3_BUCKET,
        Key         = key,
        Body        = pdf_bytes,
        ContentType = "application/pdf",
    )
    return key


def save_to_dynamodb(session_id: str, skills: dict, s3_key: str) -> None:
    """Persist the skill profile in DynamoDB."""
    table.put_item(Item={
        "session_id": session_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "s3_key":     s3_key,
        "skills":     skills,
    })


def build_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type":                "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


#Lambda Handler 

def handler(event, context):
    """
    Expected input (API Gateway proxy or direct invocation):
      { "pdf_base64": "<base64-encoded PDF string>" }
    """
    try:
        # 1. Parse input
        body = event
        if isinstance(event.get("body"), str):
            body = json.loads(event["body"])

        pdf_b64 = body.get("pdf_base64", "").strip()
        if not pdf_b64:
            return build_response(400, {"error": "pdf_base64 is required"})

        # 2. Validate base64 and decode for S3 upload
        try:
            pdf_bytes = base64.b64decode(pdf_b64)
        except Exception:
            return build_response(400, {"error": "pdf_base64 is not valid base64"})

        # 3. Generate session ID
        session_id = str(uuid.uuid4())

        # 4. Upload PDF to S3
        s3_key = upload_pdf_to_s3(pdf_bytes, session_id)
        print(f"PDF uploaded -> s3://{S3_BUCKET}/{s3_key}")

        # 5. Extract skills via Bedrock (PDF sent natively, no text extraction step)
        raw_skills = call_bedrock(pdf_b64)

        skills = {
            "languages":        raw_skills.get("languages", []),
            "frameworks":       raw_skills.get("frameworks", []),
            "experience_level": raw_skills.get("experience_level", "beginner"),
            "domains":          raw_skills.get("domains", []),
        }

        # 6. Persist to DynamoDB
        save_to_dynamodb(session_id, skills, s3_key)
        print(f"Session saved -> {session_id} | level={skills['experience_level']}")

        # 7. Return result
        return build_response(200, {
            "session_id": session_id,
            "skills":     skills,
        })

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        print(f"AWS error [{code}]: {exc}")
        return build_response(502, {"error": f"AWS service error: {code}"})

    except json.JSONDecodeError as exc:
        print(f"Bedrock returned non-JSON: {exc}")
        return build_response(502, {"error": "Failed to parse skill extraction response"})

    except Exception as exc:
        print(f"Unexpected error: {exc}")
        return build_response(500, {"error": "Internal server error"})
