"""AgentScope-only v5 runtime and distribution contracts."""
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_no_second_model_gateway_or_runtime():
    assert not (ROOT / "runtimes/pi").exists()
    assert not (ROOT / "platform_api/pi_gateway.py").exists()
    app = (ROOT / "platform_api/app.py").read_text(encoding="utf-8")
    assert "from platform_api.agentscope_runtime import" in app
    assert "raw = await ask_agent(name, prompt)" in app
    desktop = (ROOT / "platform_api/desktop.py").read_text(encoding="utf-8")
    assert "_start_pi(" not in desktop
    assert "uvicorn.run(app" in desktop


def test_package_includes_only_server_and_frontend():
    package = (ROOT / "scripts/package_web_v5.py").read_text(encoding="utf-8")
    assert "runtimes/pi" not in package
    assert "node_modules" not in package
    assert "runtime/" not in package
    assert 'entries: dict[str, Path] = {executable.name: executable}' in package


def test_agent_scope_version_and_course_isolation():
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    runtime = (ROOT / "platform_api/agentscope_runtime.py").read_text(encoding="utf-8")
    dependencies = tomllib.loads(config)["project"]["dependencies"]
    pins = [value for value in dependencies if value.startswith("agentscope")]
    assert len(pins) == 1
    match = re.fullmatch(r"agentscope==([0-9]+(?:\\.[0-9]+){2,3})", pins[0])
    assert match is not None, "AgentScope must remain exactly version-pinned"
    assert f'"sdk_version": "{match.group(1)}"' in runtime, (
        "The runtime's reported SDK version must match the installed dependency"
    )
    assert "if scope in _active:" in runtime
    assert "MAX_IN_FLIGHT = 4" in runtime


def test_v5_alpha_release_description_does_not_reintroduce_pi_runtime_claims():
    workflow = (ROOT / ".github/workflows/web-v5-bundle.yml").read_text(encoding="utf-8")
    publish = workflow.split("  publish-alpha:", 1)[1]
    assert "AgentScope-only for classroom and tutor turns" in publish
    assert "No Pi SDK or bundled Node runtime" in publish
    assert "Pi native SDK for classroom" not in publish
    assert "Bundled Node;" not in publish
