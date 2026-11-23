"""Local-first FastAPI application, preserving v4 course and timer contracts."""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from scripts.lib import learning_group, lesson_timer, task_queue
from scripts.initialize import initialize as initialize_course
from scripts.lib.course_state import load_state, validate_state
from scripts.lib.repository import course_dir, iter_course_paths, validate_course_name

from platform_api.agentscope_runtime import AgentUnavailable, ask_agent, generate_tutor_turn, runtime_status as agent_runtime_status
from platform_api import course_agents, course_graph, course_lifecycle, course_harness, snapshot_sync
from platform_api.evidence import EvidenceError, retrieve, is_current
from platform_api.grounding import GroundingError, check_response, grounded_prompt
from platform_api import exercises, books, chat_history, prep_editor, study_plan, flashcards, ontology, prep_sections
from scripts.lib import chapter_prep
from scripts.lib.prep import prep_status, check_prep, prep_path

api = FastAPI(title="Socratopia API", version="5.0.0-alpha.8", docs_url=None, redoc_url=None)


def root_dir() -> Path:
    return Path(os.environ.get("SOCRATOPIA_ROOT", ".")).resolve()


def require_auth(authorization: Annotated[str | None, Header()] = None) -> None:
    token = os.environ.get("SOCRATOPIA_API_TOKEN", "")
    provided = authorization.removeprefix("Bearer ") if authorization else ""
    if not token:
        raise HTTPException(503, "API token is not configured")
    if not secrets.compare_digest(token, provided):
        raise HTTPException(401, "Unauthorized")


Authenticated = Annotated[None, Depends(require_auth)]


def existing_course(course: str) -> str:
    try:
        name = validate_course_name(course)
        if not course_dir(root_dir(), name).is_dir():
            raise HTTPException(404, "Course does not exist")
        return name
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class CourseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    tutor: Literal["D", "E", "F"] = "F"


class ChapterPrepRequest(BaseModel):
    chapter: str = Field(min_length=1, max_length=160)
    sources: list[str] = Field(default_factory=list, max_length=16)
    apply: bool = False


class DraftSaveRequest(BaseModel):
    body: str = Field(min_length=1, max_length=262144)
    expected_sha256: str = Field(min_length=64, max_length=64)


class StructuredPrepSave(BaseModel):
    expected_sha256: str = Field(min_length=64, max_length=64)
    updates: dict[str, str] = Field(min_length=1, max_length=3)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class AgentFeatureRequest(BaseModel):
    feature: Literal["memory", "group", "graph"]
    enabled: bool



class BookImport(BaseModel):
    content: str = Field(min_length=1, max_length=2000000)


class ExerciseCreate(BaseModel):
    kind: Literal["single_select", "multi_select"]
    stem: str = Field(min_length=1, max_length=500)
    options: list[str] = Field(min_length=2, max_length=4)
    correct_indices: list[int] = Field(min_length=1, max_length=3)
    explanation: str = Field(min_length=1, max_length=1000)
    source_quote: str = Field(min_length=8, max_length=500)
    lesson_id: str | None = None


class ExerciseSubmission(BaseModel):
    request_id: str = Field(min_length=32, max_length=32)
    selected_indices: list[int] = Field(min_length=1, max_length=4)


class FlashcardCreate(BaseModel):
    front: str = Field(min_length=3, max_length=240)
    back: str = Field(min_length=1, max_length=800)
    source_quote: str = Field(min_length=8, max_length=500)
    concept_id: str | None = None
    exercise_id: str | None = None


class FlashcardReview(BaseModel):
    rating: Literal["again", "hard", "good", "easy"]
    reveal_token: str = Field(min_length=32, max_length=32)
    request_id: str = Field(min_length=32, max_length=32)


class PrerequisiteCreate(BaseModel):
    prerequisite_id: str = Field(min_length=12, max_length=12)
    concept_id: str = Field(min_length=12, max_length=12)
    source_quote: str = Field(min_length=8, max_length=500)


class ConceptQuestionLink(BaseModel):
    question_id: str = Field(min_length=32, max_length=32)
    concept_id: str = Field(min_length=12, max_length=12)
    role: Literal["practice", "transfer"]
    source_quote: str = Field(min_length=8, max_length=500)


class TimerStart(BaseModel):
    lesson_id: str


class TimerTransition(BaseModel):
    action: Literal["heartbeat", "pause", "resume", "finish", "interrupt"]


class StudyTaskCreate(BaseModel):
    title: str = Field(min_length=2, max_length=160)
    kind: Literal["study", "review", "visualize", "research"]
    request_id: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


@api.get("/api/courses/{course}/tasks")
def course_tasks(course: str, _: Authenticated) -> dict:
    name = existing_course(course)
    # This queue is a TODO register only. It does not autonomously execute code or LLMs.
    from scripts.lib.repository import read_json
    data = read_json(task_queue.queue_path(root_dir(), name)) or {"schema_version": 1, "tasks": []}
    return {"course": name, "tasks": data.get("tasks", [])[-100:]}


@api.post("/api/courses/{course}/tasks")
def course_task_add(course: str, payload: StudyTaskCreate, _: Authenticated) -> dict:
    name = active_course(course)
    try:
        return task_queue.enqueue_task(root_dir(), name, payload.kind,
                                       payload.request_id, payload={"title": payload.title})
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


class GroupConfig(BaseModel):
    members: list[Literal["D", "E", "F"]] = Field(min_length=2, max_length=3)
    lead: Literal["D", "E", "F"]


class GroupStart(BaseModel):
    topic: str = Field(min_length=1, max_length=240)
    rounds: int = Field(default=2, ge=1, le=3)


class GroupContinue(BaseModel):
    question: str = Field(default="", max_length=240)


@api.get("/api/health")
def health() -> dict:
    return {"service": "socratopia", "version": api.version, "ready": bool(os.getenv("SOCRATOPIA_API_TOKEN"))}


@api.get("/api/runtime")
async def runtime_information(response: Response, _: Authenticated) -> dict:
    response.headers["Cache-Control"] = "no-store"
    state = agent_runtime_status()
    return {**state, "tutors": state, "course_agent_isolation": True,
            "model_credentials_verified": False}


@api.get("/api/courses")
def courses(_: Authenticated) -> dict:
    names = []
    for candidate in iter_course_paths(root_dir()):
        try:
            name = validate_course_name(candidate.name)
            if not candidate.is_symlink():
                names.append(name)
        except ValueError:
            continue
    return {"courses": names}


@api.post("/api/courses")
def create_course(payload: CourseCreate, _: Authenticated) -> dict:
    try:
        return initialize_course(root_dir(), payload.name, payload.tutor)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/courses/{course}/agent")
def course_agent_status(course: str, _: Authenticated) -> dict:
    try:
        return course_agents.status(root_dir(), existing_course(course))
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/api/courses/{course}/agent/features")
def course_agent_features(course: str, payload: AgentFeatureRequest, _: Authenticated) -> dict:
    try:
        return course_agents.configure(root_dir(), active_course(course),
                                       payload.feature, payload.enabled)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/courses/{course}/ontology")
def course_ontology(course: str, response: Response, _: Authenticated) -> dict:
    response.headers["Cache-Control"] = "no-store"
    try:
        return ontology.graph(root_dir(), existing_course(course))
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/api/courses/{course}/ontology/prerequisites")
def add_course_prerequisite(course: str, payload: PrerequisiteCreate,
                            _: Authenticated) -> dict:
    name = active_course(course)
    try:
        return ontology.add_prerequisite(root_dir(), name,
            payload.prerequisite_id, payload.concept_id, payload.source_quote)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/api/courses/{course}/ontology/questions")
def link_course_question(course: str, payload: ConceptQuestionLink,
                         _: Authenticated) -> dict:
    name = active_course(course)
    try:
        return ontology.link_question(root_dir(), name,
            payload.question_id, payload.concept_id, payload.role, payload.source_quote)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/courses/{course}/diagnosis")
def course_concept_diagnosis(course: str, response: Response, _: Authenticated) -> dict:
    response.headers["Cache-Control"] = "no-store"
    try:
        return ontology.diagnosis(root_dir(), existing_course(course))
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/courses/{course}/graph")
def read_knowledge_graph(course: str, _: Authenticated) -> dict:
    try:
        return course_graph.build(root_dir(), existing_course(course))
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/courses/{course}/models")
def course_models(course: str, _: Authenticated) -> dict:
    return course_harness.routes(root_dir(), existing_course(course))


@api.post("/api/courses/{course}/models")
def assign_course_model(course: str, payload: ModelRoleRequest, _: Authenticated) -> dict:
    name = active_course(course)
    try:
        return course_harness.assign(root_dir(), name, payload.role, payload.model)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/courses/{course}/lifecycle")
def read_lifecycle(course: str, _: Authenticated) -> dict:
    return course_lifecycle.status(root_dir(), existing_course(course))


class ArchiveRequest(BaseModel):
    archived: bool


class SnapshotUploadRequest(BaseModel):
    backend: Literal["webdav", "s3"]
    acknowledged_private_data: Literal[True]


@api.get("/api/courses/{course}/sync")
def sync_capabilities(course: str, _: Authenticated) -> dict:
    name = existing_course(course)
    return {"course": name, "webdav_configured": bool(
                os.getenv("SOCRATOPIA_WEBDAV_URL") and os.getenv("SOCRATOPIA_WEBDAV_USERNAME")
                and os.getenv("SOCRATOPIA_WEBDAV_PASSWORD")),
            "s3_configured": bool(os.getenv("SOCRATOPIA_S3_BUCKET")),
            "automatic_restore": False, "automatic_upload": False,
            "contains_personal_data": True, "requires_explicit_consent": True}


@api.post("/api/courses/{course}/sync/upload")
def sync_upload(course: str, payload: SnapshotUploadRequest, _: Authenticated) -> dict:
    name = existing_course(course)
    try:
        # Never accept a remote URL, credentials, object key or paths from an AI or browser.
        return snapshot_sync.upload(root_dir(), name, payload.backend)
    except (snapshot_sync.SyncError, OSError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


class ModelRoleRequest(BaseModel):
    role: Literal["teach", "tutor", "explain_visual", "review"]
    model: str = Field(min_length=1, max_length=120)


@api.post("/api/courses/{course}/lifecycle")
def update_lifecycle(course: str, payload: ArchiveRequest, _: Authenticated) -> dict:
    return course_lifecycle.set_archive(root_dir(), existing_course(course), payload.archived)


def active_course(course: str) -> str:
    name = existing_course(course)
    try:
        course_lifecycle.require_active(root_dir(), name)
    except course_lifecycle.ArchivedCourse as exc:
        raise HTTPException(409, str(exc)) from exc
    return name


@api.get("/api/courses/{course}/state")
def course_state(course: str, _: Authenticated) -> dict:
    state = load_state(root_dir(), existing_course(course))
    errors = validate_state(state)
    if errors:
        raise HTTPException(409, "Invalid course state: " + "; ".join(errors))
    return state



@api.get("/api/courses/{course}/evidence")
def course_evidence(course: str, q: str, _: Authenticated) -> dict:
    name = existing_course(course)
    if not 1 <= len(q.strip()) <= 2000:
        raise HTTPException(422, "Query must have 1–2000 characters")
    try:
        return retrieve(root_dir(), name, q).for_client()
    except EvidenceError as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/courses/{course}/overview")
def overview(course: str, _: Authenticated) -> dict:
    name = existing_course(course)
    state = load_state(root_dir(), name)
    errors = validate_state(state)
    timer = lesson_timer.status(root_dir(), name)
    group = learning_group.status(root_dir(), name)
    book_present = False
    try:
        # The source must be readable, not merely present on disk.
        from platform_api.evidence import _read_book
        _, _ = _read_book(root_dir(), name)
        book_present = True
    except EvidenceError:
        pass
    return {
        "course": name, "state_valid": not errors, "course_phase": state.get("phase"),
        "readiness": state.get("readiness"), "blockers": list(state.get("blockers", [])) + errors,
        "book_available": book_present, "grounded_chat_ready": book_present,
        "formal_lesson_ready": bool(
            not errors and not state.get("blockers")
            and all(state.get("readiness", {}).get(k) == "ok" for k in ("catalog", "prep", "runtime"))
            and state.get("readiness", {}).get("reteach") == "clear"
            and state.get("phase") in {"ready", "in_lesson"}
        ),
        "timer_phase": timer["phase"],
        "group_phase": (group.get("session") or {}).get("phase", "not_started"),
        "mastery_automatically_updated": False,
    }



@api.get("/api/courses/{course}/prep")
def course_prep_status(course: str, _: Authenticated) -> dict:
    name = existing_course(course)
    try:
        return {"course": name, "lessons": prep_status(root_dir(), name)}
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/courses/{course}/prep/{lesson_id}")
def course_prep_check(course: str, lesson_id: str, _: Authenticated) -> dict:
    name = existing_course(course)
    try:
        from scripts.lib.repository import validate_lesson_id
        validate_lesson_id(lesson_id)
        errors, warnings = check_prep(root_dir(), name, lesson_id)
        status = prep_status(root_dir(), name).get(lesson_id, "missing")
        return {"course": name, "lesson_id": lesson_id, "status": status,
                "ready_for_class": status == "ready" and not errors and
                    not any(w.startswith("stale") for w in warnings),
                "errors": errors, "warnings": warnings}
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/courses/{course}/prep/{lesson_id}/document")
def course_prep_document(course: str, lesson_id: str, response: Response, _: Authenticated) -> dict:
    name = existing_course(course)
    response.headers["Cache-Control"] = "no-store"
    try:
        return prep_editor.get_document(root_dir(), name, lesson_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.put("/api/courses/{course}/prep/{lesson_id}/document")
def course_prep_save_draft(course: str, lesson_id: str, payload: DraftSaveRequest,
                           response: Response, _: Authenticated) -> dict:
    name = active_course(course)
    response.headers["Cache-Control"] = "no-store"
    try:
        return prep_editor.save_draft(
            root_dir(), name, lesson_id, payload.body, payload.expected_sha256
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.put("/api/courses/{course}/prep/{lesson_id}/sections")
def course_prep_save_sections(course: str, lesson_id: str, payload: StructuredPrepSave,
                              response: Response, _: Authenticated) -> dict:
    name = active_course(course)
    response.headers["Cache-Control"] = "no-store"
    try:
        return prep_sections.save(root_dir(), name, lesson_id,
                                  payload.updates, payload.expected_sha256)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/api/courses/{course}/prep/chapter/plan")
def course_prep_chapter(course: str, payload: ChapterPrepRequest, _: Authenticated) -> dict:
    # Archived courses may be inspected with a dry-run preview, but never mutated.
    name = active_course(course) if payload.apply else existing_course(course)
    try:
        result = chapter_prep.plan_chapter(
            root_dir(), name, payload.chapter,
            sources=payload.sources, apply=payload.apply
        )
        # No automatic ready promotion. Newly created plans remain drafts.
        return {**result, "ready_granted": False, "mastery_updated": False}
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/courses/{course}/book")
def book_info(course: str, _: Authenticated) -> dict:
    name = existing_course(course)
    try:
        return books.info(root_dir(), name)
    except (books.BookError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/api/courses/{course}/book")
def import_course_book(course: str, payload: BookImport, _: Authenticated) -> dict:
    name = active_course(course)
    try:
        return books.import_markdown(root_dir(), name, payload.content)
    except (books.BookError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/courses/{course}/exercises")
def exercise_list(course: str, _: Authenticated) -> dict:
    name = existing_course(course)
    try:
        return exercises.list_questions(root_dir(), name)
    except (exercises.ExerciseError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/api/courses/{course}/exercises")
def exercise_create(course: str, payload: ExerciseCreate, _: Authenticated) -> dict:
    name = active_course(course)
    try:
        return exercises.create(root_dir(), name, payload.model_dump())
    except (exercises.ExerciseError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/api/courses/{course}/exercises/{exercise_id}/submit")
def exercise_submit(course: str, exercise_id: str,
                    payload: ExerciseSubmission, _: Authenticated) -> dict:
    name = active_course(course)
    try:
        result, inserted = exercises.submit(
            root_dir(), name, exercise_id, payload.request_id, payload.selected_indices
        )
    except (exercises.ExerciseError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    # Existing 45min timer remains authoritative. Count only *new* learner
    # attempts, never a repeated idempotent network retry.
    if inserted and lesson_timer.status(root_dir(), name).get("is_active"):
        try:
            lesson_timer.transition(root_dir(), name, "heartbeat")
        except ValueError:
            pass  # A quiz attempt does not grant or reset formal lesson time.
    return {**result, "replayed": not inserted}


@api.get("/api/courses/{course}/flashcards")
def list_course_flashcards(course: str, response: Response, _: Authenticated) -> dict:
    response.headers["Cache-Control"] = "no-store"
    name = existing_course(course)
    try:
        return flashcards.list_cards(root_dir(), name)
    except (flashcards.FlashcardError, ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/api/courses/{course}/flashcards")
def create_course_flashcard(course: str, payload: FlashcardCreate, _: Authenticated) -> dict:
    name = active_course(course)
    try:
        return flashcards.create(root_dir(), name, **payload.model_dump())
    except (flashcards.FlashcardError, ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/api/courses/{course}/flashcards/{card_id}/reveal")
def reveal_course_flashcard(course: str, card_id: str, response: Response,
                            _: Authenticated) -> dict:
    response.headers["Cache-Control"] = "no-store"
    name = active_course(course)
    try:
        return flashcards.reveal(root_dir(), name, card_id)
    except (flashcards.FlashcardError, ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/api/courses/{course}/flashcards/{card_id}/review")
def review_course_flashcard(course: str, card_id: str, payload: FlashcardReview,
                            _: Authenticated) -> dict:
    name = active_course(course)
    try:
        return flashcards.review(root_dir(), name, card_id, payload.rating,
                                 payload.reveal_token, payload.request_id)
    except (flashcards.FlashcardError, ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/courses/{course}/study-plan")
def read_study_plan(course: str, response: Response, _: Authenticated) -> dict:
    response.headers["Cache-Control"] = "no-store"
    name = existing_course(course)
    try:
        return study_plan.plan(root_dir(), name)
    except (exercises.ExerciseError, ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/courses/{course}/exercise-summary")
def exercise_summary(course: str, _: Authenticated) -> dict:
    name = existing_course(course)
    try:
        return exercises.summary(root_dir(), name)
    except (exercises.ExerciseError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/courses/{course}/timer")
def timer_status(course: str, _: Authenticated) -> dict:
    return lesson_timer.status(root_dir(), existing_course(course))


@api.post("/api/courses/{course}/timer/start")
def timer_start(course: str, payload: TimerStart, _: Authenticated) -> dict:
    name = active_course(course)
    try:
        return lesson_timer.start(root_dir(), name, payload.lesson_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/api/courses/{course}/timer/transition")
def timer_transition(course: str, payload: TimerTransition, _: Authenticated) -> dict:
    name = active_course(course)
    try:
        return lesson_timer.transition(root_dir(), name, payload.action)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/courses/{course}/chat/history")
def read_chat_history(course: str, response: Response, _: Authenticated) -> dict:
    response.headers["Cache-Control"] = "no-store"
    name = existing_course(course)
    try:
        return chat_history.recent(root_dir(), name)
    except (chat_history.HistoryError, OSError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/api/courses/{course}/chat")
async def chat(course: str, payload: ChatRequest, _: Authenticated) -> dict:
    name = active_course(course)
    # Grounded mode is the only course-chat mode. Missing sources are NOT
    # replaced with model world knowledge, and cannot tick the lesson clock.
    try:
        snapshot = retrieve(root_dir(), name, payload.message)
        prompt = grounded_prompt(payload.message, snapshot)
    except (EvidenceError, GroundingError) as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        # Logical course AgentScope identity; no unverified SDK transcript survives.
        if course_agents.status(root_dir(), name)["features"]["memory"]:
            prompt = chat_history.context_for_model(root_dir(), name) + prompt
        raw = await ask_agent(name, prompt)
    except (AgentUnavailable, course_agents.CourseAgentError, chat_history.HistoryError) as exc:
        raise HTTPException(503, str(exc)) from exc
    if not is_current(root_dir(), snapshot):
        raise HTTPException(409, "Textbook changed during generation; retry against current sources")
    try:
        outcome = check_response(raw, snapshot)
    except GroundingError as exc:
        raise HTTPException(422, str(exc)) from exc
    response = {"course": name, "speaker": "course_agent",
                "agent_id": course_agents.identity(name), "source_sha256": snapshot.sha256, **outcome}
    # A model rejection, invalid citation or textbook revision above never
    # creates a transcript. Accepted abstentions remain explicitly unverified.
    try:
        chat_history.record(root_dir(), name, payload.message, response)
    except (chat_history.HistoryError, OSError, ValueError) as exc:
        raise HTTPException(409, "Could not save validated chat: " + str(exc)) from exc
    if outcome["grounding"] == "citation_checked" and lesson_timer.status(root_dir(), name).get("is_active"):
        lesson_timer.transition(root_dir(), name, "heartbeat")
    return response


def public_group(payload: dict) -> dict:
    """v4 CLI simulates tutor voices; v5 HTTP uses bounded AgentScope agents."""
    return {**payload, "single_agent_simulation": False, "execution_backend": "agentscope"}


@api.get("/api/courses/{course}/group")
def group_status(course: str, _: Authenticated) -> dict:
    return public_group(learning_group.status(root_dir(), existing_course(course)))


@api.post("/api/courses/{course}/group/configure")
def group_configure(course: str, payload: GroupConfig, _: Authenticated) -> dict:
    name = active_course(course)
    try:
        result = learning_group.configure(root_dir(), name, payload.members, payload.lead)
        course_agents.configure(root_dir(), name, "group", True)
        return public_group(result)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/api/courses/{course}/group/start")
def group_start(course: str, payload: GroupStart, _: Authenticated) -> dict:
    name = active_course(course)
    try:
        return public_group(learning_group.start(root_dir(), name, payload.topic, payload.rounds))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/api/courses/{course}/group/continue")
def group_continue(course: str, payload: GroupContinue, _: Authenticated) -> dict:
    name = active_course(course)
    try:
        return public_group(learning_group.continue_round(root_dir(), name, payload.question))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/api/courses/{course}/group/next")
async def group_next(course: str, _: Authenticated) -> dict:
    name = active_course(course)
    if not course_agents.status(root_dir(), name)["features"]["group"]:
        raise HTTPException(409, "Tutor collaboration is not activated for this course")
    state = learning_group.status(root_dir(), name)
    speaker = state.get("next_speaker")
    session = state.get("session")
    if not speaker or not session:
        raise HTTPException(409, "No tutor turn available; wait for the learner")
    try:
        snapshot = retrieve(root_dir(), name, session["topic"])
        prompt = grounded_prompt(session["topic"], snapshot)
    except (EvidenceError, GroundingError) as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        raw = await generate_tutor_turn(name, speaker, prompt, session["turns"])
    except AgentUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if not is_current(root_dir(), snapshot):
        raise HTTPException(409, "Textbook changed during tutor generation")
    try:
        outcome = check_response(raw, snapshot)
    except GroundingError as exc:
        raise HTTPException(422, str(exc)) from exc
    if outcome["grounding"] != "citation_checked":
        raise HTTPException(409, "Tutor abstained; discussion did not advance")
    try:
        result = learning_group.record(
            root_dir(), name, speaker, outcome["text"],
            expected_session_id=session["id"],
            expected_turns=len(session["turns"]),
            expected_topic=session["topic"],
            evidence={"sha256": snapshot.sha256, "citations": outcome["citations"]},
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {**public_group(result), "source_sha256": snapshot.sha256,
            "last_turn_evidence": outcome["citations"],
            "grounding": outcome["grounding"], "fact_verified": False,
            "single_agent_simulation": False, "execution_backend": "agentscope"}


app = api
