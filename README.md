# InterviewAI

**A local-first AI mock interview coach.** Turn a target role, resume, and job description into a structured practice interview, then evaluate written or transcribed spoken answers with actionable feedback. Interview history and score progress are saved locally.

## Features

- Generate 3–10 role-, experience-, and interview-type-specific questions.
- Parse resume PDFs and job descriptions; compare skills and responsibilities.
- Evaluate relevance, completeness, clarity, and technical accuracy; get strengths, improvement tips, and a suggested answer.
- Practice by typing or recording answers; transcribe locally with Faster-Whisper and read questions aloud with browser speech synthesis.
- Save scorecards and interview history in SQLite; track performance on the dashboard.
- Responsive UI with light/dark themes, upload validation, and readable API errors.
- Use a local Ollama model and project MCP tools without paid AI API keys.

## Screenshots

| Dashboard | Interview scorecard |
| --- | --- |
| ![Dashboard](docs/screenshots/dashboard.png) | ![Interview scorecard](docs/screenshots/results.png) |

## Stack

React, Vite, FastAPI, Pydantic, SQLite, Ollama, PyMuPDF, spaCy, Python MCP SDK, Faster-Whisper, browser MediaRecorder and Speech Synthesis.

## Run locally on Windows

### Prerequisites

- Windows 10/11, Python 3.10+, Node.js 20.19+ or 22.12+
- [Ollama](https://ollama.com/download); download a model such as llama3.2:3b

Download the model in PowerShell:

    ollama pull llama3.2:3b

### Backend

From the repository root:

    py -m venv backend\.venv
    backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
    backend\.venv\Scripts\python.exe -m uvicorn --app-dir backend main:app --reload

The API is at http://127.0.0.1:8000 (docs: /docs, health: /health). The local SQLite database is initialized automatically.

### Frontend

In a second PowerShell window:

    cd frontend
    npm install
    npm run dev

Open the Vite URL (usually http://localhost:5173). The development server proxies API requests to FastAPI.

## Tests

    cd frontend
    npm run lint
    npm run build
    cd ..
    backend\.venv\Scripts\python.exe -m unittest discover -s backend -p "test_*.py" -v
    backend\.venv\Scripts\python.exe -m pip check

GitHub Actions runs frontend lint/build and backend tests/dependency checks on pushes and pull requests.

## Privacy and deployment

Ollama, MCP, document parsing, and speech transcription run on your machine. Contact details are excluded from question-generation context; uploaded resume/job-description text is not persisted. Audio is temporary and deleted after transcription. Confirmed transcripts and feedback are saved in the local backend\interviewai.db database.

This repository contains the complete application source. The interactive application is designed for local use: its backend requires local Ollama, starts MCP as a local subprocess, and stores data on the local filesystem. **Do not expose the development API publicly as-is.** Public hosting requires a model-capable backend, durable database, production CORS/access controls, and an explicit privacy policy.

A static, non-interactive project portfolio is published at [YashChauhan178.github.io/InterviewAI](https://yashchauhan178.github.io/InterviewAI/); GitHub Pages deploys only the docs/ showcase.
# InterviewAI

A local-first AI mock interview coach for role-specific practice, voice answers, and actionable feedback.

![InterviewAI dashboard](docs/screenshots/dashboard.png)

## Portfolio preview

The [static portfolio site](https://YashChauhan178.github.io/InterviewAI/) showcases the project. This public repository intentionally contains only the portfolio assets; the interactive application, local AI services, and interview data remain on the developer's machine.

## Highlights

- Tailored interview sessions based on role, experience level, interview type, resume, and job description.
- Typed or spoken responses, browser speech playback, and local transcription support.
- Rubric-based feedback, suggested answers, saved session history, scorecards, and progress tracking.
- React/Vite frontend, FastAPI backend, SQLite persistence, local Ollama integration, and project MCP tools.

## Screenshots

### Dashboard
![Dashboard with score progress and recent sessions](docs/screenshots/dashboard.png)

### Interview results
![Interview results with answer feedback](docs/screenshots/results.png)

## Privacy and deployment

Interview content and local model requests are intended to stay on the user's computer. The public GitHub Pages site is a static showcase only; it does not expose the interactive backend or personal interview data.
