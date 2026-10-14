from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import tempfile
import uuid
from collections.abc import Generator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, NoReturn

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator

from document_analysis import (
    analyze_job_description,
    compare_resume_to_job,
    extract_contact_details,
    extract_resume_sections,
)
from audio_transcription import AudioTranscriptionError, FasterWhisperTranscriber
from llm_service import EVALUATION_DIMENSIONS, GeneratedInterview, LLMServiceError, OllamaService
from mcp_client import connect_mcp_server


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "interviewai.db"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_AUDIO_UPLOAD_BYTES = 25 * 1024 * 1024
logger = logging.getLogger(__name__)
llm_service = OllamaService()
audio_transcriber = FasterWhisperTranscriber()


@contextmanager
def connect_db() -> Generator[sqlite3.Connection, None, None]:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database() -> None:
    with connect_db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS interviews (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                experience_level TEXT NOT NULL,
                interview_type TEXT NOT NULL,
                questions TEXT NOT NULL,
                question_details TEXT NOT NULL DEFAULT '[]',
                generation_source TEXT NOT NULL DEFAULT 'ollama',
                status TEXT NOT NULL DEFAULT 'in_progress',
                current_index INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
                question_index INTEGER NOT NULL,
                answer TEXT NOT NULL,
                score INTEGER NOT NULL,
                evaluation TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(interview_id, question_index)
            );
            """
        )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(interviews)")
        }
        if "question_details" not in columns:
            connection.execute(
                "ALTER TABLE interviews ADD COLUMN question_details TEXT NOT NULL DEFAULT '[]'"
            )
        if "generation_source" not in columns:
            connection.execute(
                "ALTER TABLE interviews ADD COLUMN generation_source TEXT NOT NULL DEFAULT 'ollama'"
            )


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title="InterviewAI API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class InterviewRequest(BaseModel):
    role: str = Field(min_length=2, max_length=100)
    experience_level: Literal["Entry-level", "Mid-level", "Senior", "Lead / Staff"] = "Mid-level"
    interview_type: Literal["Mixed", "Technical", "Behavioral"] = "Mixed"
    question_count: int = Field(default=5, ge=3, le=10)
    resume_text: str = Field(default="", max_length=30_000)
    job_description: str = Field(default="", max_length=15_000)

    @field_validator("role")
    @classmethod
    def strip_role(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("Role must contain at least 2 non-whitespace characters")
        return value


class AnswerRequest(BaseModel):
    question_index: int = Field(ge=0)
    answer: str = Field(min_length=1, max_length=10_000)

    @field_validator("answer")
    @classmethod
    def strip_answer(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Answer cannot be blank")
        return value


class DocumentAnalysisRequest(BaseModel):
    resume_text: str = Field(default="", max_length=30_000)
    job_description: str = Field(default="", max_length=15_000)


class HelpChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2_000)


class HelpChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2_000)
    history: list[HelpChatMessage] = Field(default_factory=list, max_length=8)

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be blank")
        return value


def row_to_interview(row: sqlite3.Row, connection: sqlite3.Connection) -> dict[str, Any]:
    answers = connection.execute(
        "SELECT question_index, answer, score, evaluation FROM answers WHERE interview_id = ? ORDER BY question_index",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "role": row["role"],
        "experience_level": row["experience_level"],
        "interview_type": row["interview_type"],
        "questions": json.loads(row["questions"]),
        "question_details": json.loads(row["question_details"]),
        "generation_source": row["generation_source"],
        "status": row["status"],
        "current_index": row["current_index"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "answers": [
            {
                "question_index": answer["question_index"],
                "answer": answer["answer"],
                "score": answer["score"],
                "evaluation": json.loads(answer["evaluation"]),
            }
            for answer in answers
        ],
    }


def get_interview_or_404(connection: sqlite3.Connection, interview_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM interviews WHERE id = ?", (interview_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Interview not found")
    return row


def raise_llm_http_error(error: LLMServiceError) -> NoReturn:
    raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@app.get("/")
def home() -> dict[str, str]:
    return {"message": "InterviewAI backend is running"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/api/llm/status")
async def llm_status() -> dict[str, Any]:
    return await llm_service.status()


@app.get("/api/mcp/status")
async def mcp_status() -> dict[str, Any]:
    try:
        async with connect_mcp_server() as client:
            tools = await client.list_tools()
    except (McpError, OSError, RuntimeError) as error:
        raise HTTPException(
            status_code=503,
            detail="The local InterviewAI MCP server is unavailable.",
        ) from error
    return {
        "connected": True,
        "server": "InterviewAI",
        "tools": [
            {"name": tool.name, "description": tool.description}
            for tool in tools.tools
        ],
    }


@app.get("/api/llm/test")
async def test_llm() -> dict[str, Any]:
    try:
        return (await llm_service.test_response()).model_dump(mode="json")
    except LLMServiceError as error:
        raise_llm_http_error(error)


@app.post("/api/help/chat")
async def help_chat(request: HelpChatRequest) -> dict[str, str]:
    try:
        answer = await llm_service.answer_help_question(
            request.message,
            [message.model_dump() for message in request.history],
        )
    except LLMServiceError as error:
        raise_llm_http_error(error)
    return {"answer": answer}


@app.post("/api/analyze-documents")
def analyze_documents(request: DocumentAnalysisRequest) -> dict[str, Any]:
    return compare_resume_to_job(request.resume_text, request.job_description)


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT i.*, AVG(a.score) AS average_score, COUNT(a.id) AS answer_count
            FROM interviews i LEFT JOIN answers a ON a.interview_id = i.id
            GROUP BY i.id ORDER BY i.created_at DESC
            """
        ).fetchall()
        completed = [row for row in rows if row["status"] == "completed"]
        score_progress: list[dict[str, Any]] = []
        dimension_scores: dict[str, list[int]] = {
            name: [] for name in EVALUATION_DIMENSIONS
        }
        for row in reversed(completed):
            answer_rows = connection.execute(
                "SELECT score, evaluation FROM answers WHERE interview_id = ? ORDER BY question_index",
                (row["id"],),
            ).fetchall()
            for answer in answer_rows:
                evaluation = json.loads(answer["evaluation"])
                for criterion in evaluation.get("criteria", []):
                    name = str(criterion.get("name", "")).casefold()
                    canonical_name = next(
                        (
                            dimension
                            for dimension in EVALUATION_DIMENSIONS
                            if dimension.casefold() == name
                        ),
                        None,
                    )
                    if canonical_name is not None:
                        dimension_scores[canonical_name].append(int(criterion["score"]))
            if row["average_score"] is not None:
                score_progress.append(
                    {
                        "id": row["id"],
                        "role": row["role"],
                        "created_at": row["created_at"],
                        "score": round(row["average_score"]),
                    }
                )
    scores = [row["average_score"] for row in completed if row["average_score"] is not None]
    score_progress = score_progress[-8:]
    return {
        "total_interviews": len(rows),
        "completed_interviews": len(completed),
        "average_score": round(sum(scores) / len(scores)) if scores else 0,
        "practice_minutes": sum(row["answer_count"] for row in rows) * 3,
        "score_progress": score_progress,
        "score_dimensions": [
            {
                "name": name,
                "score": round(sum(values) / len(values)) if values else 0,
                "answer_count": len(values),
            }
            for name, values in dimension_scores.items()
        ],
        "recent_interviews": [
            {
                "id": row["id"],
                "role": row["role"],
                "experience_level": row["experience_level"],
                "interview_type": row["interview_type"],
                "status": row["status"],
                "average_score": round(row["average_score"]) if row["average_score"] is not None else None,
                "created_at": row["created_at"],
            }
            for row in rows[:6]
        ],
    }


@app.get("/api/interviews")
def list_interviews() -> list[dict[str, Any]]:
    with connect_db() as connection:
        rows = connection.execute("SELECT * FROM interviews ORDER BY created_at DESC").fetchall()
        return [row_to_interview(row, connection) for row in rows]


@app.post("/api/interviews")
async def create_interview(request: InterviewRequest) -> dict[str, Any]:
    interview_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        generated: GeneratedInterview = await llm_service.generate_interview(
            role=request.role,
            experience_level=request.experience_level,
            interview_type=request.interview_type,
            question_count=request.question_count,
            resume_text=request.resume_text,
            job_description=request.job_description,
        )
    except LLMServiceError as error:
        raise_llm_http_error(error)
    if generated.generation_source == "fallback":
        logger.warning("Using validated fallback questions for interview role %s", request.role)
    question_details = [question.model_dump(mode="json") for question in generated.questions]
    questions = [question.question for question in generated.questions]
    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO interviews
                (id, role, experience_level, interview_type, questions, question_details,
                 generation_source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interview_id,
                request.role.strip(),
                request.experience_level,
                request.interview_type,
                json.dumps(questions),
                json.dumps(question_details),
                generated.generation_source,
                created_at,
            ),
        )
        row = get_interview_or_404(connection, interview_id)
        return row_to_interview(row, connection)


@app.get("/api/interviews/{interview_id}")
def get_interview(interview_id: str) -> dict[str, Any]:
    with connect_db() as connection:
        row = get_interview_or_404(connection, interview_id)
        return row_to_interview(row, connection)


@app.delete("/api/interviews/{interview_id}", status_code=204)
def delete_interview(interview_id: str) -> None:
    with connect_db() as connection:
        cursor = connection.execute("DELETE FROM interviews WHERE id = ?", (interview_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Interview not found")


@app.post("/api/interviews/{interview_id}/answers")
async def submit_answer(interview_id: str, request: AnswerRequest) -> dict[str, Any]:
    with connect_db() as connection:
        row = get_interview_or_404(connection, interview_id)
        questions = json.loads(row["questions"])
        if request.question_index >= len(questions):
            raise HTTPException(status_code=400, detail="Question index is out of range")
        if row["status"] == "completed":
            raise HTTPException(status_code=409, detail="This interview has already been completed")
        if request.question_index > row["current_index"]:
            raise HTTPException(
                status_code=409,
                detail="Complete earlier questions before continuing",
            )

        question_details: Any = json.loads(row["question_details"])
        if request.question_index < len(question_details):
            detail: dict[str, Any] = question_details[request.question_index]
        else:
            detail = {
                "competency": "Communication",
                "evaluation_criteria": ["Relevance", "Structure", "Specificity"],
                "suggested_answer": "",
            }
        role = str(row["role"])
        experience_level = str(row["experience_level"])
        interview_type = str(row["interview_type"])
        question = str(questions[request.question_index])
        competency = str(detail.get("competency", "Communication"))
        try:
            criteria = TypeAdapter(list[str]).validate_python(
                detail.get("evaluation_criteria", [])
            )
        except ValidationError:
            criteria = []
        if not criteria:
            criteria = ["Relevance", "Structure", "Specificity"]
        sample_answer = str(detail.get("suggested_answer", ""))
        previous_answers = connection.execute(
            """
            SELECT question_index, answer FROM answers
            WHERE interview_id = ? AND question_index < ?
            ORDER BY question_index DESC LIMIT 4
            """,
            (interview_id, request.question_index),
        ).fetchall()
        conversation_history = [
            {
                "question": str(questions[answer["question_index"]])[:500],
                "answer": str(answer["answer"])[:1000],
            }
            for answer in reversed(previous_answers)
        ]
        existing_answer = connection.execute(
            "SELECT 1 FROM answers WHERE interview_id = ? AND question_index = ?",
            (interview_id, request.question_index),
        ).fetchone()
        allow_follow_up = not bool(detail.get("is_follow_up")) and existing_answer is None

    try:
        evaluation = await llm_service.evaluate_answer(
            role=role,
            experience_level=experience_level,
            interview_type=interview_type,
            question=question,
            competency=competency,
            criteria=criteria,
            sample_answer=sample_answer,
            answer=request.answer,
            conversation_history=conversation_history,
            previous_questions=[str(item) for item in questions],
            allow_follow_up=allow_follow_up,
        )
    except LLMServiceError as error:
        raise_llm_http_error(error)
    evaluation_data = evaluation.model_dump(mode="json")
    evaluation_data["word_count"] = len(request.answer.split())
    if not allow_follow_up:
        evaluation_data["follow_up_question"] = None
    evaluation_data["note"] = (
        "Local Ollama feedback scores relevance, completeness, clarity, and technical accuracy "
        "using the role-specific question and competency."
    )

    with connect_db() as connection:
        row = get_interview_or_404(connection, interview_id)
        if row["status"] == "completed":
            raise HTTPException(status_code=409, detail="This interview has already been completed")
        connection.execute(
            """
            INSERT INTO answers (interview_id, question_index, answer, score, evaluation, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(interview_id, question_index) DO UPDATE SET
                answer = excluded.answer, score = excluded.score,
                evaluation = excluded.evaluation, created_at = excluded.created_at
            """,
            (
                interview_id,
                request.question_index,
                request.answer,
                evaluation_data["score"],
                json.dumps(evaluation_data),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        follow_up = evaluation_data.get("follow_up_question") if allow_follow_up else None
        if follow_up:
            question_details = json.loads(row["question_details"])
            while len(question_details) < len(questions):
                question_details.append(
                    {
                        "question": questions[len(question_details)],
                        "competency": "Communication",
                        "evaluation_criteria": ["Relevance", "Structure", "Specificity"],
                        "suggested_answer": "",
                    }
                )
            questions.insert(request.question_index + 1, follow_up)
            question_details.insert(
                request.question_index + 1,
                {
                    "question": follow_up,
                    "competency": competency,
                    "evaluation_criteria": criteria,
                    "suggested_answer": "",
                    "is_follow_up": True,
                },
            )
            connection.execute(
                "UPDATE interviews SET questions = ?, question_details = ? WHERE id = ?",
                (json.dumps(questions), json.dumps(question_details), interview_id),
            )
        is_last_question = request.question_index == len(questions) - 1
        connection.execute(
            "UPDATE interviews SET current_index = ?, status = ?, completed_at = ? WHERE id = ?",
            (
                min(request.question_index + 1, len(questions) - 1),
                "completed" if is_last_question else "in_progress",
                datetime.now(timezone.utc).isoformat() if is_last_question else None,
                interview_id,
            ),
        )
        updated = get_interview_or_404(connection, interview_id)
        return {"interview": row_to_interview(updated, connection), "evaluation": evaluation_data}


@app.post("/api/transcribe")
async def transcribe_audio(file: UploadFile = File(...)) -> dict[str, str]:
    temporary_path: Path | None = None
    try:
        suffix = Path(file.filename or "").suffix.lower()
        supported_suffixes = {
            ".aac", ".flac", ".m4a", ".mp3", ".mp4", ".ogg", ".opus", ".wav", ".webm"
        }
        if suffix not in supported_suffixes:
            raise HTTPException(
                status_code=415,
                detail="Upload a WEBM, WAV, MP3, M4A, MP4, OGG, OPUS, AAC, or FLAC recording.",
            )
        content_type = (file.content_type or "").lower()
        if not (
            content_type.startswith("audio/")
            or content_type in {"application/octet-stream", "video/webm"}
        ):
            raise HTTPException(status_code=415, detail="The uploaded file must contain audio.")
        content = await file.read(MAX_AUDIO_UPLOAD_BYTES + 1)
        if not content:
            raise HTTPException(status_code=400, detail="The audio recording is empty.")
        if len(content) > MAX_AUDIO_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Audio uploads must be 25 MB or smaller.")
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
        transcript = await asyncio.to_thread(audio_transcriber.transcribe, temporary_path)
        return {"text": transcript}
    except AudioTranscriptionError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    finally:
        await file.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@app.post("/api/parse-document")
async def parse_document(
    file: UploadFile = File(...),
    kind: str = Form(default="resume"),
) -> dict[str, Any]:
    if kind not in ("resume", "job_description"):
        raise HTTPException(status_code=400, detail="Document kind must be resume or job_description")
    filename = (file.filename or "").lower()
    if Path(filename).suffix not in (".pdf", ".txt", ".md"):
        raise HTTPException(status_code=415, detail="Upload a PDF, TXT, or MD file")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Files must be 10 MB or smaller")
    if Path(filename).suffix == ".pdf":
        try:
            with pymupdf.open(stream=content, filetype="pdf") as document:
                text_parts: list[str] = []
                for page in document:
                    page_content: Any = page
                    text_parts.append(page_content.get_text("text"))
                text = "\n".join(text_parts)
        except pymupdf.FileDataError as error:
            raise HTTPException(status_code=400, detail="The uploaded PDF could not be read") from error
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise HTTPException(status_code=400, detail="Text files must use UTF-8 encoding") from error
    text = text.strip()
    if not text:
        if Path(filename).suffix == ".pdf":
            raise HTTPException(
                status_code=422,
                detail="This PDF has no selectable text. Scanned PDFs are not supported yet.",
            )
        raise HTTPException(status_code=400, detail="No readable text was found in the file")
    if len(text) > 30_000:
        text = text[:30_000]
    result: dict[str, Any] = {"text": text, "filename": file.filename or "document"}
    if kind == "resume":
        result["sections"] = extract_resume_sections(text)
        result["contact_details"] = extract_contact_details(text)
        result["skills"] = compare_resume_to_job(text, "")["resume_skills"]
    else:
        result["analysis"] = analyze_job_description(text)
    return result
