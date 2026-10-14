# InterviewAI

**A local-first, AI-powered mock interview coach.**

InterviewAI creates role-specific practice interviews from a target role, experience level, resume, and job description. Answer by typing or recording your voice, then review structured feedback and track your progress in a private, local interview history.

The application is designed to run on your own Windows computer. Its AI features use a local [Ollama](https://ollama.com/) model; no paid AI API key is required.

![InterviewAI dashboard](docs/screenshots/dashboard.png)

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation and startup on Windows](#installation-and-startup-on-windows)
- [First-run verification](#first-run-verification)
- [Using the application](#using-the-application)
- [Configuration](#configuration)
- [API overview](#api-overview)
- [Privacy and local data](#privacy-and-local-data)
- [Troubleshooting](#troubleshooting)
- [Development and tests](#development-and-tests)
- [Deployment notes](#deployment-notes)
- [Project structure](#project-structure)

## Features

- **AI-generated questions tailored to your role:** Select an experience level, interview type, and 3–10 questions. Ollama receives the role and relevant resume/job-description context to generate questions, competencies, evaluation criteria, and suggested answers. The API records whether the questions came from Ollama or a validated fallback.
- **Resume and job-description parsing:** Upload PDF, TXT, or Markdown files for local text extraction. Analyze job requirements and compare them with resume skills.
- **Structured answer feedback:** Evaluate answers for relevance, completeness, clarity, and technical accuracy. Review the score, strengths, improvement suggestions, and a suggested answer.
- **Voice practice:** Record answers in the browser and transcribe them locally using Faster-Whisper. Browser speech synthesis can read questions aloud. Typed answers are always available.
- **Interview history:** Save interviews and answers in SQLite, see recent sessions and score trends, reopen previous sessions, and delete interviews from the history view.
- **Local AI help chat:** Ask the configured Ollama model for help with interview preparation or the application. Recent chat context is limited and is not saved to the interview database.
- **Document analysis through MCP:** The local InterviewAI MCP server exposes tools for extracting job requirements and comparing a candidate resume with a job description.
- **Responsive interface:** Includes dashboard metrics, light/dark themes, progress tracking, accessible focus states, and clear loading and error messages.

## How it works

```text
React + Vite in your browser
          │ /api requests (Vite proxy)
          ▼
FastAPI backend ─────── SQLite interview history
     │
     ├── Ollama on localhost ── question generation, evaluation, help chat
     ├── Local MCP subprocess ── job/resume analysis tools
     ├── PyMuPDF ── PDF text extraction
     └── Faster-Whisper ── local audio transcription
```

The frontend development server proxies `/api` requests to `http://127.0.0.1:8000`. The backend starts the local MCP server as a subprocess when needed. Ollama and Faster-Whisper models run on the same computer.

## Requirements

- Windows 10 or 11
- Python 3.10 or 3.12 (these versions are exercised by CI)
- Node.js 22.12 or newer (or Node.js 20.19 or newer)
- [Ollama for Windows](https://ollama.com/download)
- Enough disk space for the Ollama model (approximately 2–3 GB for `llama3.2:3b`) and the optional Faster-Whisper model
- A microphone for voice recording; typed answers do not require one

The first Faster-Whisper transcription may download its model and can take longer than later recordings. Browser microphone access is available on `localhost` in supported browsers; allow microphone permission when prompted.

## Installation and startup on Windows

### 1. Clone or download the repository

If you have Git installed:

```powershell
git clone https://github.com/YashChauhan178/InterviewAI.git
cd InterviewAI
```

If you already have the project, open PowerShell in its repository folder. The commands below assume the repository root contains `backend`, `frontend`, and `README.md`.

### 2. Install and start Ollama

Install Ollama, then pull the default model in PowerShell:

```powershell
ollama pull llama3.2:3b
ollama list
```

Ollama normally runs in the background and serves its local API at `http://127.0.0.1:11434`. If `ollama` is not recognized, finish installing Ollama and open a new PowerShell window. Keep Ollama running while using AI features.

### 3. Set up and start the backend

From the **repository root**, run:

```powershell
py -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install --upgrade pip
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
backend\.venv\Scripts\python.exe -m uvicorn --app-dir backend main:app --reload
```

Keep this PowerShell window open. The `--app-dir backend` option is important: it adds the backend directory to Python's import path, so Uvicorn can import `main:app` when the command is run from the repository root. The database is initialized automatically at `backend\interviewai.db`.

Check the backend in a browser:

- API health: <http://127.0.0.1:8000/health>
- Interactive API documentation: <http://127.0.0.1:8000/docs>
- Ollama status: <http://127.0.0.1:8000/api/llm/status>
- MCP status: <http://127.0.0.1:8000/api/mcp/status>

### 4. Set up and start the frontend

Open a **second PowerShell window** in the repository root and run:

```powershell
cd frontend
npm ci
npm run dev
```

Open the URL Vite prints in the terminal, usually <http://localhost:5173>. Keep both the backend and frontend terminal windows running while using the application.

On later runs, dependency installation is not needed. Start the backend from the repository root:

```powershell
backend\.venv\Scripts\python.exe -m uvicorn --app-dir backend main:app --reload
```

Then start the frontend in a second window:

```powershell
cd frontend
npm run dev
```

To stop either development server, focus its terminal and press **Ctrl+C**.

## First-run verification

1. Confirm `http://127.0.0.1:8000/health` returns `{"status":"healthy"}`.
2. Open the app at `http://localhost:5173`.
3. Check the app's Ollama status. If the configured model is missing, run `ollama pull llama3.2:3b`.
4. Create a short practice interview for a role such as “Software Engineer” or “Financial Analyst.”
5. Confirm the interview shows role-relevant questions and can be opened from interview history.
6. Submit an answer to see the evaluation and scorecard.
7. Optionally try a PDF/TXT/MD upload, voice recording, question read-aloud, and AI help chat.

The interview response includes `generation_source` (`ollama` or `fallback`). Ollama-generated questions are role- and context-driven. A validated fallback question set is only used if model output cannot be validated; if Ollama itself is unavailable, the app reports an error rather than claiming fallback output was AI-generated.

## Using the application

### Create and complete an interview

1. Choose a target role and experience level.
2. Choose **Mixed**, **Technical**, or **Behavioral**, and select 3–10 questions.
3. Optionally upload a resume and job description. The application extracts text and can show a comparison of matching and missing skills.
4. Start the interview. Questions are shown one at a time with progress information.
5. Type your response, or record it and submit the local transcript. Use the read-aloud controls if desired.
6. Submit each answer in order. Review its feedback, then continue until the scorecard appears.
7. Find saved sessions in **My interviews**. Use the delete control and confirm to remove a session and its associated answers.

### Documents and audio

- Resume and job-description files: PDF, TXT, or MD; maximum 10 MB per upload.
- Text files must be UTF-8 encoded.
- PDFs must contain extractable text. Scanned/image-only PDFs are not OCR processed.
- Audio uploads: WEBM, WAV, MP3, M4A, MP4, OGG, OPUS, AAC, or FLAC; maximum 25 MB.
- Transcription uses Faster-Whisper on the local machine with the `tiny.en` model by default.

## Configuration

Set optional environment variables in the same PowerShell window before starting the backend:

```powershell
$env:OLLAMA_MODEL = "llama3.2:3b"
$env:WHISPER_MODEL = "tiny.en"
backend\.venv\Scripts\python.exe -m uvicorn --app-dir backend main:app --reload
```

| Variable | Default | Description |
| --- | --- | --- |
| `OLLAMA_MODEL` | `llama3.2:3b` | Model tag to use for questions, answer evaluation, and help chat. The model must already be pulled with Ollama. |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama API URL. For privacy, the backend only accepts HTTP URLs on localhost or a loopback IP address. |
| `WHISPER_MODEL` | `tiny.en` | Faster-Whisper model used for local transcription. |

For example, after pulling a different Ollama model, set `OLLAMA_MODEL` to its exact tag. Environment variables set in one PowerShell window do not carry over to another window.

## API overview

The backend's complete interactive schema is available at <http://127.0.0.1:8000/docs> while it is running.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Backend health check. |
| `GET` | `/api/dashboard` | Interview totals, average scores, recent sessions, and score progress. |
| `GET` | `/api/interviews` | List saved interviews. |
| `POST` | `/api/interviews` | Generate and save a role-specific interview. |
| `GET` | `/api/interviews/{interview_id}` | Retrieve an interview and its answers. |
| `POST` | `/api/interviews/{interview_id}/answers` | Evaluate and save an answer. Answers must be submitted in question order. |
| `DELETE` | `/api/interviews/{interview_id}` | Delete an interview and its answers. |
| `POST` | `/api/parse-document` | Extract text from a PDF, TXT, or MD upload. |
| `POST` | `/api/analyze-documents` | Compare resume text with job-description text. |
| `POST` | `/api/transcribe` | Transcribe an audio upload locally. |
| `GET` | `/api/llm/status` | Check local Ollama/model availability. |
| `GET` | `/api/llm/test` | Test model response generation. |
| `GET` | `/api/mcp/status` | Check the local MCP server and its tools. |
| `POST` | `/api/help/chat` | Ask the local model a help question. |

## Privacy and local data

- The application is local-first. Ollama, the MCP subprocess, PDF extraction, SQLite, and audio transcription run on your computer.
- Ollama is restricted to a loopback address; the backend rejects a non-local Ollama URL.
- Resume context used in question generation is privacy-filtered to remove detected contact details.
- Original resume and job-description text is not stored in the interview database. Interview questions, submitted answers, scores, and evaluations are stored locally.
- Audio recordings are written to a temporary file for transcription and removed afterward. The resulting transcript and evaluation are saved with the interview when submitted.
- The help-chat history is held in the browser tab and sent as limited recent context; it is not persisted in the interview database.
- The SQLite database is `backend\interviewai.db`. Back up this file to preserve interview history. Removing it resets saved local interview data.
- Do not commit real resumes, recordings, secrets, `.env` files, or your local database to the repository.

## Troubleshooting

### `Error loading ASGI app. Could not import module "main".`

Run Uvicorn from the repository root using the backend app directory:

```powershell
backend\.venv\Scripts\python.exe -m uvicorn --app-dir backend main:app --reload
```

If you are already in the `backend` directory, use:

```powershell
.venv\Scripts\python.exe -m uvicorn main:app --reload
```

Check that `backend\main.py` exists and that the command uses the project's virtual environment. Do not run `uvicorn main:app` from the repository root without `--app-dir backend`.

### Ollama is not responding or the model is missing

1. Start or reopen the Ollama application.
2. In PowerShell, run `ollama list` and check for the configured model.
3. Pull the default model if needed: `ollama pull llama3.2:3b`.
4. Check <http://127.0.0.1:8000/api/llm/status>.
5. If you changed `OLLAMA_MODEL`, confirm its value exactly matches a tag shown by `ollama list`, then restart the backend.

### The browser cannot reach the API

- Confirm both backend and frontend terminals are still running.
- Open <http://127.0.0.1:8000/health> and check that it returns a healthy response.
- Use the Vite URL, normally `http://localhost:5173`, rather than opening frontend files directly from disk.
- If port 8000 or 5173 is already in use, stop the other application using it or configure an available port.

### Voice recording or transcription does not work

- Use a supported browser and grant microphone permission to the local site.
- Check Windows microphone privacy settings and confirm the microphone works in another application.
- Try a clear recording with speech; empty or speech-free recordings cannot be transcribed.
- The first transcription can take longer because Faster-Whisper may download its model. Keep the backend running and check its terminal for errors.
- You can use typed answers if voice input is unavailable.

### Upload is rejected

- Use a PDF, TXT, or MD file of 10 MB or less for documents, or a supported audio format of 25 MB or less.
- Save text files as UTF-8.
- Scanned/image-only PDFs do not contain extractable text and are not OCR processed.

## Development and tests

### Frontend checks

```powershell
cd frontend
npm run lint
npm run build
```

### Backend tests

Run from the repository root:

```powershell
backend\.venv\Scripts\python.exe -m unittest discover -s backend -p "test_*.py" -v
backend\.venv\Scripts\python.exe -m pip check
```

Backend tests use temporary SQLite databases and mocked model requests; they do not require a running Ollama service or live model inference. The GitHub Actions workflow validates frontend lint/build and backend tests/dependencies. Its backend matrix covers Python 3.10 and 3.12.

### Run the production frontend build locally

Build the frontend with `npm run build`, then preview the static build with:

```powershell
npm run preview
```

The preview is for checking the frontend build; the API backend still needs to be running for application features.

## Deployment notes

The interactive application is designed for local development and is **not ready to expose directly to the public internet**. The backend expects local Ollama, starts MCP as a local subprocess, stores data in a local SQLite file, and allows only the local Vite development origins. A hosted static frontend cannot connect to your computer's Ollama service.

Public deployment requires a model-capable backend, durable database storage, production CORS and access controls, secure handling of resumes and answers, and an explicit privacy policy. Do not expose the development API publicly as-is.

A static, non-interactive project page is published at [YashChauhan178.github.io/InterviewAI](https://YashChauhan178.github.io/InterviewAI/). It publishes project information and screenshots from `docs/`; it does not host the interactive app or include local candidate data.

## Project structure

```text
InterviewAI/
├── backend/
│   ├── main.py                 # FastAPI routes and SQLite persistence
│   ├── llm_service.py          # Ollama question generation and evaluation
│   ├── document_analysis.py    # Resume/JD parsing and comparison
│   ├── audio_transcription.py  # Local Faster-Whisper transcription
│   ├── mcp_client.py           # Launch and connect to MCP subprocess
│   ├── mcp_server.py           # Local resume/JD analysis tools
│   ├── requirements.txt        # Python dependencies
│   └── test_*.py               # Backend tests
├── frontend/
│   ├── src/                    # React application and styles
│   ├── package.json            # Frontend scripts and dependencies
│   └── vite.config.js          # Local API proxy configuration
├── docs/
│   └── screenshots/            # Project screenshots
└── .github/workflows/          # CI and project-page workflows
```
