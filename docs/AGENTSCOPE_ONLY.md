# Socratopia AgentScope-only migration · 2026-10-18

## Verified official baseline
- AgentScope 2.0.10 (released 2026-10-12): https://github.com/agentscope-ai/agentscope/releases/tag/v2.0.10
- AgentScope official documentation: https://docs.agentscope.io/en/versions/2.0.10/
- Python 3.15 / FastAPI, React 19 / Ant Design 6 / AntV G2. Avoid upgrading build tool majors without independent browser and packaging gates.

## Runtime ownership
- Python FastAPI invokes `platform_api.agentscope_runtime.ask_agent` for normal classroom and `generate_tutor_turn` for explicit D/E/F collaboration.
- The same bounded runtime uses per-course logical identities, read-only opt-in graph tools and server-supplied model profiles. No second SDK, gateway, autonomous agent loop or tool execution engine.
- Each inference request creates a fresh bounded agent, with timeout and one in-flight request per course (up to four globally). Persistent facts remain in per-course SQLite and Markdown.
- Models do not decide PREP approval, 45-minute lesson time, deterministic grading, verified mastery, archive state or file permissions.
- LLM output must pass source ID, verbatim textbook quote, relevance check and source SHA fence before it is displayed or persisted.
- Course-agent feature flags include memory, collaboration and read-only graph.

## Delivery
- Frozen desktop runs FastAPI and statically serves the built React UI. No Node process or Pi runtime is distributed.
- CI must cover AgentScope 2.0.10 import signatures, model error cases, course isolation, browser E2E and four native ZIP smoke tests.
- Missing or failing real-model evaluations remain explicitly unverified.

## Deferred improvements
- Reconcile old v4 host-specific prompt/skill documents into framework-agnostic teaching resources; remove obsolete host adapters after migrating their useful learning content.
- Add real-provider E2E (normal classroom + two independent tutors) before stable.
- Evaluate opt-in AgentScope model router and TeamPipeline against bounded turn-by-turn tutoring; do not introduce uncontrolled agent loops or duplicate memory stores.

## Archived-course write boundary · 2026-11-23

Archiving a course makes its course-owned state read-only until it is restored. The API enforces this at the route boundary with `active_course` before changing agent feature flags, importing the authoritative textbook, or applying a generated chapter PREP. A chapter PREP dry-run remains available because it only returns a preview; `apply=true` is rejected with HTTP 409 while archived. Read-only status/evidence views remain available, and the lifecycle endpoint is the explicit restore path. Snapshot export remains a separate explicit-consent backup action and does not mutate local course files.

Regression coverage verifies that all three blocked writes leave their target files unchanged/absent, previews remain non-mutating, and the same operations work again after explicit restoration. The test uses temporary fixture courses only; it does not access or modify user course data.
