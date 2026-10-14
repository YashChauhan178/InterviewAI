import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pymupdf
from fastapi.testclient import TestClient

import main
from llm_service import (
    AnswerEvaluation,
    CriterionEvaluation,
    GeneratedInterview,
    LLMServiceError,
    ModelTestResult,
    OllamaService,
    QuestionSpec,
)


def sample_interview() -> GeneratedInterview:
    return GeneratedInterview(
        questions=[
            QuestionSpec(
                question=f"How would you handle a role-specific challenge number {index}?",
                competency="Problem solving",
                evaluation_criteria=["Relevance", "Reasoning", "Specificity"],
                suggested_answer="I would clarify the goal, investigate the constraints, and measure the outcome.",
            )
            for index in range(3)
        ]
    )


def sample_evaluation() -> AnswerEvaluation:
    return AnswerEvaluation(
        score=80,
        criteria=[
            CriterionEvaluation(name="Relevance", score=80, feedback="Directly addresses the question."),
            CriterionEvaluation(name="Completeness", score=70, feedback="Explain the decision a little further."),
            CriterionEvaluation(name="Clarity", score=90, feedback="The answer is easy to follow."),
            CriterionEvaluation(name="Technical accuracy", score=80, feedback="The technical approach is sound."),
        ],
        strengths=["Connects the action to a measured result."],
        improvements=["Explain why you selected that approach."],
        suggested_answer="I would first clarify the objective, then test an approach and report the measured result.",
    )


class InterviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_database_path = main.DATABASE_PATH
        main.DATABASE_PATH = Path(self.temporary_directory.name) / "test.db"

    def tearDown(self) -> None:
        main.DATABASE_PATH = self.original_database_path
        self.temporary_directory.cleanup()

    def test_interview_flow_uses_llm_results_and_persists_feedback(self) -> None:
        with (
            patch.object(
                main.llm_service,
                "generate_interview",
                new_callable=AsyncMock,
                return_value=sample_interview(),
            ) as generate,
            patch.object(
                main.llm_service,
                "evaluate_answer",
                new_callable=AsyncMock,
                return_value=sample_evaluation(),
            ) as evaluate,
            TestClient(main.app) as client,
        ):
            response = client.post(
                "/api/interviews",
                json={
                    "role": "Python developer",
                    "experience_level": "Senior",
                    "interview_type": "Technical",
                    "question_count": 3,
                    "resume_text": "Built Python APIs with FastAPI.",
                    "job_description": "Python, FastAPI, and data systems.",
                },
            )
            self.assertEqual(response.status_code, 200)
            interview = response.json()
            self.assertEqual(len(interview["questions"]), 3)
            self.assertEqual(len(interview["question_details"]), 3)
            self.assertEqual(interview["generation_source"], "ollama")
            self.assertEqual(
                interview["question_details"][0]["suggested_answer"],
                sample_interview().questions[0].suggested_answer,
            )
            generate.assert_awaited_once()
            self.assertEqual(generate.await_args.kwargs["resume_text"], "Built Python APIs with FastAPI.")
            self.assertEqual(generate.await_args.kwargs["job_description"], "Python, FastAPI, and data systems.")

            answer_response = client.post(
                f"/api/interviews/{interview['id']}/answers",
                json={"question_index": 0, "answer": "I investigated the issue and measured the result."},
            )
            self.assertEqual(answer_response.status_code, 200)
            result = answer_response.json()
            self.assertEqual(result["evaluation"]["criteria"][0]["name"], "Relevance")
            self.assertEqual(result["evaluation"]["suggested_answer"], sample_evaluation().suggested_answer)
            self.assertEqual(evaluate.await_args.kwargs["competency"], "Problem solving")
            self.assertEqual(evaluate.await_args.kwargs["criteria"], ["Relevance", "Reasoning", "Specificity"])

            reopened = client.get(f"/api/interviews/{interview['id']}").json()
            self.assertEqual(reopened["answers"][0]["evaluation"]["score"], 80)
            self.assertEqual(reopened["question_details"][0]["competency"], "Problem solving")
            self.assertEqual(client.get("/api/dashboard").json()["total_interviews"], 1)

    def test_answer_submission_cannot_skip_unanswered_questions(self) -> None:
        with (
            patch.object(
                main.llm_service,
                "generate_interview",
                new_callable=AsyncMock,
                return_value=sample_interview(),
            ),
            patch.object(
                main.llm_service,
                "evaluate_answer",
                new_callable=AsyncMock,
                return_value=sample_evaluation(),
            ) as evaluate,
            TestClient(main.app) as client,
        ):
            interview = client.post(
                "/api/interviews",
                json={"role": "Python developer", "question_count": 3},
            ).json()

            response = client.post(
                f"/api/interviews/{interview['id']}/answers",
                json={"question_index": 1, "answer": "I would investigate the issue and measure the result."},
            )

            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                response.json()["detail"],
                "Complete earlier questions before continuing",
            )
            evaluate.assert_not_awaited()
            saved_interview = client.get(f"/api/interviews/{interview['id']}").json()
            self.assertEqual(saved_interview["current_index"], 0)
            self.assertEqual(saved_interview["answers"], [])

    def test_mcp_status_lists_project_tools(self) -> None:
        tool_names = ["analyze_job_requirements", "compare_candidate_to_job"]
        tools = SimpleNamespace(
            tools=[
                SimpleNamespace(name=name, description=f"Description for {name}")
                for name in tool_names
            ]
        )
        mcp_client = SimpleNamespace(list_tools=AsyncMock(return_value=tools))
        connection_count = 0

        @asynccontextmanager
        async def fake_connect_mcp_server():
            nonlocal connection_count
            connection_count += 1
            yield mcp_client

        with (
            patch.object(main, "connect_mcp_server", fake_connect_mcp_server),
            patch.object(main, "_mcp_status_cache", None),
            patch.object(main, "_mcp_status_cached_at", 0.0),
            TestClient(main.app) as client,
        ):
            response = client.get("/api/mcp/status")
            cached_response = client.get("/api/mcp/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(cached_response.status_code, 200)
        status = response.json()
        self.assertTrue(status["connected"])
        self.assertEqual(status["server"], "InterviewAI")
        self.assertEqual(
            {tool["name"] for tool in status["tools"]},
            set(tool_names),
        )
        self.assertEqual(connection_count, 1)

    def test_dashboard_returns_completed_score_progress_and_rubric_averages(self) -> None:
        with (
            patch.object(
                main.llm_service,
                "generate_interview",
                new_callable=AsyncMock,
                return_value=sample_interview(),
            ),
            patch.object(
                main.llm_service,
                "evaluate_answer",
                new_callable=AsyncMock,
                return_value=sample_evaluation(),
            ),
            TestClient(main.app) as client,
        ):
            interview = client.post(
                "/api/interviews",
                json={"role": "Product Designer", "question_count": 3},
            ).json()
            for question_index in range(3):
                response = client.post(
                    f"/api/interviews/{interview['id']}/answers",
                    json={
                        "question_index": question_index,
                        "answer": "I clarified the problem, tested an approach, and measured the result.",
                    },
                )
                self.assertEqual(response.status_code, 200)

            dashboard = client.get("/api/dashboard").json()

        self.assertEqual(dashboard["completed_interviews"], 1)
        self.assertEqual(
            dashboard["score_progress"],
            [
                {
                    "id": interview["id"],
                    "role": "Product Designer",
                    "created_at": interview["created_at"],
                    "score": 80,
                }
            ],
        )
        self.assertEqual(
            dashboard["score_dimensions"],
            [
                {"name": "Relevance", "score": 80, "answer_count": 3},
                {"name": "Completeness", "score": 70, "answer_count": 3},
                {"name": "Clarity", "score": 90, "answer_count": 3},
                {"name": "Technical accuracy", "score": 80, "answer_count": 3},
            ],
        )

    def test_answer_adds_a_follow_up_and_limits_follow_ups_to_one_turn(self) -> None:
        follow_up_evaluation = sample_evaluation().model_copy(
            update={"follow_up_question": "What did you learn from that debugging process?"}
        )
        with (
            patch.object(
                main.llm_service,
                "generate_interview",
                new_callable=AsyncMock,
                return_value=sample_interview(),
            ),
            patch.object(
                main.llm_service,
                "evaluate_answer",
                new_callable=AsyncMock,
                side_effect=[follow_up_evaluation, follow_up_evaluation],
            ) as evaluate,
            TestClient(main.app) as client,
        ):
            interview = client.post(
                "/api/interviews",
                json={"role": "Python developer", "question_count": 3},
            ).json()
            response = client.post(
                f"/api/interviews/{interview['id']}/answers",
                json={"question_index": 0, "answer": "I fixed a bug in my code."},
            )
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertEqual(result["interview"]["current_index"], 1)
            self.assertEqual(result["interview"]["questions"][1], follow_up_evaluation.follow_up_question)
            self.assertTrue(result["interview"]["question_details"][1]["is_follow_up"])

            follow_up_response = client.post(
                f"/api/interviews/{interview['id']}/answers",
                json={"question_index": 1, "answer": "I learned to write a regression test."},
            )
            self.assertEqual(follow_up_response.status_code, 200)
            self.assertIsNone(follow_up_response.json()["evaluation"]["follow_up_question"])
            self.assertFalse(evaluate.await_args_list[1].kwargs["allow_follow_up"])
            self.assertEqual(
                evaluate.await_args_list[1].kwargs["conversation_history"],
                [{"question": interview["questions"][0], "answer": "I fixed a bug in my code."}],
            )

    def test_audio_transcription_endpoint_validates_and_transcribes_without_saving_audio(self) -> None:
        def fake_transcribe(audio_path: Path) -> str:
            self.assertTrue(audio_path.exists())
            self.assertEqual(audio_path.read_bytes(), b"fake webm bytes")
            return "I built a Python API."

        with (
            patch.object(main.audio_transcriber, "transcribe", side_effect=fake_transcribe) as transcribe,
            TestClient(main.app) as client,
        ):
            response = client.post(
                "/api/transcribe",
                files={"file": ("answer.webm", b"fake webm bytes", "audio/webm")},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"text": "I built a Python API."})
            transcribe.assert_called_once()
            self.assertFalse(Path(transcribe.call_args.args[0]).exists())

            unsupported = client.post(
                "/api/transcribe",
                files={"file": ("answer.exe", b"not audio", "application/octet-stream")},
            )
            self.assertEqual(unsupported.status_code, 415)

            empty = client.post(
                "/api/transcribe",
                files={"file": ("answer.wav", b"", "audio/wav")},
            )
            self.assertEqual(empty.status_code, 400)

            oversized = client.post(
                "/api/transcribe",
                files={
                    "file": (
                        "answer.wav",
                        b"x" * (main.MAX_AUDIO_UPLOAD_BYTES + 1),
                        "audio/wav",
                    )
                },
            )
            self.assertEqual(oversized.status_code, 413)
            transcribe.assert_called_once()

    def test_follow_up_preserves_alignment_when_legacy_question_details_are_empty(self) -> None:
        evaluation = sample_evaluation().model_copy(
            update={"follow_up_question": "What did you learn from that experience?"}
        )
        with (
            patch.object(
                main.llm_service,
                "generate_interview",
                new_callable=AsyncMock,
                return_value=sample_interview(),
            ),
            patch.object(
                main.llm_service,
                "evaluate_answer",
                new_callable=AsyncMock,
                return_value=evaluation,
            ),
            TestClient(main.app) as client,
        ):
            interview = client.post(
                "/api/interviews",
                json={"role": "Python developer", "question_count": 3},
            ).json()
            with main.connect_db() as connection:
                connection.execute(
                    "UPDATE interviews SET question_details = '[]' WHERE id = ?",
                    (interview["id"],),
                )
            response = client.post(
                f"/api/interviews/{interview['id']}/answers",
                json={"question_index": 0, "answer": "I worked through a challenging problem."},
            )
            self.assertEqual(response.status_code, 200)
            details = response.json()["interview"]["question_details"]
            self.assertEqual(len(details), 4)
            self.assertTrue(details[1]["is_follow_up"])

    def test_missing_local_model_returns_actionable_error_without_creating_interview(self) -> None:
        with (
            patch.object(
                main.llm_service,
                "generate_interview",
                new_callable=AsyncMock,
                side_effect=LLMServiceError("Ollama is not running.", status_code=503),
            ),
            TestClient(main.app) as client,
        ):
            response = client.post("/api/interviews", json={"role": "Software engineer"})
            self.assertEqual(response.status_code, 503)
            self.assertIn("Ollama is not running", response.json()["detail"])
            self.assertEqual(client.get("/api/interviews").json(), [])

    def test_delete_interview_removes_saved_answers_and_dashboard_metrics(self) -> None:
        with (
            patch.object(
                main.llm_service,
                "generate_interview",
                new_callable=AsyncMock,
                return_value=sample_interview(),
            ),
            TestClient(main.app) as client,
        ):
            interview = client.post(
                "/api/interviews",
                json={"role": "Python developer", "question_count": 3},
            ).json()
            with main.connect_db() as connection:
                connection.execute(
                    """
                    INSERT INTO answers
                        (interview_id, question_index, answer, score, evaluation, created_at)
                    VALUES (?, 0, ?, 80, ?, ?)
                    """,
                    (
                        interview["id"],
                        "I improved the API response time.",
                        json.dumps(sample_evaluation().model_dump(mode="json")),
                        "2026-01-01T00:00:00+00:00",
                    ),
                )

            deleted = client.delete(f"/api/interviews/{interview['id']}")
            self.assertEqual(deleted.status_code, 204)
            self.assertEqual(client.get("/api/interviews").json(), [])
            self.assertEqual(client.get("/api/interviews/" + interview["id"]).status_code, 404)
            self.assertEqual(client.get("/api/dashboard").json()["total_interviews"], 0)
            with main.connect_db() as connection:
                answer_count = connection.execute(
                    "SELECT COUNT(*) FROM answers WHERE interview_id = ?",
                    (interview["id"],),
                ).fetchone()[0]
            self.assertEqual(answer_count, 0)

    def test_help_chat_uses_local_model_and_bounds_history(self) -> None:
        history = [
            {"role": "user", "content": "How do I create a practice session?"},
            {"role": "assistant", "content": "Choose Start an interview from the overview."},
        ]
        with (
            patch.object(
                main.llm_service,
                "answer_help_question",
                new_callable=AsyncMock,
                return_value="Choose My interviews to review saved sessions.",
            ) as answer_help,
            TestClient(main.app) as client,
        ):
            response = client.post(
                "/api/help/chat",
                json={"message": "Where can I see my history?", "history": history},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["answer"],
                "Choose My interviews to review saved sessions.",
            )
            answer_help.assert_awaited_once_with(
                "Where can I see my history?",
                history,
            )
            invalid_message = client.post(
                "/api/help/chat",
                json={"message": "   "},
            )
            self.assertEqual(invalid_message.status_code, 422)
            excessive_history = client.post(
                "/api/help/chat",
                json={
                    "message": "Help",
                    "history": [{"role": "user", "content": "Help"}] * 9,
                },
            )
            self.assertEqual(excessive_history.status_code, 422)

    def test_fallback_generation_source_is_saved_and_visible(self) -> None:
        fallback = GeneratedInterview(
            questions=sample_interview().questions,
            generation_source="fallback",
        )
        with (
            patch.object(
                main.llm_service,
                "generate_interview",
                new_callable=AsyncMock,
                return_value=fallback,
            ),
            TestClient(main.app) as client,
        ):
            response = client.post("/api/interviews", json={"role": "Software Engineer", "question_count": 3})
            self.assertEqual(response.status_code, 200)
            interview_id = response.json()["id"]
            self.assertEqual(response.json()["generation_source"], "fallback")
            reopened = client.get(f"/api/interviews/{interview_id}")
            self.assertEqual(reopened.json()["generation_source"], "fallback")

    def test_legacy_database_is_migrated_with_empty_question_details(self) -> None:
        with main.connect_db() as connection:
            connection.execute(
                """
                CREATE TABLE interviews (
                    id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    experience_level TEXT NOT NULL,
                    interview_type TEXT NOT NULL,
                    questions TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'in_progress',
                    current_index INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE answers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
                    question_index INTEGER NOT NULL,
                    answer TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    evaluation TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(interview_id, question_index)
                )
                """
            )
        main.initialize_database()
        with main.connect_db() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(interviews)")}
        self.assertIn("question_details", columns)
        self.assertIn("generation_source", columns)

    def test_document_upload_parses_pdf_and_rejects_invalid_uploads(self) -> None:
        with TestClient(main.app) as client:
            invalid_role = client.post("/api/interviews", json={"role": "  "})
            self.assertEqual(invalid_role.status_code, 422)

            parsed = client.post(
                "/api/parse-document",
                data={"kind": "resume"},
                files={"file": ("resume.txt", b"Python engineer with API experience.", "text/plain")},
            )
            self.assertEqual(parsed.status_code, 200)
            self.assertEqual(parsed.json()["text"], "Python engineer with API experience.")

            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "Python and FastAPI project experience.")
            pdf_bytes = document.tobytes()
            document.close()
            parsed_pdf = client.post(
                "/api/parse-document",
                data={"kind": "resume"},
                files={"file": ("resume.pdf", pdf_bytes, "application/pdf")},
            )
            self.assertEqual(parsed_pdf.status_code, 200)
            self.assertIn("Python and FastAPI", parsed_pdf.json()["text"])
            self.assertIn("Python", parsed_pdf.json()["skills"])

            resume = (
                "Alex Morgan\nalex.morgan@example.com\n+1 (555) 123-4567\n"
                "Skills\nPython, FastAPI\nProjects\nBuilt a REST API."
            )
            parsed_resume = client.post(
                "/api/parse-document",
                data={"kind": "resume"},
                files={"file": ("resume.txt", resume.encode(), "text/plain")},
            )
            self.assertEqual(parsed_resume.status_code, 200)
            self.assertEqual(parsed_resume.json()["sections"]["skills"], "Python, FastAPI")
            self.assertEqual(parsed_resume.json()["contact_details"]["email"], "alex.morgan@example.com")
            self.assertEqual(parsed_resume.json()["contact_details"]["phone"], "+1 (555) 123-4567")
            parsed_job = client.post(
                "/api/parse-document",
                data={"kind": "job_description"},
                files={
                    "file": (
                        "job.txt",
                        b"Develop Python APIs with FastAPI and maintain PostgreSQL services.",
                        "text/plain",
                    )
                },
            )
            self.assertEqual(parsed_job.status_code, 200)
            self.assertIn("FastAPI", parsed_job.json()["analysis"]["required_skills"])

            invalid_pdf = client.post(
                "/api/parse-document",
                data={"kind": "resume"},
                files={"file": ("broken.pdf", b"not a PDF", "application/pdf")},
            )
            self.assertEqual(invalid_pdf.status_code, 400)

            unsupported = client.post(
                "/api/parse-document",
                data={"kind": "resume"},
                files={"file": ("resume.exe", b"not a resume", "application/octet-stream")},
            )
            self.assertEqual(unsupported.status_code, 415)

            scanned_pdf = pymupdf.open()
            scanned_pdf.new_page()
            scanned_pdf_bytes = scanned_pdf.tobytes()
            scanned_pdf.close()
            scanned = client.post(
                "/api/parse-document",
                data={"kind": "resume"},
                files={"file": ("scanned.pdf", scanned_pdf_bytes, "application/pdf")},
            )
            self.assertEqual(scanned.status_code, 422)
            self.assertIn("Scanned PDFs", scanned.json()["detail"])
            oversized = client.post(
                "/api/parse-document",
                data={"kind": "resume"},
                files={"file": ("large.txt", b"x" * (main.MAX_UPLOAD_BYTES + 1), "text/plain")},
            )
            self.assertEqual(oversized.status_code, 413)

    def test_document_analysis_compares_software_and_finance_requirements(self) -> None:
        with TestClient(main.app) as client:
            software = client.post(
                "/api/analyze-documents",
                json={
                    "resume_text": "Skills\nPython, FastAPI, PostgreSQL\nProjects\nBuilt a REST API.",
                    "job_description": (
                        "Develop Python APIs with FastAPI and Docker. "
                        "Write automated tests and maintain PostgreSQL services."
                    ),
                },
            )
            self.assertEqual(software.status_code, 200)
            software_result = software.json()
            self.assertIn("Python", software_result["matched_skills"])
            self.assertIn("FastAPI", software_result["matched_skills"])
            self.assertIn("Docker", software_result["missing_skills"])
            self.assertTrue(software_result["responsibilities"])

            finance = client.post(
                "/api/analyze-documents",
                json={
                    "resume_text": "Skills\nExcel, financial modeling, valuation",
                    "job_description": (
                        "Perform financial analysis and valuation. "
                        "Prepare forecasts and manage risk for an investment portfolio."
                    ),
                },
            )
            self.assertEqual(finance.status_code, 200)
            finance_result = finance.json()
            self.assertIn("valuation", [skill.casefold() for skill in finance_result["matched_skills"]])
            self.assertIn("financial analysis", [skill.casefold() for skill in finance_result["missing_skills"]])

    def test_model_test_endpoint_reports_local_model_response(self) -> None:
        with (
            patch.object(
                main.llm_service,
                "test_response",
                new_callable=AsyncMock,
                return_value=ModelTestResult(
                    ok=True,
                    model="llama3.2:3b",
                    message="Ollama responded successfully.",
                ),
            ),
            TestClient(main.app) as client,
        ):
            response = client.get("/api/llm/test")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["model"], "llama3.2:3b")
            self.assertTrue(response.json()["ok"])


class OllamaServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_tools_are_passed_to_ollama_and_can_be_called_by_it(self) -> None:
        requests: list[dict[str, Any]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(
                    200,
                    json={
                        "models": [
                            {"name": "llama3.2:3b", "capabilities": ["completion", "tools"]}
                        ]
                    },
                )
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                return httpx.Response(
                    200,
                    json={
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "analyze_job_requirements",
                                        "arguments": {
                                            "job_description": (
                                                "Build Python services with FastAPI and SQL."
                                            )
                                        },
                                    }
                                }
                            ],
                        }
                    },
                )
            return httpx.Response(
                200,
                json={"message": {"content": sample_interview().model_dump_json()}},
            )

        service = OllamaService(
            base_url="http://127.0.0.1",
            transport=httpx.MockTransport(respond),
        )
        result = await service.generate_interview(
            role="Backend engineer",
            experience_level="Mid-level",
            interview_type="Technical",
            question_count=3,
            resume_text="",
            job_description="Build Python services with FastAPI and SQL.",
        )

        self.assertEqual(len(result.questions), 3)
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            {
                tool["function"]["name"]
                for tool in requests[0]["tools"]
            },
            {"analyze_job_requirements", "compare_candidate_to_job"},
        )
        self.assertIn(
            "Additional analysis from local InterviewAI MCP tools",
            requests[0]["messages"][1]["content"],
        )
        self.assertEqual(requests[0]["options"]["num_predict"], 640)
        tool_message = next(
            message for message in requests[1]["messages"] if message["role"] == "tool"
        )
        self.assertEqual(tool_message["tool_name"], "analyze_job_requirements")
        self.assertIn("FastAPI", tool_message["content"])

    async def test_structured_interview_evaluation_and_model_status(self) -> None:
        recorded_requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            recorded_requests.append(request)
            if request.url.path == "/api/tags":
                return httpx.Response(
                    200,
                    json={
                        "models": [
                            {"name": "llama3.2:3b", "capabilities": ["completion", "tools"]}
                        ]
                    },
                )
            payload = json.loads(request.content)
            if "Evaluate the candidate answer" in payload["messages"][1]["content"]:
                evaluation = sample_evaluation()
                content = evaluation.model_copy(
                    update={
                        "score": 1,
                        "criteria": list(reversed(evaluation.criteria)),
                    }
                ).model_dump_json()
            else:
                content = sample_interview().model_dump_json()
            return httpx.Response(200, json={"message": {"content": content}})

        service = OllamaService(
            base_url="http://127.0.0.1",
            model="llama3.2:3b",
            transport=httpx.MockTransport(respond),
        )
        status = await service.status()
        self.assertTrue(status["available"])
        self.assertTrue(status["model_available"])
        self.assertTrue(status["supports_tools"])

        generated = await service.generate_interview(
            role="Product designer",
            experience_level="Senior",
            interview_type="Mixed",
            question_count=3,
            resume_text="Designed onboarding for a mobile app.",
            job_description="Lead product discovery and usability research.",
        )
        self.assertEqual(len(generated.questions), 3)

        evaluation = await service.evaluate_answer(
            role="Product designer",
            experience_level="Senior",
            interview_type="Mixed",
            question=generated.questions[0].question,
            competency=generated.questions[0].competency,
            criteria=generated.questions[0].evaluation_criteria,
            sample_answer=generated.questions[0].suggested_answer,
            answer="I tested three prototypes and improved task completion.",
            conversation_history=[
                {"question": "How do you approach research?", "answer": "I interview users."}
            ],
        )
        self.assertEqual(evaluation.score, 80)
        self.assertEqual(
            [criterion.name for criterion in evaluation.criteria],
            [
                "Relevance",
                "Completeness",
                "Clarity",
                "Technical accuracy",
            ],
        )
        self.assertEqual(len(recorded_requests), 3)
        self.assertEqual(
            json.loads(recorded_requests[1].content)["model"],
            "llama3.2:3b",
        )
        self.assertIn("I interview users", recorded_requests[2].content.decode())
        self.assertEqual(
            json.loads(recorded_requests[1].content)["options"]["num_predict"],
            640,
        )
        self.assertEqual(
            json.loads(recorded_requests[2].content)["options"]["num_predict"],
            700,
        )

    async def test_follow_up_is_discarded_when_it_repeats_an_existing_question(self) -> None:
        evaluation = sample_evaluation().model_copy(
            update={"follow_up_question": "Can you describe your approach to this challenge?"}
        )
        service = OllamaService(
            base_url="http://127.0.0.1",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={"message": {"content": evaluation.model_dump_json()}},
                )
            ),
        )
        result = await service.evaluate_answer(
            role="Software Engineer",
            experience_level="Mid-level",
            interview_type="Technical",
            question="How do you approach this challenge?",
            competency="Problem solving",
            criteria=["Relevance", "Reasoning", "Specificity"],
            sample_answer=sample_interview().questions[0].suggested_answer,
            answer="I break the problem into steps.",
            previous_questions=["How do you approach this challenge?"],
        )
        self.assertIsNone(result.follow_up_question)

    async def test_incomplete_answer_generates_a_contextual_follow_up(self) -> None:
        evaluation = sample_evaluation().model_copy(
            update={
                "criteria": [
                    criterion.model_copy(update={"score": 40})
                    if criterion.name == "Completeness"
                    else criterion
                    for criterion in sample_evaluation().criteria
                ],
                "follow_up_question": None,
            }
        )
        prompts: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            prompt = json.loads(request.content)["messages"][1]["content"]
            prompts.append(prompt)
            response = (
                {"question": "Which database would you select, and what trade-off matters most?"}
                if "Write exactly one concise interview follow-up" in prompt
                else json.loads(evaluation.model_dump_json())
            )
            return httpx.Response(
                200,
                json={"message": {"content": json.dumps(response)}},
            )

        service = OllamaService(
            base_url="http://127.0.0.1",
            transport=httpx.MockTransport(respond),
        )
        result = await service.evaluate_answer(
            role="Backend engineer",
            experience_level="Mid-level",
            interview_type="Technical",
            question="How would you choose a database for a high-traffic app?",
            competency="Database trade-offs",
            criteria=["Explain trade-offs", "Use an example", "Discuss the outcome"],
            sample_answer=sample_interview().questions[0].suggested_answer,
            answer="Both databases can work.",
            conversation_history=[
                {"question": "What is the workload?", "answer": "A high-traffic API."}
            ],
            previous_questions=["What is the workload?"],
        )
        self.assertEqual(
            result.follow_up_question,
            "Which database would you select, and what trade-off matters most?",
        )
        self.assertEqual(len(prompts), 2)
        self.assertIn("A high-traffic API", prompts[1])

    async def test_ten_point_model_scores_are_normalized_to_the_ui_scale(self) -> None:
        evaluation = sample_evaluation().model_copy(
            update={
                "score": 8,
                "criteria": [
                    criterion.model_copy(update={"score": 8})
                    for criterion in sample_evaluation().criteria
                ],
            }
        )
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"message": {"content": evaluation.model_dump_json()}},
            )

        service = OllamaService(
            base_url="http://127.0.0.1",
            transport=httpx.MockTransport(respond),
        )
        result = await service.evaluate_answer(
            role="Backend engineer",
            experience_level="Mid-level",
            interview_type="Technical",
            question="How do you choose a database?",
            competency="Technical judgment",
            criteria=["Reasoning", "Trade-offs", "Example"],
            sample_answer=sample_interview().questions[0].suggested_answer,
            answer="I compare the data and workload.",
        )
        self.assertEqual(result.score, 80)
        self.assertTrue(all(item.score == 80 for item in result.criteria))
        self.assertEqual(len(requests), 1)

    async def test_evaluation_request_defines_a_calibrated_percentage_scale(self) -> None:
        requests: list[dict[str, object]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"message": {"content": sample_evaluation().model_dump_json()}},
            )

        service = OllamaService(
            base_url="http://127.0.0.1",
            transport=httpx.MockTransport(respond),
        )
        await service.evaluate_answer(
            role="Backend engineer",
            experience_level="Mid-level",
            interview_type="Technical",
            question="How would you validate an API request?",
            competency="API design",
            criteria=["Validation", "Error handling", "Testing"],
            sample_answer=sample_interview().questions[0].suggested_answer,
            answer="I would validate request data with Pydantic and test invalid inputs.",
        )

        self.assertEqual(len(requests), 1)
        request = requests[0]
        schema = request["format"]
        properties = schema["properties"]
        self.assertEqual(properties["score"]["minimum"], 0)
        self.assertEqual(properties["score"]["maximum"], 100)
        criterion_score = properties["criteria"]["items"]["properties"]["score"]
        self.assertEqual(criterion_score["minimum"], 0)
        self.assertEqual(criterion_score["maximum"], 100)
        prompt = request["messages"][1]["content"]
        self.assertIn("Do not use 0 for an answer that contains relevant content", prompt)
        self.assertIn("80-89 means strong", prompt)

    def test_ollama_service_rejects_non_local_endpoints(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            OllamaService(base_url="https://api.example.com")

    async def test_invalid_model_questions_fall_back_to_valid_role_questions(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(
                    200,
                    json={"models": [{"name": "llama3.2:3b", "capabilities": []}]},
                )
            return httpx.Response(
                200,
                json={"message": {"content": '{"questions":[{"question":"Too short"}]}' }},
            )

        service = OllamaService(
            base_url="http://127.0.0.1",
            transport=httpx.MockTransport(respond),
        )
        interview = await service.generate_interview(
            role="Junior Software Engineer",
            experience_level="Entry-level",
            interview_type="Mixed",
            question_count=3,
            resume_text="Python and FastAPI project experience.",
            job_description="Build Python services and write tests.",
        )
        self.assertEqual(interview.generation_source, "fallback")
        self.assertEqual(len(interview.questions), 3)
        self.assertEqual(len({question.question for question in interview.questions}), 3)
        self.assertTrue(all("Junior Software Engineer" in question.question for question in interview.questions))

    async def test_model_request_omits_resume_contact_details(self) -> None:
        recorded_prompts: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(
                    200,
                    json={"models": [{"name": "llama3.2:3b", "capabilities": []}]},
                )
            body = json.loads(request.content)
            recorded_prompts.append(body["messages"][1]["content"])
            return httpx.Response(
                200,
                json={"message": {"content": sample_interview().model_dump_json()}},
            )

        service = OllamaService(
            base_url="http://127.0.0.1",
            transport=httpx.MockTransport(respond),
        )
        await service.generate_interview(
            role="Backend engineer",
            experience_level="Mid-level",
            interview_type="Technical",
            question_count=3,
            resume_text=(
                "Alex Morgan\nalex.morgan@example.com\n+1 (555) 123-4567\n"
                "Skills\nPython, FastAPI\nProjects\nBuilt a REST API."
            ),
            job_description="Develop backend APIs with Python and FastAPI.",
        )
        self.assertEqual(len(recorded_prompts), 1)
        self.assertNotIn("Alex Morgan", recorded_prompts[0])
        self.assertNotIn("alex.morgan@example.com", recorded_prompts[0])
        self.assertNotIn("+1 (555) 123-4567", recorded_prompts[0])
        self.assertIn("Python", recorded_prompts[0])

    async def test_connectivity_test_calls_and_validates_ollama(self) -> None:
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": json.dumps(
                            {
                                "ok": True,
                                "model": "llama3.2:3b",
                                "message": "Ollama responded successfully.",
                            }
                        )
                    }
                },
            )

        service = OllamaService(
            base_url="http://127.0.0.1",
            model="llama3.2:3b",
            transport=httpx.MockTransport(respond),
        )
        result = await service.test_response()
        self.assertTrue(result.ok)
        self.assertEqual(result.model, "llama3.2:3b")
        self.assertEqual(requests[0].url.path, "/api/chat")

    async def test_help_chat_sends_bounded_context_to_local_ollama(self) -> None:
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"message": {"content": json.dumps({"answer": "Use the STAR method."})}},
            )

        service = OllamaService(
            base_url="http://127.0.0.1",
            model="llama3.2:3b",
            transport=httpx.MockTransport(respond),
        )
        answer = await service.answer_help_question(
            "How can I make an answer clearer?",
            [{"role": "user", "content": "I have an interview tomorrow."}],
        )

        self.assertEqual(answer, "Use the STAR method.")
        self.assertEqual(len(requests), 1)
        payload = json.loads(requests[0].content)
        self.assertEqual(requests[0].url.path, "/api/chat")
        self.assertFalse(payload.get("tools"))
        self.assertEqual(payload["options"]["num_predict"], 400)
        self.assertIn("I have an interview tomorrow.", payload["messages"][1]["content"])
        self.assertIn("How can I make an answer clearer?", payload["messages"][1]["content"])

    async def test_ollama_timeout_returns_actionable_gateway_timeout(self) -> None:
        def timeout(_: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("local model timed out")

        service = OllamaService(
            base_url="http://127.0.0.1",
            transport=httpx.MockTransport(timeout),
        )
        with self.assertRaises(LLMServiceError) as raised:
            await service.test_response()
        self.assertEqual(raised.exception.status_code, 504)
        self.assertIn("too long to respond", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
