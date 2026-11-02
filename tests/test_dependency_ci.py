"""Dependency and self-contained executable release contracts."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Every shipping workflow must keep external actions pinned to immutable SHAs.
# Pin values intentionally live in workflow YAML, not in this test: Dependabot
# must be able to rotate a valid 40-character SHA without a false CI failure.
REQUIRED_ACTION_FAMILIES = {
    ".github/workflows/ci.yml": {"actions/checkout", "actions/setup-python"},
    ".github/workflows/release.yml": {
        "actions/checkout", "actions/setup-python",
        "actions/upload-artifact", "actions/download-artifact",
    },
    ".github/workflows/platform-v5.yml": {
        "actions/checkout", "actions/setup-python", "actions/setup-node",
    },
    ".github/workflows/web-v5-bundle.yml": {
        "actions/checkout", "actions/setup-python", "actions/setup-node",
        "actions/download-artifact",
    },
}
FILES = tuple(REQUIRED_ACTION_FAMILIES)
ACTION_REF_PATTERN = r"^[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+@[0-9a-f]{40}$"


def _action_refs(workflow: str) -> list[str]:
    return re.findall(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", workflow, re.MULTILINE)


class DependencyAndStandaloneContracts(unittest.TestCase):
    def test_all_external_actions_use_immutable_commit_sha(self):
        for name in FILES:
            with self.subTest(workflow=name):
                content = (ROOT / name).read_text(encoding="utf-8")
                actions = _action_refs(content)
                self.assertTrue(actions)
                for action in actions:
                    self.assertRegex(action, ACTION_REF_PATTERN)
                self.assertNotIn("pull_request_target:", content)

    def test_required_official_action_families_present(self):
        for name, required in REQUIRED_ACTION_FAMILIES.items():
            with self.subTest(workflow=name):
                content = (ROOT / name).read_text(encoding="utf-8")
                families = {ref.split("@", 1)[0] for ref in _action_refs(content)}
                for family in required:
                    self.assertIn(family, families)

    def test_action_parser_covers_named_and_unnamed_steps(self):
        sha = "a" * 40
        workflow = chr(10).join((
            f"      - uses: actions/checkout@{sha}",
            "      - name: Upload bundle",
            f"        uses: actions/upload-artifact@{sha}",
        ))
        self.assertEqual(_action_refs(workflow), [
            f"actions/checkout@{sha}", f"actions/upload-artifact@{sha}",
        ])

    def test_action_sha_rotation_keeps_pin_validation_strict(self):
        # A routine Dependabot SHA rotation must not fail this contract.
        for digest in ("a" * 40, "b" * 40):
            self.assertRegex("actions/setup-python@" + digest, ACTION_REF_PATTERN)
        # But mutable tags, abbreviated SHAs and missing pins must still fail.
        for ref in (
            "actions/setup-python@v7",
            "actions/setup-python@" + "a" * 39,
            "actions/setup-python",
            "actions/setup-python@" + "g" * 40,
        ):
            with self.subTest(ref=ref):
                self.assertNotRegex(ref, ACTION_REF_PATTERN)

    def test_dependencies_explicitly_pinned_and_dependabot_configured(self):
        self.assertEqual((ROOT / "requirements-build.txt").read_text(encoding="utf-8").strip(),
                         "pyinstaller==6.22.3")
        self.assertEqual((ROOT / "requirements-ci.txt").read_text(encoding="utf-8").strip(),
                         "pytest==9.1.1")
        config = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
        for term in ("package-ecosystem: github-actions", "package-ecosystem: pip",
                     "interval: weekly"):
            self.assertIn(term, config)

    def test_frozen_backend_cannot_ignore_selected_workspace(self):
        source = (ROOT / "scripts/frozen_backend.py").read_text(encoding="utf-8")
        rust = (ROOT / "rust/src/main.rs").read_text(encoding="utf-8")
        smoke = (ROOT / "scripts/smoke_standalone.py").read_text(encoding="utf-8")
        self.assertIn('process.env("SOCRATOPIA_WORKSPACE_ROOT", root);', rust)
        self.assertIn('os.environ.get("SOCRATOPIA_WORKSPACE_ROOT")', source)
        self.assertIn("installed_bin.is_some() && !use_bundle", rust)
        self.assertIn("缺少 bin/socratopia-backend", rust)
        self.assertIn("alternate-workspace", smoke)
        self.assertIn("安装包不完整", smoke)
        self.assertIn("SOCRATOPIA_REQUIRE_BUNDLED", smoke)


if __name__ == "__main__":
    unittest.main()
