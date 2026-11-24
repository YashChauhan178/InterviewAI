from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import secrets
import tempfile
import time
import uuid
from collections.abc import Generator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, NoReturn

import httpx
import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi import Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator
from starlette.middleware.sessions import SessionMiddleware

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
QUESTION_CATEGORIES = (
    "Basic Aptitude",
    "Logical Reasoning",
    "Coding Rounds & Interview",
)
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_AUDIO_UPLOAD_BYTES = 25 * 1024 * 1024
FREE_INTERVIEW_LIMIT = 3
FREE_INTERVIEW_WINDOW_DAYS = 14
MCP_STATUS_CACHE_TTL_SECONDS = 30
logger = logging.getLogger(__name__)
SESSION_SECRET = os.getenv("INTERVIEWAI_SESSION_SECRET")
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_urlsafe(48)
    logger.warning(
        "INTERVIEWAI_SESSION_SECRET is unset; sessions will be invalidated when the backend restarts. "
        "Configure a persistent secret before deployment."
    )
llm_service = OllamaService()
audio_transcriber = FasterWhisperTranscriber()
_mcp_status_cache: dict[str, Any] | None = None
_mcp_status_cached_at = 0.0
_mcp_status_lock = asyncio.Lock()


class SQLServerResult:
    def __init__(self, cursor: Any) -> None:
        self.cursor = cursor
        self.rowcount = cursor.rowcount

    def _row_to_dict(self, row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            column[0]: value
            for column, value in zip(self.cursor.description, row, strict=True)
        }

    def fetchone(self) -> dict[str, Any] | None:
        return self._row_to_dict(self.cursor.fetchone())

    def fetchall(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in self.cursor.fetchall():
            converted = self._row_to_dict(row)
            if converted is not None:
                rows.append(converted)
        return rows


class SQLServerConnection:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> SQLServerResult:
        cursor = self.connection.cursor()
        if parameters:
            cursor.execute(query, parameters)
        else:
            cursor.execute(query)
        return SQLServerResult(cursor)


def sqlserver_connection_string() -> str:
    configured = os.getenv("INTERVIEWAI_SQLSERVER_CONNECTION_STRING")
    if configured:
        return configured
    driver = os.getenv("INTERVIEWAI_SQLSERVER_DRIVER", "ODBC Driver 18 for SQL Server")
    server = os.getenv("INTERVIEWAI_SQLSERVER_SERVER", r"localhost\SQLEXPRESS")
    database = os.getenv("INTERVIEWAI_SQLSERVER_DATABASE", "InterviewAI")
    return (
        f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
        "Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes;"
    )


def database_backend() -> str:
    backend = os.getenv("INTERVIEWAI_DB_BACKEND", "sqlserver").casefold()
    if backend not in {"sqlserver", "sqlite"}:
        raise RuntimeError("INTERVIEWAI_DB_BACKEND must be either 'sqlserver' or 'sqlite'.")
    return backend


@contextmanager
def connect_db() -> Generator[Any, None, None]:
    if database_backend() == "sqlserver":
        try:
            import pyodbc
        except ImportError as error:
            raise RuntimeError(
                "SQL Server storage requires pyodbc and the Microsoft ODBC Driver for SQL Server."
            ) from error
        try:
            raw_connection = pyodbc.connect(sqlserver_connection_string(), timeout=5)
        except pyodbc.Error as error:
            logger.exception("Could not connect to the configured SQL Server database")
            raise RuntimeError(
                "Could not connect to SQL Server. Check that the SQL Server service is running, "
                "the server/instance and database are correct, and Windows authentication is enabled."
            ) from error
        connection = SQLServerConnection(raw_connection)
        try:
            yield connection
            raw_connection.commit()
        except BaseException:
            raw_connection.rollback()
            raise
        finally:
            raw_connection.close()
        return

    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database() -> None:
    if database_backend() == "sqlserver":
        with connect_db() as connection:
            connection.execute(
                """
                IF OBJECT_ID(N'dbo.interviews', N'U') IS NULL
                CREATE TABLE dbo.interviews (
                    id NVARCHAR(36) NOT NULL PRIMARY KEY,
                    role NVARCHAR(100) NOT NULL,
                    experience_level NVARCHAR(30) NOT NULL,
                    interview_type NVARCHAR(20) NOT NULL,
                    question_category NVARCHAR(50) NOT NULL DEFAULT N'Coding Rounds & Interview',
                    difficulty NVARCHAR(20) NOT NULL DEFAULT N'Medium',
                    student_name NVARCHAR(120) NOT NULL DEFAULT N'Student',
                    student_identifier NVARCHAR(100) NOT NULL DEFAULT N'',
                    questions NVARCHAR(MAX) NOT NULL,
                    question_details NVARCHAR(MAX) NOT NULL DEFAULT N'[]',
                    generation_source NVARCHAR(20) NOT NULL DEFAULT N'ollama',
                    status NVARCHAR(20) NOT NULL DEFAULT N'in_progress',
                    current_index INT NOT NULL DEFAULT 0,
                    created_at NVARCHAR(40) NOT NULL,
                    completed_at NVARCHAR(40) NULL
                )
                """
            )
            for column, definition in (
                ("question_details", "NVARCHAR(MAX) NOT NULL DEFAULT N'[]'"),
                ("generation_source", "NVARCHAR(20) NOT NULL DEFAULT N'ollama'"),
                ("question_category", "NVARCHAR(50) NOT NULL DEFAULT N'Coding Rounds & Interview'"),
                ("difficulty", "NVARCHAR(20) NOT NULL DEFAULT N'Medium'"),
                ("student_name", "NVARCHAR(120) NOT NULL DEFAULT N'Student'"),
                ("student_identifier", "NVARCHAR(100) NOT NULL DEFAULT N''"),
            ):
                connection.execute(
                    f"IF COL_LENGTH(N'dbo.interviews', N'{column}') IS NULL "
                    f"ALTER TABLE dbo.interviews ADD {column} {definition}"
                )
            connection.execute(
                """
                IF OBJECT_ID(N'dbo.answers', N'U') IS NULL
                CREATE TABLE dbo.answers (
                    id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    interview_id NVARCHAR(36) NOT NULL,
                    question_index INT NOT NULL,
                    answer NVARCHAR(MAX) NOT NULL,
                    score INT NOT NULL,
                    evaluation NVARCHAR(MAX) NOT NULL,
                    created_at NVARCHAR(40) NOT NULL,
                    CONSTRAINT FK_answers_interviews FOREIGN KEY (interview_id)
                        REFERENCES dbo.interviews(id) ON DELETE CASCADE,
                    CONSTRAINT UQ_answers_interview_question UNIQUE (interview_id, question_index)
                )
                """
            )
            for statement in (
                """
                IF OBJECT_ID(N'dbo.users', N'U') IS NULL
                CREATE TABLE dbo.users (
                    id NVARCHAR(36) NOT NULL PRIMARY KEY,
                    email NVARCHAR(320) NOT NULL UNIQUE,
                    name NVARCHAR(120) NOT NULL,
                    created_at NVARCHAR(40) NOT NULL
                )
                """,
                """
                IF OBJECT_ID(N'dbo.oauth_identities', N'U') IS NULL
                CREATE TABLE dbo.oauth_identities (
                    provider NVARCHAR(20) NOT NULL,
                    provider_user_id NVARCHAR(200) NOT NULL,
                    user_id NVARCHAR(36) NOT NULL REFERENCES dbo.users(id),
                    email NVARCHAR(320) NOT NULL,
                    CONSTRAINT PK_oauth_identities PRIMARY KEY (provider, provider_user_id)
                )
                """,
                """
                IF OBJECT_ID(N'dbo.organizations', N'U') IS NULL
                CREATE TABLE dbo.organizations (
                    id NVARCHAR(36) NOT NULL PRIMARY KEY,
                    name NVARCHAR(120) NOT NULL,
                    slug NVARCHAR(140) NOT NULL UNIQUE,
                    [plan] NVARCHAR(20) NOT NULL DEFAULT N'free',
                    created_by NVARCHAR(36) NOT NULL,
                    created_at NVARCHAR(40) NOT NULL
                )
                """,
                """
                IF OBJECT_ID(N'dbo.organization_memberships', N'U') IS NULL
                CREATE TABLE dbo.organization_memberships (
                    organization_id NVARCHAR(36) NOT NULL REFERENCES dbo.organizations(id) ON DELETE CASCADE,
                    user_id NVARCHAR(36) NOT NULL REFERENCES dbo.users(id) ON DELETE CASCADE,
                    role NVARCHAR(20) NOT NULL,
                    created_at NVARCHAR(40) NOT NULL,
                    CONSTRAINT PK_organization_memberships PRIMARY KEY (organization_id, user_id)
                )
                """,
                """
                IF OBJECT_ID(N'dbo.user_sessions', N'U') IS NULL
                CREATE TABLE dbo.user_sessions (
                    id NVARCHAR(36) NOT NULL PRIMARY KEY,
                    user_id NVARCHAR(36) NOT NULL REFERENCES dbo.users(id) ON DELETE CASCADE,
                    organization_id NVARCHAR(36) NULL REFERENCES dbo.organizations(id),
                    expires_at NVARCHAR(40) NOT NULL,
                    created_at NVARCHAR(40) NOT NULL
                )
                """,
                """
                IF OBJECT_ID(N'dbo.organization_invitations', N'U') IS NULL
                CREATE TABLE dbo.organization_invitations (
                    id NVARCHAR(36) NOT NULL PRIMARY KEY,
                    organization_id NVARCHAR(36) NOT NULL REFERENCES dbo.organizations(id) ON DELETE CASCADE,
                    token_hash NVARCHAR(64) NOT NULL UNIQUE,
                    role NVARCHAR(20) NOT NULL,
                    created_by NVARCHAR(36) NOT NULL,
                    expires_at NVARCHAR(40) NOT NULL,
                    used_at NVARCHAR(40) NULL
                )
                """,
                """
                IF OBJECT_ID(N'dbo.billing_subscriptions', N'U') IS NULL
                CREATE TABLE dbo.billing_subscriptions (
                    id NVARCHAR(36) NOT NULL PRIMARY KEY,
                    owner_type NVARCHAR(20) NOT NULL,
                    owner_id NVARCHAR(36) NOT NULL,
                    provider_subscription_id NVARCHAR(100) NOT NULL UNIQUE,
                    plan_id NVARCHAR(100) NOT NULL,
                    status NVARCHAR(30) NOT NULL,
                    checkout_url NVARCHAR(2048) NULL,
                    current_period_end NVARCHAR(40) NULL,
                    created_at NVARCHAR(40) NOT NULL,
                    updated_at NVARCHAR(40) NOT NULL
                )
                """,
            ):
                connection.execute(statement)
            connection.execute(
                "IF COL_LENGTH(N'dbo.billing_subscriptions', N'checkout_url') IS NULL "
                "ALTER TABLE dbo.billing_subscriptions ADD checkout_url NVARCHAR(2048) NULL"
            )
            for column, definition in (
                ("tenant_id", "NVARCHAR(36) NULL"),
                ("user_id", "NVARCHAR(36) NULL"),
            ):
                connection.execute(
                    f"IF COL_LENGTH(N'dbo.interviews', N'{column}') IS NULL "
                    f"ALTER TABLE dbo.interviews ADD {column} {definition}"
                )
        return

    with connect_db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS interviews (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                experience_level TEXT NOT NULL,
                interview_type TEXT NOT NULL,
                question_category TEXT NOT NULL DEFAULT 'Coding Rounds & Interview',
                difficulty TEXT NOT NULL DEFAULT 'Medium',
                student_name TEXT NOT NULL DEFAULT 'Student',
                student_identifier TEXT NOT NULL DEFAULT '',
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
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_identities (
                provider TEXT NOT NULL,
                provider_user_id TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                email TEXT NOT NULL,
                PRIMARY KEY (provider, provider_user_id)
            );
            CREATE TABLE IF NOT EXISTS organizations (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                [plan] TEXT NOT NULL DEFAULT 'free',
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS organization_memberships (
                organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (organization_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS user_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                organization_id TEXT NULL REFERENCES organizations(id),
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS organization_invitations (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL,
                created_by TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT NULL
            );
            CREATE TABLE IF NOT EXISTS billing_subscriptions (
                id TEXT PRIMARY KEY,
                owner_type TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                provider_subscription_id TEXT NOT NULL UNIQUE,
                plan_id TEXT NOT NULL,
                status TEXT NOT NULL,
                checkout_url TEXT NULL,
                current_period_end TEXT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        billing_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(billing_subscriptions)")
        }
        if "checkout_url" not in billing_columns:
            connection.execute("ALTER TABLE billing_subscriptions ADD COLUMN checkout_url TEXT NULL")
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
        for column, definition in (
            ("question_category", "TEXT NOT NULL DEFAULT 'Coding Rounds & Interview'"),
            ("difficulty", "TEXT NOT NULL DEFAULT 'Medium'"),
            ("student_name", "TEXT NOT NULL DEFAULT 'Student'"),
            ("student_identifier", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in columns:
                connection.execute(f"ALTER TABLE interviews ADD COLUMN {column} {definition}")
        if "tenant_id" not in columns:
            connection.execute("ALTER TABLE interviews ADD COLUMN tenant_id TEXT NULL")
        if "user_id" not in columns:
            connection.execute("ALTER TABLE interviews ADD COLUMN user_id TEXT NULL")


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title="InterviewAI API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="interviewai_session",
    same_site="lax",
    https_only=os.getenv("INTERVIEWAI_COOKIE_SECURE", "false").casefold() == "true",
    max_age=60 * 60 * 12,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "INTERVIEWAI_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    from authlib.integrations.base_client.errors import OAuthError
    from authlib.integrations.starlette_client import OAuth
except ImportError as error:
    raise RuntimeError("OAuth login requires Authlib. Install backend requirements.") from error

oauth = OAuth()


def register_oauth_clients() -> None:
    google_id = os.getenv("GOOGLE_CLIENT_ID")
    google_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if google_id and google_secret:
        oauth.register(
            name="google",
            client_id=google_id,
            client_secret=google_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    github_id = os.getenv("GITHUB_CLIENT_ID")
    github_secret = os.getenv("GITHUB_CLIENT_SECRET")
    if github_id and github_secret:
        oauth.register(
            name="github",
            client_id=github_id,
            client_secret=github_secret,
            authorize_url="https://github.com/login/oauth/authorize",
            access_token_url="https://github.com/login/oauth/access_token",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "read:user user:email"},
        )
    apple_id = os.getenv("APPLE_CLIENT_ID")
    apple_secret = os.getenv("APPLE_CLIENT_SECRET")
    if apple_id and apple_secret:
        oauth.register(
            name="apple",
            client_id=apple_id,
            client_secret=apple_secret,
            server_metadata_url="https://appleid.apple.com/.well-known/openid-configuration",
            client_kwargs={"scope": "name email"},
        )


register_oauth_clients()


def current_public_url() -> str:
    return os.getenv("INTERVIEWAI_PUBLIC_URL", "http://localhost:5173").rstrip("/")


def oauth_callback_url(provider: str) -> str:
    api_base = os.getenv("INTERVIEWAI_API_PUBLIC_URL", current_public_url()).rstrip("/")
    return f"{api_base}/api/auth/{provider}/callback"


def provider_configuration() -> list[dict[str, Any]]:
    return [
        {"id": provider, "label": label, "enabled": oauth.create_client(provider) is not None}
        for provider, label in (
            ("google", "Google"),
            ("github", "GitHub"),
            ("apple", "Apple"),
        )
    ]


def slugify(value: str) -> str:
    slug = "-".join(part for part in "".join(
        character.lower() if character.isalnum() else " "
        for character in value
    ).split() if part)
    return f"{slug[:100] or 'company'}-{secrets.token_hex(4)}"


def ensure_personal_organization(connection: Any, user_id: str, user_name: str) -> str:
    existing = connection.execute(
        """
        SELECT o.id FROM organizations o
        JOIN organization_memberships m ON m.organization_id = o.id
        WHERE m.user_id = ? AND m.role = 'owner' AND o.slug LIKE ?
        ORDER BY o.created_at ASC
        """,
        (user_id, f"personal-{user_id[:8]}%"),
    ).fetchone()
    if existing is not None:
        return str(existing["id"])
    organization_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO organizations (id, name, slug, [plan], created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            organization_id,
            f"{user_name}'s workspace"[:120],
            f"personal-{user_id[:8]}-{secrets.token_hex(3)}",
            "free",
            user_id,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    connection.execute(
        "INSERT INTO organization_memberships (organization_id, user_id, role, created_at) VALUES (?, ?, ?, ?)",
        (organization_id, user_id, "owner", datetime.now(timezone.utc).isoformat()),
    )
    return organization_id


def require_current_user(request: Request) -> dict[str, Any]:
    session_id = request.session.get("auth_session_id")
    if not isinstance(session_id, str):
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    now = datetime.now(timezone.utc).isoformat()
    with connect_db() as connection:
        session = connection.execute(
            """
            SELECT s.user_id, s.organization_id, u.email, u.name, o.slug
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            JOIN organizations o ON o.id = s.organization_id
            WHERE s.id = ? AND s.expires_at > ?
            """,
            (session_id, now),
        ).fetchone()
        if session is None:
            request.session.clear()
            raise HTTPException(status_code=401, detail="Your session expired. Sign in again.")
        membership = connection.execute(
            """
            SELECT role FROM organization_memberships
            WHERE organization_id = ? AND user_id = ?
            """,
            (session["organization_id"], session["user_id"]),
        ).fetchone()
    if membership is None:
        raise HTTPException(status_code=403, detail="Your workspace membership is no longer active.")
    return {
        "id": str(session["user_id"]),
        "email": str(session["email"]),
        "name": str(session["name"]),
        "organization_id": str(session["organization_id"]),
        "organization_role": str(membership["role"]),
        "is_personal": str(session["slug"]).startswith("personal-"),
    }


def free_interview_usage(connection: Any, user_id: str) -> tuple[int, str | None]:
    window_start = (
        datetime.now(timezone.utc) - timedelta(days=FREE_INTERVIEW_WINDOW_DAYS)
    ).isoformat()
    usage = connection.execute(
        """
        SELECT COUNT(*) AS interview_count, MIN(created_at) AS oldest_created_at
        FROM interviews WHERE user_id = ? AND created_at >= ?
        """,
        (user_id, window_start),
    ).fetchone()
    oldest_created_at = usage["oldest_created_at"]
    reset_at = None
    if oldest_created_at is not None:
        reset_at = (
            datetime.fromisoformat(str(oldest_created_at))
            + timedelta(days=FREE_INTERVIEW_WINDOW_DAYS)
        ).isoformat()
    return int(usage["interview_count"]), reset_at


def add_or_get_oauth_user(
    provider: str,
    provider_user_id: str,
    email: str,
    name: str,
) -> tuple[str, str]:
    normalized_email = email.strip().casefold()
    with connect_db() as connection:
        identity = connection.execute(
            "SELECT user_id FROM oauth_identities WHERE provider = ? AND provider_user_id = ?",
            (provider, provider_user_id),
        ).fetchone()
        if identity is not None:
            user_id = str(identity["user_id"])
            user = connection.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
            return user_id, str(user["name"])
        existing_user = connection.execute(
            "SELECT id, name FROM users WHERE email = ?", (normalized_email,)
        ).fetchone()
        if existing_user is None:
            user_id = str(uuid.uuid4())
            clean_name = name.strip()[:120] or normalized_email.split("@", 1)[0]
            connection.execute(
                "INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?)",
                (user_id, normalized_email, clean_name, datetime.now(timezone.utc).isoformat()),
            )
        else:
            user_id = str(existing_user["id"])
            clean_name = str(existing_user["name"])
        connection.execute(
            "INSERT INTO oauth_identities (provider, provider_user_id, user_id, email) VALUES (?, ?, ?, ?)",
            (provider, provider_user_id, user_id, normalized_email),
        )
        organization_id = ensure_personal_organization(connection, user_id, clean_name)
        return user_id, clean_name


def create_auth_session(
    request: Request,
    user_id: str,
    user_name: str,
    *,
    account_type: str,
    organization_name: str,
) -> None:
    with connect_db() as connection:
        organization_id = ensure_personal_organization(connection, user_id, user_name)
        if account_type == "company" and organization_name.strip():
            clean_name = organization_name.strip()[:120]
            existing_company = connection.execute(
                """
                SELECT id FROM organizations
                WHERE created_by = ? AND name = ? AND slug NOT LIKE 'personal-%'
                ORDER BY created_at
                """,
                (user_id, clean_name),
            ).fetchone()
            if existing_company is not None:
                organization_id = str(existing_company["id"])
            else:
                organization_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO organizations (id, name, slug, [plan], created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        organization_id,
                        clean_name,
                        slugify(clean_name),
                        "free",
                        user_id,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                connection.execute(
                    "INSERT INTO organization_memberships (organization_id, user_id, role, created_at) VALUES (?, ?, ?, ?)",
                    (organization_id, user_id, "owner", datetime.now(timezone.utc).isoformat()),
                )
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        connection.execute(
            """
            INSERT INTO user_sessions (id, user_id, organization_id, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_id,
                user_id,
                organization_id,
                (now + timedelta(hours=12)).isoformat(),
                now.isoformat(),
            ),
        )
    request.session.clear()
    request.session["auth_session_id"] = session_id


def interview_scope(user: dict[str, Any], alias: str = "") -> tuple[str, tuple[Any, ...]]:
    prefix = f"{alias}." if alias else ""
    scope = f"{prefix}tenant_id = ?"
    parameters: tuple[Any, ...] = (user["organization_id"],)
    if user["organization_role"] not in {"owner", "admin", "interviewer"}:
        scope += f" AND {prefix}user_id = ?"
        parameters += (user["id"],)
    return scope, parameters


class InterviewRequest(BaseModel):
    role: str = Field(min_length=2, max_length=100)
    student_name: str = Field(default="Student", min_length=2, max_length=120)
    student_identifier: str = Field(default="", max_length=100)
    experience_level: Literal["Entry-level", "Mid-level", "Senior", "Lead / Staff"] = "Mid-level"
    interview_type: Literal["Mixed", "Technical", "Behavioral"] = "Mixed"
    question_category: Literal[
        "Basic Aptitude",
        "Logical Reasoning",
        "Coding Rounds & Interview",
    ] = "Coding Rounds & Interview"
    difficulty: Literal["Easy", "Medium", "Hard"] = "Medium"
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

    @field_validator("student_name", "student_identifier")
    @classmethod
    def strip_student_fields(cls, value: str) -> str:
        return value.strip()


class OrganizationRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("Organization name must contain at least 2 non-whitespace characters")
        return value


class JoinOrganizationRequest(BaseModel):
    invite_token: str = Field(min_length=32, max_length=128)


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


def row_to_interview(row: Any, connection: Any) -> dict[str, Any]:
    answers = connection.execute(
        "SELECT question_index, answer, score, evaluation FROM answers WHERE interview_id = ? ORDER BY question_index",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "role": row["role"],
        "experience_level": row["experience_level"],
        "interview_type": row["interview_type"],
        "question_category": row["question_category"],
        "difficulty": row["difficulty"],
        "student_name": row["student_name"],
        "student_identifier": row["student_identifier"],
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


def get_interview_or_404(connection: Any, interview_id: str) -> Any:
    row = connection.execute("SELECT * FROM interviews WHERE id = ?", (interview_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Interview not found")
    return row


def get_scoped_interview_or_404(
    connection: Any,
    interview_id: str,
    user: dict[str, Any],
) -> Any:
    scope, parameters = interview_scope(user)
    row = connection.execute(
        f"SELECT * FROM interviews WHERE id = ? AND {scope}",
        (interview_id, *parameters),
    ).fetchone()
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


@app.get("/api/storage/status")
def storage_status(user: dict[str, Any] = Depends(require_current_user)) -> dict[str, str]:
    backend = database_backend()
    with connect_db() as connection:
        if backend == "sqlserver":
            row = connection.execute(
                "SELECT DB_NAME() AS database_name"
            ).fetchone()
            return {"engine": "Microsoft SQL Server", "database": str(row["database_name"])}
        return {"engine": "SQLite", "database": str(DATABASE_PATH.name)}


@app.get("/api/auth/providers")
def auth_providers() -> dict[str, Any]:
    return {
        "providers": provider_configuration(),
        "company_signup": True,
    }


@app.get("/api/auth/me")
def auth_me(user: dict[str, Any] = Depends(require_current_user)) -> dict[str, Any]:
    with connect_db() as connection:
        organizations = connection.execute(
            """
            SELECT o.id, o.name, o.slug, o.[plan], m.role
            FROM organizations o JOIN organization_memberships m ON m.organization_id = o.id
            WHERE m.user_id = ? ORDER BY o.created_at
            """,
            (user["id"],),
        ).fetchall()
        subscription = connection.execute(
            """
            SELECT status, current_period_end FROM billing_subscriptions
            WHERE owner_type = 'student' AND owner_id = ?
            ORDER BY updated_at DESC
            """,
            (user["id"],),
        ).fetchall()
        interview_count, free_reset_at = (
            free_interview_usage(connection, str(user["id"]))
            if user["is_personal"]
            else (0, None)
        )
    active_subscription = next(
        (row for row in subscription if row["status"] in {"active", "authenticated"}),
        None,
    )
    plan = "pro" if active_subscription and active_subscription["status"] == "active" else "free"
    return {
        **user,
        "plan": plan,
        "subscription_status": active_subscription["status"] if active_subscription else "free",
        "free_interviews_remaining": (
            max(0, FREE_INTERVIEW_LIMIT - interview_count)
            if user["is_personal"] and plan == "free"
            else None
        ),
        "free_interviews_reset_at": (
            free_reset_at
            if user["is_personal"] and plan == "free" and interview_count >= FREE_INTERVIEW_LIMIT
            else None
        ),
        "free_interview_window_days": FREE_INTERVIEW_WINDOW_DAYS,
        "organizations": organizations,
    }


@app.get("/api/auth/{provider}/start")
async def auth_start(
    provider: str,
    request: Request,
    account_type: Literal["student", "company"] = "student",
    company_name: str = "",
) -> RedirectResponse:
    client = oauth.create_client(provider)
    if client is None:
        raise HTTPException(status_code=503, detail=f"{provider.title()} sign-in is not configured.")
    if account_type == "company" and len(company_name.strip()) < 2:
        raise HTTPException(status_code=422, detail="Enter a company name to create a company workspace.")
    request.session["account_type"] = account_type
    request.session["organization_name"] = company_name[:120]
    invite_token = request.query_params.get("invite_token", "")
    if invite_token:
        if not 32 <= len(invite_token) <= 128:
            raise HTTPException(status_code=422, detail="The invitation token is invalid.")
        request.session["invite_token"] = invite_token
    if provider == "apple":
        return await client.authorize_redirect(
            request,
            oauth_callback_url(provider),
            response_mode="query",
        )
    return await client.authorize_redirect(request, oauth_callback_url(provider))


@app.get("/api/auth/{provider}/callback")
async def auth_callback(provider: str, request: Request) -> RedirectResponse:
    client = oauth.create_client(provider)
    if client is None:
        raise HTTPException(status_code=503, detail=f"{provider.title()} sign-in is not configured.")
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as error:
        logger.warning("OAuth callback failed for provider %s", provider)
        raise HTTPException(status_code=400, detail="Sign-in was cancelled or the provider rejected the request.") from error
    if provider == "github":
        try:
            profile_response = await client.get("user", token=token)
            profile_response.raise_for_status()
            profile = profile_response.json()
            emails_response = await client.get("user/emails", token=token)
            emails_response.raise_for_status()
            emails = emails_response.json()
            if not isinstance(emails, list):
                raise HTTPException(status_code=502, detail="GitHub returned an invalid email profile.")
            email = next(
                (
                    item.get("email")
                    for item in emails
                    if isinstance(item, dict)
                    and item.get("primary")
                    and item.get("verified")
                ),
                None,
            ) or next(
                (
                    item.get("email")
                    for item in emails
                    if isinstance(item, dict) and item.get("verified")
                ),
                None,
            )
        except httpx.HTTPError as error:
            logger.warning("GitHub profile lookup failed")
            raise HTTPException(status_code=502, detail="GitHub could not verify your account. Please retry.") from error
        if not email:
            raise HTTPException(status_code=400, detail="GitHub must provide a verified email address.")
        if not isinstance(email, str):
            raise HTTPException(status_code=400, detail="GitHub returned an invalid email address.")
        provider_user_id = str(profile.get("id", ""))
        user_name = str(profile.get("name") or profile.get("login") or email.split("@", 1)[0])
    else:
        profile = token.get("userinfo")
        if not isinstance(profile, dict):
            raise HTTPException(status_code=400, detail="The identity provider returned no verified profile.")
        email = profile.get("email")
        verified = profile.get("email_verified")
        if not isinstance(email, str) or verified not in (True, "true", "True"):
            raise HTTPException(status_code=400, detail="The identity provider must return a verified email.")
        provider_user_id = str(profile.get("sub", ""))
        user_name = str(profile.get("name") or email.split("@", 1)[0])
    if not provider_user_id:
        raise HTTPException(status_code=400, detail="The identity provider returned an invalid user ID.")
    invite_token = str(request.session.get("invite_token", ""))
    account_type = str(request.session.get("account_type", "student"))
    organization_name = str(request.session.get("organization_name", ""))
    user_id, stored_name = add_or_get_oauth_user(provider, provider_user_id, str(email), user_name)
    create_auth_session(
        request,
        user_id,
        stored_name,
        account_type=account_type,
        organization_name=organization_name,
    )
    redirect_query = "login=success"
    if invite_token:
        token_hash = hashlib.sha256(invite_token.encode()).hexdigest()
        with connect_db() as connection:
            invitation = connection.execute(
                """
                SELECT id, organization_id, role, expires_at, used_at
                FROM organization_invitations WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if (
                invitation is not None
                and invitation["used_at"] is None
                and str(invitation["expires_at"]) > datetime.now(timezone.utc).isoformat()
            ):
                now = datetime.now(timezone.utc).isoformat()
                claimed = connection.execute(
                    """
                    UPDATE organization_invitations SET used_at = ?
                    WHERE id = ? AND used_at IS NULL AND expires_at > ?
                    """,
                    (now, invitation["id"], now),
                )
                if claimed.rowcount:
                    existing = connection.execute(
                        """
                        SELECT 1 FROM organization_memberships
                        WHERE organization_id = ? AND user_id = ?
                        """,
                        (invitation["organization_id"], user_id),
                    ).fetchone()
                    if existing is None:
                        connection.execute(
                            """
                            INSERT INTO organization_memberships
                                (organization_id, user_id, role, created_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                invitation["organization_id"],
                                user_id,
                                invitation["role"],
                                now,
                            ),
                        )
                    session_id = request.session.get("auth_session_id")
                    if isinstance(session_id, str):
                        connection.execute(
                            "UPDATE user_sessions SET organization_id = ? WHERE id = ?",
                            (invitation["organization_id"], session_id),
                        )
                    request.session["organization_id"] = str(invitation["organization_id"])
                else:
                    redirect_query += "&invite_error=1"
            else:
                redirect_query += "&invite_error=1"
    return RedirectResponse(f"{current_public_url()}/?{redirect_query}", status_code=303)


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> dict[str, bool]:
    session_id = request.session.get("auth_session_id")
    if isinstance(session_id, str):
        with connect_db() as connection:
            connection.execute("DELETE FROM user_sessions WHERE id = ?", (session_id,))
    request.session.clear()
    return {"logged_out": True}


@app.get("/api/organizations")
def list_organizations(user: dict[str, Any] = Depends(require_current_user)) -> list[dict[str, Any]]:
    with connect_db() as connection:
        return connection.execute(
            """
            SELECT o.id, o.name, o.slug, o.[plan], m.role
            FROM organizations o JOIN organization_memberships m ON m.organization_id = o.id
            WHERE m.user_id = ? ORDER BY o.created_at
            """,
            (user["id"],),
        ).fetchall()


@app.post("/api/organizations")
def create_organization(
    payload: OrganizationRequest,
    request: Request,
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, Any]:
    organization_id = str(uuid.uuid4())
    organization_slug = slugify(payload.name)
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO organizations (id, name, slug, [plan], created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                organization_id,
                payload.name,
                organization_slug,
                "free",
                user["id"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO organization_memberships (organization_id, user_id, role, created_at) VALUES (?, ?, ?, ?)",
            (organization_id, user["id"], "owner", datetime.now(timezone.utc).isoformat()),
        )
        session_id = request.session.get("auth_session_id")
        if isinstance(session_id, str):
            connection.execute(
                "UPDATE user_sessions SET organization_id = ? WHERE id = ?",
                (organization_id, session_id),
            )
    request.session["organization_id"] = organization_id
    return {
        "id": organization_id,
        "name": payload.name,
        "slug": organization_slug,
        "role": "owner",
    }


@app.post("/api/organizations/{organization_id}/select")
def select_organization(
    organization_id: str,
    request: Request,
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, str]:
    with connect_db() as connection:
        membership = connection.execute(
            "SELECT 1 FROM organization_memberships WHERE organization_id = ? AND user_id = ?",
            (organization_id, user["id"]),
        ).fetchone()
        if membership is None:
            raise HTTPException(status_code=403, detail="You are not a member of this workspace.")
        session_id = request.session.get("auth_session_id")
        if isinstance(session_id, str):
            connection.execute(
                "UPDATE user_sessions SET organization_id = ? WHERE id = ?",
                (organization_id, session_id),
            )
    request.session["organization_id"] = organization_id
    return {"organization_id": organization_id}


@app.post("/api/organizations/{organization_id}/invites")
def create_organization_invite(
    organization_id: str,
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, Any]:
    with connect_db() as connection:
        membership = connection.execute(
            """
            SELECT role FROM organization_memberships
            WHERE organization_id = ? AND user_id = ?
            """,
            (organization_id, user["id"]),
        ).fetchone()
        if membership is None or membership["role"] not in {"owner", "admin"}:
            raise HTTPException(status_code=403, detail="Workspace administrators can invite members.")
        invite = secrets.token_urlsafe(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        connection.execute(
            """
            INSERT INTO organization_invitations
                (id, organization_id, token_hash, role, created_by, expires_at, used_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                str(uuid.uuid4()),
                organization_id,
                hashlib.sha256(invite.encode()).hexdigest(),
                "student",
                user["id"],
                expires_at,
            ),
        )
    return {"invite_token": invite, "expires_at": expires_at}


@app.post("/api/organizations/join")
def join_organization(
    payload: JoinOrganizationRequest,
    request: Request,
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, str]:
    token_hash = hashlib.sha256(payload.invite_token.encode()).hexdigest()
    with connect_db() as connection:
        invitation = connection.execute(
            """
            SELECT id, organization_id, role, expires_at, used_at
            FROM organization_invitations WHERE token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if (
            invitation is None
            or invitation["used_at"] is not None
            or str(invitation["expires_at"]) <= datetime.now(timezone.utc).isoformat()
        ):
            raise HTTPException(status_code=400, detail="This invitation is invalid, expired, or already used.")
        now = datetime.now(timezone.utc).isoformat()
        claimed = connection.execute(
            """
            UPDATE organization_invitations SET used_at = ?
            WHERE id = ? AND used_at IS NULL AND expires_at > ?
            """,
            (now, invitation["id"], now),
        )
        if not claimed.rowcount:
            raise HTTPException(status_code=400, detail="This invitation is invalid, expired, or already used.")
        membership = connection.execute(
            """
            SELECT 1 FROM organization_memberships
            WHERE organization_id = ? AND user_id = ?
            """,
            (invitation["organization_id"], user["id"]),
        ).fetchone()
        if membership is None:
            connection.execute(
                "INSERT INTO organization_memberships (organization_id, user_id, role, created_at) VALUES (?, ?, ?, ?)",
                (
                    invitation["organization_id"],
                    user["id"],
                    invitation["role"],
                    now,
                ),
            )
        session_id = request.session.get("auth_session_id")
        if isinstance(session_id, str):
            connection.execute(
                "UPDATE user_sessions SET organization_id = ? WHERE id = ?",
                (invitation["organization_id"], session_id),
            )
    return {"organization_id": str(invitation["organization_id"]), "joined": "true"}


def billing_configuration() -> dict[str, Any]:
    return {
        "provider": "Razorpay",
        "configured": bool(
            os.getenv("RAZORPAY_KEY_ID")
            and os.getenv("RAZORPAY_KEY_SECRET")
            and os.getenv("RAZORPAY_PRO_PLAN_ID")
            and os.getenv("RAZORPAY_WEBHOOK_SECRET")
        ),
        "currency": os.getenv("INTERVIEWAI_PRO_CURRENCY", "USD").upper(),
        "amount": int(os.getenv("INTERVIEWAI_PRO_AMOUNT", "900")),
    }


@app.get("/api/billing/plans")
def billing_plans() -> dict[str, Any]:
    configuration = billing_configuration()
    return {
        "student": [
            {"id": "free", "name": "Free", "amount": 0, "currency": configuration["currency"], "interval": "month"},
            {
                "id": "pro",
                "name": "Pro",
                "amount": configuration["amount"],
                "currency": configuration["currency"],
                "interval": "month",
                "configured": configuration["configured"],
            },
        ],
        "company": {"name": "Company", "pricing": "Contact sales", "contact_email": os.getenv("INTERVIEWAI_SALES_EMAIL", "")},
    }


@app.post("/api/billing/checkout")
async def create_billing_checkout(user: dict[str, Any] = Depends(require_current_user)) -> dict[str, str]:
    configuration = billing_configuration()
    if not configuration["configured"]:
        raise HTTPException(
            status_code=503,
            detail="Razorpay billing is not configured. Set API credentials and a matching Pro plan ID.",
        )
    with connect_db() as connection:
        existing_subscriptions = connection.execute(
            """
            SELECT status, checkout_url FROM billing_subscriptions
            WHERE owner_type = 'student' AND owner_id = ?
            ORDER BY updated_at DESC
            """,
            (user["id"],),
        ).fetchall()
    if any(row["status"] in {"active", "authenticated"} for row in existing_subscriptions):
        raise HTTPException(status_code=409, detail="This student account already has an active Pro subscription.")
    pending = next(
        (
            row
            for row in existing_subscriptions
            if row["status"] == "created" and isinstance(row["checkout_url"], str)
        ),
        None,
    )
    if pending is not None:
        return {"checkout_url": str(pending["checkout_url"])}
    credentials = (os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"])
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://api.razorpay.com/v1/subscriptions",
            auth=credentials,
            json={
                "plan_id": os.environ["RAZORPAY_PRO_PLAN_ID"],
                "customer_notify": 1,
                "total_count": 120,
                "notes": {"owner_type": "student", "owner_id": user["id"]},
            },
        )
    if response.is_error:
        logger.error("Razorpay subscription creation failed with HTTP %s", response.status_code)
        raise HTTPException(status_code=502, detail="Razorpay could not create the subscription. Check plan configuration.")
    result = response.json()
    provider_subscription_id = result.get("id")
    checkout_url = result.get("short_url")
    if not isinstance(provider_subscription_id, str) or not isinstance(checkout_url, str):
        raise HTTPException(status_code=502, detail="Razorpay returned an incomplete subscription response.")
    now = datetime.now(timezone.utc).isoformat()
    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO billing_subscriptions
                (id, owner_type, owner_id, provider_subscription_id, plan_id, status,
                 checkout_url, current_period_end, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                "student",
                user["id"],
                provider_subscription_id,
                os.environ["RAZORPAY_PRO_PLAN_ID"],
                str(result.get("status", "created")),
                checkout_url,
                None,
                now,
                now,
            ),
        )
    return {"checkout_url": checkout_url}


@app.post("/api/billing/cancel")
async def cancel_billing_subscription(
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, str]:
    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        raise HTTPException(status_code=503, detail="Razorpay billing is not configured.")
    with connect_db() as connection:
        subscription = connection.execute(
            """
            SELECT provider_subscription_id FROM billing_subscriptions
            WHERE owner_type = 'student' AND owner_id = ? AND status IN ('active', 'authenticated')
            ORDER BY updated_at DESC
            """,
            (user["id"],),
        ).fetchone()
    if subscription is None:
        raise HTTPException(status_code=404, detail="No active Pro subscription was found.")
    subscription_id = str(subscription["provider_subscription_id"])
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.razorpay.com/v1/subscriptions/{subscription_id}/cancel",
            auth=(key_id, key_secret),
            json={"cancel_at_cycle_end": 1},
        )
    if response.is_error:
        logger.error("Razorpay cancellation failed with HTTP %s", response.status_code)
        raise HTTPException(status_code=502, detail="Razorpay could not schedule cancellation. Please retry.")
    return {"status": "cancellation_scheduled"}


@app.post("/api/billing/webhook")
async def razorpay_webhook(request: Request) -> dict[str, bool]:
    webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")
    if not webhook_secret:
        raise HTTPException(status_code=503, detail="Razorpay webhook verification is not configured.")
    body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    expected = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=400, detail="Invalid Razorpay webhook signature.")
    try:
        event = json.loads(body)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Invalid Razorpay webhook payload.") from error
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Invalid Razorpay webhook payload.")
    event_type = event.get("event")
    payload = event.get("payload")
    subscription_payload = payload.get("subscription") if isinstance(payload, dict) else None
    subscription = (
        subscription_payload.get("entity")
        if isinstance(subscription_payload, dict)
        else None
    )
    if not isinstance(subscription, dict):
        raise HTTPException(status_code=400, detail="Webhook payload is missing the subscription details.")
    subscription_id = subscription.get("id")
    if not isinstance(subscription_id, str):
        raise HTTPException(status_code=400, detail="Webhook payload is missing the subscription ID.")
    status_by_event = {
        "subscription.activated": "active",
        "subscription.charged": "active",
        "subscription.authenticated": "authenticated",
        "subscription.halted": "halted",
        "subscription.cancelled": "cancelled",
        "subscription.completed": "completed",
        "subscription.paused": "paused",
        "subscription.resumed": "active",
    }
    mapped_status = status_by_event.get(str(event_type))
    if mapped_status is None:
        return {"received": True}
    period_end = subscription.get("current_end")
    try:
        current_period_end = (
            datetime.fromtimestamp(int(period_end), timezone.utc).isoformat()
            if period_end
            else None
        )
    except (OverflowError, TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Webhook subscription period is invalid.") from error
    with connect_db() as connection:
        updated = connection.execute(
            """
            UPDATE billing_subscriptions SET status = ?, current_period_end = ?, updated_at = ?
            WHERE provider_subscription_id = ?
            """,
            (
                mapped_status,
                current_period_end,
                datetime.now(timezone.utc).isoformat(),
                subscription_id,
            ),
        )
    if updated.rowcount == 0:
        logger.warning("Received Razorpay webhook for an unknown subscription %s", subscription_id)
    return {"received": True}


@app.get("/api/llm/status")
async def llm_status(user: dict[str, Any] = Depends(require_current_user)) -> dict[str, Any]:
    return await llm_service.status()


@app.get("/api/mcp/status")
async def mcp_status(user: dict[str, Any] = Depends(require_current_user)) -> dict[str, Any]:
    global _mcp_status_cache, _mcp_status_cached_at
    async with _mcp_status_lock:
        if (
            _mcp_status_cache is not None
            and time.monotonic() - _mcp_status_cached_at < MCP_STATUS_CACHE_TTL_SECONDS
        ):
            return dict(_mcp_status_cache)
        try:
            async with connect_mcp_server() as client:
                tools = await client.list_tools()
        except (McpError, OSError, RuntimeError) as error:
            raise HTTPException(
                status_code=503,
                detail="The local InterviewAI MCP server is unavailable.",
            ) from error
        _mcp_status_cache = {
            "connected": True,
            "server": "InterviewAI",
            "tools": [
                {"name": tool.name, "description": tool.description}
                for tool in tools.tools
            ],
        }
        _mcp_status_cached_at = time.monotonic()
        return dict(_mcp_status_cache)


@app.get("/api/llm/test")
async def test_llm(user: dict[str, Any] = Depends(require_current_user)) -> dict[str, Any]:
    try:
        return (await llm_service.test_response()).model_dump(mode="json")
    except LLMServiceError as error:
        raise_llm_http_error(error)


@app.post("/api/help/chat")
async def help_chat(
    request: HelpChatRequest,
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, str]:
    history: list[dict[str, str]] = [
        {"role": message.role, "content": message.content}
        for message in request.history
    ]
    try:
        answer = await llm_service.answer_help_question(
            request.message,
            history,
        )
    except LLMServiceError as error:
        raise_llm_http_error(error)
    return {"answer": answer}


@app.post("/api/analyze-documents")
def analyze_documents(
    request: DocumentAnalysisRequest,
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, Any]:
    return compare_resume_to_job(request.resume_text, request.job_description)


@app.get("/api/dashboard")
def dashboard(user: dict[str, Any] = Depends(require_current_user)) -> dict[str, Any]:
    scope, scope_parameters = interview_scope(user, "i")
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT i.*, answer_stats.average_score,
                COALESCE(answer_stats.answer_count, 0) AS answer_count
            FROM interviews i
            LEFT JOIN (
                SELECT interview_id, AVG(score) AS average_score, COUNT(id) AS answer_count
                FROM answers GROUP BY interview_id
            ) AS answer_stats ON answer_stats.interview_id = i.id
            WHERE {scope}
            ORDER BY i.created_at DESC
            """.format(scope=scope),
            scope_parameters,
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
                "question_category": row["question_category"],
                "student_name": row["student_name"],
                "student_identifier": row["student_identifier"],
                "status": row["status"],
                "average_score": round(row["average_score"]) if row["average_score"] is not None else None,
                "created_at": row["created_at"],
            }
            for row in rows[:6]
        ],
    }


@app.get("/api/interviews")
def list_interviews(user: dict[str, Any] = Depends(require_current_user)) -> list[dict[str, Any]]:
    scope, parameters = interview_scope(user)
    with connect_db() as connection:
        rows = connection.execute(
            f"SELECT * FROM interviews WHERE {scope} ORDER BY created_at DESC",
            parameters,
        ).fetchall()
        return [row_to_interview(row, connection) for row in rows]


@app.post("/api/interviews")
async def create_interview(
    request: InterviewRequest,
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, Any]:
    if user["is_personal"]:
        with connect_db() as connection:
            interview_count, _ = free_interview_usage(connection, str(user["id"]))
            pro = connection.execute(
                """
                SELECT 1 FROM billing_subscriptions
                WHERE owner_type = 'student' AND owner_id = ? AND status = 'active'
                """,
                (user["id"],),
            ).fetchone()
        if pro is None and interview_count >= FREE_INTERVIEW_LIMIT:
            raise HTTPException(
                status_code=402,
                detail=(
                    "You’ve used all 3 free interviews in the last 14 days. "
                    "Upgrade to Pro or wait for your oldest interview to leave the 14-day window."
                ),
            )
    interview_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        generated: GeneratedInterview = await llm_service.generate_interview(
            role=request.role,
            experience_level=request.experience_level,
            interview_type=request.interview_type,
            question_category=request.question_category,
            difficulty=request.difficulty,
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
                (id, role, experience_level, interview_type, question_category, difficulty,
                 student_name, student_identifier, tenant_id, user_id, questions, question_details,
                 generation_source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interview_id,
                request.role.strip(),
                request.experience_level,
                request.interview_type,
                request.question_category,
                request.difficulty,
                request.student_name or "Student",
                request.student_identifier,
                user["organization_id"],
                user["id"],
                json.dumps(questions),
                json.dumps(question_details),
                generated.generation_source,
                created_at,
            ),
        )
        row = get_interview_or_404(connection, interview_id)
        return row_to_interview(row, connection)


@app.get("/api/interviews/{interview_id}")
def get_interview(
    interview_id: str,
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, Any]:
    with connect_db() as connection:
        row = get_scoped_interview_or_404(connection, interview_id, user)
        return row_to_interview(row, connection)


@app.delete("/api/interviews/{interview_id}", status_code=204)
def delete_interview(
    interview_id: str,
    user: dict[str, Any] = Depends(require_current_user),
) -> None:
    scope, parameters = interview_scope(user)
    with connect_db() as connection:
        cursor = connection.execute(
            f"DELETE FROM interviews WHERE id = ? AND {scope}",
            (interview_id, *parameters),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Interview not found")


@app.post("/api/interviews/{interview_id}/answers")
async def submit_answer(
    interview_id: str,
    request: AnswerRequest,
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, Any]:
    with connect_db() as connection:
        row = get_scoped_interview_or_404(connection, interview_id, user)
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
            ORDER BY question_index DESC
            """,
            (interview_id, request.question_index),
        ).fetchall()[:4]
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
            question_category=str(row["question_category"]),
            difficulty=str(row["difficulty"]),
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
        answer_values = (
            request.answer,
            evaluation_data["score"],
            json.dumps(evaluation_data),
            datetime.now(timezone.utc).isoformat(),
            interview_id,
            request.question_index,
        )
        updated_answer = connection.execute(
            """
            UPDATE answers SET answer = ?, score = ?, evaluation = ?, created_at = ?
            WHERE interview_id = ? AND question_index = ?
            """,
            answer_values,
        )
        if updated_answer.rowcount == 0:
            connection.execute(
                """
                INSERT INTO answers (answer, score, evaluation, created_at, interview_id, question_index)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                answer_values,
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
async def transcribe_audio(
    file: UploadFile = File(...),
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, str]:
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
    user: dict[str, Any] = Depends(require_current_user),
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
