"""Local API integration smoke gates for v5; legacy tests run separately."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

if sys.version_info < (3, 15):
    pytest.skip("v5 API requires Python 3.15", allow_module_level=True)

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient
from platform_api.app import api
from platform_api.agentscope_runtime import AgentUnavailable, ask_agent
from platform_api.evidence import retrieve


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "templates", tmp_path / "templates")
    monkeypatch.setenv("SOCRATOPIA_ROOT", str(tmp_path))
    monkeypatch.setenv("SOCRATOPIA_API_TOKEN", "test-only-secret-token-00001")
    return TestClient(api)


def add_book(course: str, text: str) -> Path:
    path = Path(os.environ["SOCRATOPIA_ROOT"]) / "TEXTBOOK" / course / "book.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def draft(statement: str, quote: str, source_id: str = "S1") -> str:
    return json.dumps({"status": "answer", "claims": [
        {"text": statement, "source_id": source_id, "quote": quote}
    ]}, ensure_ascii=False)


def auth() -> dict:
    return {"Authorization": "Bearer test-only-secret-token-00001"}


def test_auth_fails_closed(client: TestClient) -> None:
    assert client.get("/api/courses").status_code == 401
    assert client.get("/api/courses", headers={"Authorization": "Bearer incorrect"}).status_code == 401
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/runtime").status_code == 401
    info = client.get("/api/runtime", headers=auth()).json()
    assert info["backend"] == "agentscope-course"
    assert info["collaboration_backend"] == "agentscope"
    assert info["tutors"]["max_agent_iterations"] == 1


def test_create_and_read_course_without_mastery(client: TestClient) -> None:
    created = client.post("/api/courses", json={"name": "遗传学", "tutor": "F"}, headers=auth())
    assert created.status_code == 200, created.text
    assert created.json()["ready_for_class"] is False
    assert "遗传学" in client.get("/api/courses", headers=auth()).json()["courses"]
    state = client.get("/api/courses/遗传学/state", headers=auth()).json()
    assert state["phase"] == "setup"
    assert state["blockers"]
    timer = client.get("/api/courses/遗传学/timer", headers=auth()).json()
    assert timer["remaining_seconds"] == 2700
    # A newly initialized course cannot claim a completed lesson.
    start = client.post("/api/courses/遗传学/timer/start",
                        headers=auth(), json={"lesson_id": "lesson_001"})
    assert start.status_code == 409
    assert client.post("/api/courses", json={"name": "../escape"}, headers=auth()).status_code == 409


def test_chat_preserves_progress(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.post("/api/courses", json={"name": "生物信息学"}, headers=auth())

    quote = "RNA-seq通过测序分析样本的RNA表达情况。"
    add_book("生物信息学", "# RNA-seq\n" + quote + "\n")

    async def fake_pi(course: str, message: str) -> str:
        assert course == "生物信息学"
        assert "<untrusted_course_evidence>" in message
        return draft("RNA-seq用于研究表达。", quote)

    monkeypatch.setattr("platform_api.app.ask_agent", fake_pi)
    reply = client.post("/api/courses/生物信息学/chat", headers=auth(), json={"message": "什么是 RNA-seq?"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["mastery_updated"] is False
    assert reply.json()["grounding"] == "citation_checked"
    assert reply.json()["fact_verified"] is False
    assert reply.json()["citations"][0]["quote"] == quote
    timer = client.get("/api/courses/生物信息学/timer", headers=auth()).json()
    assert timer["phase"] == "not_started"


def test_real_group_state_machine_with_stubbed_llm(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.post("/api/courses", json={"name": "学习小组课程"}, headers=auth())
    quote = "基因表达是遗传信息转化为功能产物的过程。"
    add_book("学习小组课程", "# 基因表达\n" + quote + "\n")
    config = client.post("/api/courses/学习小组课程/group/configure",
                         headers=auth(), json={"members": ["D", "E"], "lead": "D"})
    assert config.status_code == 200, config.text
    assert config.json()["single_agent_simulation"] is False
    assert config.json()["execution_backend"] == "agentscope"
    started = client.post("/api/courses/学习小组课程/group/start",
                          headers=auth(), json={"topic": "基因表达", "rounds": 2})
    assert started.json()["next_speaker"] == "D"

    async def fake_tutor(course: str, speaker: str, topic: str, prior: list[dict]) -> str:
        assert "基因表达" in topic and "<source id=" in topic
        return draft("基因表达涉及遗传信息转化。", quote)

    monkeypatch.setattr("platform_api.app.generate_tutor_turn", fake_tutor)
    first = client.post("/api/courses/学习小组课程/group/next", headers=auth())
    assert first.status_code == 200, first.text
    saved = client.get("/api/courses/学习小组课程/group", headers=auth()).json()
    evidence = saved["session"]["turns"][0]["evidence"]
    assert evidence["grounding"] == "citation_checked"
    assert evidence["fact_verified"] is False
    assert evidence["citations"][0]["quote"] == quote
    assert len(evidence["sha256"]) == 64
    second = client.post("/api/courses/学习小组课程/group/next", headers=auth())
    assert second.json()["requires_user_turn"] is True
    blocked = client.post("/api/courses/学习小组课程/group/next", headers=auth())
    assert blocked.status_code == 409
    continued = client.post("/api/courses/学习小组课程/group/continue", headers=auth(), json={})
    assert continued.json()["next_speaker"] == "D"


def test_agent_runtime_requires_model_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(AgentUnavailable):
        asyncio.run(ask_agent("课程", "你好"))


def test_missing_and_irrelevant_books_fail_closed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.post("/api/courses", json={"name": "无教材"}, headers=auth())

    async def forbidden(*args):
        raise AssertionError("No model calls without relevant sources")

    monkeypatch.setattr("platform_api.app.ask_agent", forbidden)
    assert client.post("/api/courses/无教材/chat", headers=auth(), json={"message": "基因表达"}).status_code == 409
    overview = client.get("/api/courses/无教材/overview", headers=auth()).json()
    assert overview["grounded_chat_ready"] is False and overview["formal_lesson_ready"] is False
    add_book("无教材", "# 植物\n光合作用是植物将光能转化为化学能的过程。\n")
    assert client.post("/api/courses/无教材/chat", headers=auth(), json={"message": "RNA-seq"}).status_code == 409
    assert client.get("/api/courses/无教材/evidence?q=光合作用", headers=auth()).json()["excerpts"]


def test_sources_are_course_isolated(client: TestClient) -> None:
    for name in ["课程甲", "课程乙"]:
        client.post("/api/courses", json={"name": name}, headers=auth())
    add_book("课程甲", "# 细胞分裂\n细胞分裂会产生子细胞。\n")
    add_book("课程乙", "# 细胞分裂\n不同课程的证据不可交叉使用。\n")
    root = Path(os.environ["SOCRATOPIA_ROOT"])
    a, b = retrieve(root, "课程甲", "细胞分裂"), retrieve(root, "课程乙", "细胞分裂")
    assert a.sha256 != b.sha256
    assert "不可交叉" not in str(a.sources) and "产生子细胞" not in str(b.sources)


@pytest.mark.parametrize("malicious", [
    '{"status":"answer","claims":[{"text":"错误","source_id":"S404","quote":"细胞分裂会产生子细胞。"}]}',
    '{"status":"answer","claims":[{"text":"错误","source_id":"S1","quote":"一段伪造的原始教材证据。"}]}',
    '{"status":"answer","claims":[]}',
    '{"status":"answer","claims":[{"text":"海洋洋流决定火山爆发周期。","source_id":"S1","quote":"细胞分裂会产生子细胞。"}]}',
    "根据教材我知道这是真的。",
])
def test_model_invented_citations_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch, malicious: str) -> None:
    client.post("/api/courses", json={"name": "细胞学"}, headers=auth())
    add_book("细胞学", "# 细胞分裂\n细胞分裂会产生子细胞。\n")
    progress = Path(os.environ["SOCRATOPIA_ROOT"]) / "DATA" / "细胞学" / "PROGRESS.md"
    before = progress.read_bytes()

    async def forged(*args):
        return malicious

    monkeypatch.setattr("platform_api.app.ask_agent", forged)
    response = client.post("/api/courses/细胞学/chat", headers=auth(), json={"message": "细胞分裂"})
    assert response.status_code == 422, response.text
    assert progress.read_bytes() == before


def test_abstention_remains_unverified(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.post("/api/courses", json={"name": "知识测试"}, headers=auth())
    add_book("知识测试", "# 基因表达\n基因表达是遗传信息形成产物的过程。\n")
    async def abstain(*args):
        return '{"status":"abstain","reason":"找不到答案"}'
    monkeypatch.setattr("platform_api.app.ask_agent", abstain)
    result = client.post("/api/courses/知识测试/chat", headers=auth(), json={"message": "基因表达"})
    assert result.status_code == 200
    data = result.json()
    assert data["grounding"] == "insufficient_evidence"
    assert data["citations"] == [] and data["mastery_updated"] is False


def test_changed_textbook_blocks_response(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.post("/api/courses", json={"name": "分子生物学"}, headers=auth())
    book = add_book("分子生物学", "# DNA复制\nDNA复制需要模板链和DNA聚合酶。\n")
    async def changed(*args):
        book.write_text("# DNA复制\nDNA复制有新的内容。\n", encoding="utf-8")
        return draft("DNA复制有模板。", "DNA复制需要模板链和DNA聚合酶。")
    monkeypatch.setattr("platform_api.app.ask_agent", changed)
    result = client.post("/api/courses/分子生物学/chat", headers=auth(), json={"message": "DNA复制"})
    assert result.status_code == 409


def test_group_abstention_does_not_advance(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.post("/api/courses", json={"name": "讨论课"}, headers=auth())
    add_book("讨论课", "# 基因表达\n基因表达是细胞利用遗传信息的过程。\n")
    client.post("/api/courses/讨论课/group/configure", headers=auth(), json={"members": ["D", "E"], "lead": "D"})
    client.post("/api/courses/讨论课/group/start", headers=auth(), json={"topic": "基因表达"})
    async def abstain(*args):
        return '{"status":"abstain","reason":"不确定"}'
    monkeypatch.setattr("platform_api.app.generate_tutor_turn", abstain)
    assert client.post("/api/courses/讨论课/group/next", headers=auth()).status_code == 409
    state = client.get("/api/courses/讨论课/group", headers=auth()).json()
    assert state["next_speaker"] == "D" and state["session"]["turns"] == []


def test_restarted_group_rejects_outdated_reply(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.lib import learning_group
    root = Path(os.environ["SOCRATOPIA_ROOT"])
    client.post("/api/courses", json={"name": "轮次竞态"}, headers=auth())
    add_book("轮次竞态", "# 基因表达\n基因表达是细胞利用遗传信息的过程。\n")
    client.post("/api/courses/轮次竞态/group/configure", headers=auth(), json={"members": ["D", "E"], "lead": "D"})
    client.post("/api/courses/轮次竞态/group/start", headers=auth(), json={"topic": "基因表达"})
    async def contested(*args):
        learning_group.stop(root, "轮次竞态")
        learning_group.start(root, "轮次竞态", "基因表达")
        return draft("基因表达属于细胞过程。", "基因表达是细胞利用遗传信息的过程。")
    monkeypatch.setattr("platform_api.app.generate_tutor_turn", contested)
    assert client.post("/api/courses/轮次竞态/group/next", headers=auth()).status_code == 409
    state = client.get("/api/courses/轮次竞态/group", headers=auth()).json()
    assert state["next_speaker"] == "D" and state["session"]["turns"] == []


def test_symlink_book_refused(client: TestClient, tmp_path: Path) -> None:
    client.post("/api/courses", json={"name": "安全课程"}, headers=auth())
    outside = tmp_path / "outside.md"
    outside.write_text("# RNA-seq\nRNA-seq is a transcriptome profiling assay.\n")
    (tmp_path / "TEXTBOOK" / "安全课程" / "book.md").symlink_to(outside)
    assert client.get("/api/courses/安全课程/evidence?q=RNA-seq", headers=auth()).status_code == 409

def test_pi_memory_is_course_bound_and_invalidated_by_revised_book(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from platform_api.chat_history import context_for_model

    for course in ("教材甲", "教材乙"):
        assert client.post("/api/courses", headers=auth(), json={"name": course}).status_code == 200
    quote = "DNA双螺旋的两条链反向平行并通过碱基配对。"
    book = add_book("教材甲", "# DNA结构\n" + quote + "\n")
    add_book("教材乙", "# RNA结构\nRNA通常由一条多核苷酸链构成。\n")
    async def quoted(*args):
        return draft(quote, quote)
    monkeypatch.setattr("platform_api.app.ask_agent", quoted)
    result = client.post("/api/courses/教材甲/chat", headers=auth(), json={"message": "DNA双螺旋"})
    assert result.status_code == 200, result.text
    root = Path(os.environ["SOCRATOPIA_ROOT"])
    assert "反向平行" in context_for_model(root, "教材甲")
    assert "反向平行" not in context_for_model(root, "教材乙")
    book.write_text("# DNA新版本\n原教材已修订。\n", encoding="utf-8")
    assert context_for_model(root, "教材甲") == ""


def test_course_lifecycle_and_grounded_graph(client, monkeypatch):
    from pathlib import Path
    from platform_api.course_graph import build
    root = Path(os.environ["SOCRATOPIA_ROOT"])
    assert client.post("/api/courses", headers=auth(), json={"name": "课程图谱"}).status_code == 200
    add_book("课程图谱", "# 基因结构\n## DNA双螺旋\n两条链反向平行。\n## RNA\n核糖核酸。\n")
    graph = client.get("/api/courses/课程图谱/graph", headers=auth())
    assert graph.status_code == 200
    assert [n["label"] for n in graph.json()["nodes"]] == ["基因结构", "DNA双螺旋", "RNA"]
    assert len(graph.json()["edges"]) == 2
    assert graph.json()["fact_verified"] is False
    archive = client.post("/api/courses/课程图谱/lifecycle", headers=auth(), json={"archived": True})
    assert archive.status_code == 200 and archive.json()["archived"]
    assert client.post("/api/courses/课程图谱/chat", headers=auth(), json={"message": "DNA"}).status_code == 409
    assert client.get("/api/courses/课程图谱/graph", headers=auth()).status_code == 200
    assert client.post("/api/courses/课程图谱/lifecycle", headers=auth(), json={"archived": False}).json()["archived"] is False


def test_archived_courses_block_remaining_write_endpoints(client: TestClient) -> None:
    root = Path(os.environ["SOCRATOPIA_ROOT"])

    def create_course(name: str) -> None:
        response = client.post("/api/courses", headers=auth(), json={"name": name})
        assert response.status_code == 200, response.text

    def archive_course(name: str, archived: bool) -> None:
        response = client.post(
            f"/api/courses/{name}/lifecycle",
            headers=auth(),
            json={"archived": archived},
        )
        assert response.status_code == 200, response.text
        assert response.json()["archived"] is archived

    # Agent feature flags are course state, not an exception to archive mode.
    create_course("封存功能")
    archive_course("封存功能", True)
    feature_file = root / "DATA" / "封存功能" / "runtime" / "agent_features.json"
    blocked_feature = client.post(
        "/api/courses/封存功能/agent/features",
        headers=auth(),
        json={"feature": "graph", "enabled": True},
    )
    assert blocked_feature.status_code == 409
    assert not feature_file.exists()
    archive_course("封存功能", False)
    enabled_feature = client.post(
        "/api/courses/封存功能/agent/features",
        headers=auth(),
        json={"feature": "graph", "enabled": True},
    )
    assert enabled_feature.status_code == 200
    assert enabled_feature.json()["features"]["graph"] is True

    # A textbook import must not create a new authoritative book while archived.
    create_course("封存教材")
    archive_course("封存教材", True)
    book_content = (
        "# 植物遗传学\n"
        "遗传信息通过染色体从亲代传递给子代，减数分裂和受精参与有性生殖。\n"
    )
    book_file = root / "TEXTBOOK" / "封存教材" / "book.md"
    blocked_book = client.post(
        "/api/courses/封存教材/book",
        headers=auth(),
        json={"content": book_content},
    )
    assert blocked_book.status_code == 409
    assert not book_file.exists()
    archive_course("封存教材", False)
    imported_book = client.post(
        "/api/courses/封存教材/book",
        headers=auth(),
        json={"content": book_content},
    )
    assert imported_book.status_code == 200
    assert book_file.read_text(encoding="utf-8") == book_content

    # PREP preview is read-only; applying the generated draft is a mutation.
    create_course("封存备课")
    add_book(
        "封存备课",
        "# 第一章 DNA复制\n## 复制起始\nDNA复制需要模板链和DNA聚合酶。\n",
    )
    archive_course("封存备课", True)
    preview = client.post(
        "/api/courses/封存备课/prep/chapter/plan",
        headers=auth(),
        json={"chapter": "第一章 DNA复制", "apply": False},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["dry_run"] is True
    prep_file = Path(preview.json()["path"])
    assert not prep_file.exists()

    blocked_prep = client.post(
        "/api/courses/封存备课/prep/chapter/plan",
        headers=auth(),
        json={"chapter": "第一章 DNA复制", "apply": True},
    )
    assert blocked_prep.status_code == 409
    assert not prep_file.exists()

    archive_course("封存备课", False)
    applied_prep = client.post(
        "/api/courses/封存备课/prep/chapter/plan",
        headers=auth(),
        json={"chapter": "第一章 DNA复制", "apply": True},
    )
    assert applied_prep.status_code == 200, applied_prep.text
    assert prep_file.is_file()


def test_course_study_task_idempotency(client):
    assert client.post("/api/courses", headers=auth(), json={"name":"任务课"}).status_code == 200
    payload = {"title": "复习显性与隐性遗传", "kind": "review", "request_id": "task-repeat-12345678"}
    one = client.post("/api/courses/任务课/tasks", headers=auth(), json=payload)
    two = client.post("/api/courses/任务课/tasks", headers=auth(), json=payload)
    assert one.status_code == 200
    assert one.json()["id"] == two.json()["id"]
    tasks = client.get("/api/courses/任务课/tasks", headers=auth()).json()["tasks"]
    assert len(tasks) == 1 and tasks[0]["status"] == "pending"
