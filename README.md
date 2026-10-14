# InterviewAI

**A local-first mock interview coach.** InterviewAI turns a target role, a resume, and a job description into a structured practice interview, then evaluates written or transcribed spoken answers with actionable feedback. The React dashboard tracks saved sessions and score progress.

## Highlights

- **Personalized practice:** Generate 3–10 technical, behavioral, or mixed questions for a selected role and experience level.
- **Resume and job-description analysis:** Parse PDF, TXT, and Markdown locally; compare recognized skills and responsibilities before question generation.
- **Structured feedback:** Score relevance, completeness, clarity, and technical accuracy; review evidence, strengths, improvements, and a suggested answer.
- **Voice practice:** Read questions aloud, record answers in the browser, and transcribe locally with Faster-Whisper. Typed answers remain available as a fallback.
- **Persistent history:** Save questions, answers, rubrics, and feedback in SQLite; review score trends and delete an interview with its saved answers from My interviews.
- **In-app AI help:** Ask the local Ollama model for guidance on using the app and preparing for interviews; recent chat context is bounded and is not persisted.
- **Local AI and MCP:** Use Ollama and the project MCP tools on the same machine. Interview generation reuses its MCP session, and successful tool-status checks are briefly cached to avoid redundant process startup. No paid AI API key is needed.
- **Accessible presentation:** Responsive dashboard, light/dark themes, upload and API error messages, progress indicators, and keyboard focus states.

## Screenshots

| Dashboard and score progress | Interview scorecard |
| --- | --- |
| ![Dashboard](docs/screenshots/dashboard.png) | ![Interview scorecard](docs/screenshots/results.png) |

## Stack

| Area | Technology |
| --- | --- |
| Frontend | React, Vite, React Router, custom responsive CSS |
| API | FastAPI, Pydantic, Uvicorn |
| Persistence | SQLite |
| Local model | Ollama (default: `llama3.2:3b`) |
| Document analysis | PyMuPDF, spaCy tokenizer and local skill vocabulary |
| MCP | Python MCP SDK, stdio server in `backend/mcp_server.py` |
| Speech | Browser MediaRecorder and Speech Synthesis; Faster-Whisper transcription |

## Run locally on Windows

### Prerequisites

- Windows 10 or 11
- Python 3.10 or later
- Node.js 20.19+ or 22.12+
- Ollama and enough disk space for the selected model (about 2–3 GB for `llama3.2:3b`)

### 1. Install the local model

Install Ollama from [ollama.com](https://ollama.com/download), then open PowerShell and run:

```powershell
ollama pull llama3.2:3b
```

Ollama runs its API locally at `http://127.0.0.1:11434`. To use another model already installed:

```powershell
$env:OLLAMA_MODEL = "qwen2.5:3b"
```

InterviewAI intentionally rejects remote Ollama endpoints so private resume and answer data are not sent to a cloud model.

### 2. Install and start the backend

From the repository root:

```powershell
py -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
backend\.venv\Scripts\python.exe -m uvicorn --app-dir backend main:app --reload
```

The backend initializes `backend\interviewai.db` automatically. API docs are at `http://127.0.0.1:8000/docs`; health is at `http://127.0.0.1:8000/health`.

### 3. Install and start the frontend

In a second PowerShell window:

```powershell
cd frontend
npm install
npm run dev
```

Open the Vite URL (usually `http://localhost:5173`). The development server proxies `/api` requests to FastAPI.

## Privacy and data

- Ollama, the MCP stdio server, document parsing, and speech transcription run locally.
- Contact details are removed from the candidate context used for question generation.
- Original resume and job-description text is not saved in the interview database.
- Audio is written to a temporary file for transcription and removed afterward; the confirmed transcript and feedback are saved with the session.
- Interview history is local to this machine in `backend\interviewai.db`. Back up that file if you want to preserve history; deleting it resets local practice data.

## Upload and API behavior

- Resume and job-description uploads accept PDF, TXT, and MD files up to 10 MB. Unsupported types return `415`, oversized files return `413`, damaged PDFs return `400`, and scanned/image-only PDFs return a clear `422` response.
- Audio transcription accepts WEBM, WAV, MP3, M4A, MP4, OGG, OPUS, AAC, and FLAC up to 25 MB.
- API validation errors are returned as readable FastAPI detail messages and shown in the app. If Ollama or the model is unavailable, the UI reports that state; malformed generated question data uses a validated fallback set.
- The API rejects answers submitted for later unanswered questions, preventing accidental skips from completing a session.
- Delete saved interviews from **My interviews** using the trash button and confirmation. The API deletes associated answers in the same SQLite transaction.
- The floating **AI help** chat sends your question and at most eight recent chat messages to the configured local Ollama model; chat history is kept only in the browser tab.
- `/api/dashboard` reports session totals, average score, recent interviews, up to eight completed-session score points, and averages for the current four-dimension evaluation rubric.
- `/api/llm/status`, `/api/llm/test`, `/api/mcp/status`, and `/api/help/chat` report or use local model and MCP availability.

## Development and tests

Frontend lint and production build:

```powershell
cd frontend
npm run lint
npm run build
```

Backend tests (run from the repository root):

```powershell
backend\.venv\Scripts\python.exe -m unittest discover -s backend -p "test_*.py" -v
backend\.venv\Scripts\python.exe -m pip check
```

The backend tests cover persistence and scorecard summaries, input and upload validation, incomplete/scanned/broken/oversized documents, software and finance requirement analysis, Ollama failures, structured evaluation, follow-ups, MCP tools, and audio upload handling. They use a temporary SQLite database and mocked model requests; they do not require live model inference.

GitHub Actions runs the frontend lint/build and backend tests plus dependency checks on pushes and pull requests. The backend CI matrix covers Python 3.10 and 3.12.

## Deployment status

The interactive application targets **local development**, not an internet-facing deployment. The default backend requires a local Ollama service, launches its MCP server as a local subprocess, stores SQLite data on its local filesystem, and only allows the development frontend origins. A hosted frontend alone would not make local Ollama reachable, and an ephemeral backend filesystem would lose interview history.

A static, non-interactive portfolio is published at [YashChauhan178.github.io/InterviewAI](https://yashchauhan178.github.io/InterviewAI/). This repository contains the full application source; GitHub Pages publishes only the project information and screenshots in `docs/`. Candidate data remains local and is not part of the repository.

The interactive application is designed to run locally. Hosting it as a public service requires a model-capable backend, durable database storage, production CORS and access controls, and an explicit privacy policy for resume and answer data. Do not expose the development API publicly as-is.
