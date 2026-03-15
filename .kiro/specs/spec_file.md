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