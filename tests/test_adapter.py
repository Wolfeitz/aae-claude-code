from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from aae_claude_code.cli import PROJECTION_MARKER, handle_hook, install, sync_skills


class ClaudeCodeAdapterTests(unittest.TestCase):
    def test_init_installs_native_files_and_preserves_existing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / ".claude/settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text('{"existing": true}', encoding="utf-8")
            self.assertEqual(install(root), 0)
            self.assertEqual(settings.read_text(), '{"existing": true}')
            self.assertTrue((root / "CLAUDE.md").is_file())
            self.assertTrue((root / ".claude/agents/aae-independent-reviewer.md").is_file())

    def test_projects_skills_without_replacing_native_authorship(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / ".aae/skills/repo-recon"
            source.mkdir(parents=True)
            (source / "skill.json").write_text(
                json.dumps({"name": "repo-recon", "description": "Inspect the repository"}),
                encoding="utf-8",
            )
            (source / "SKILL.md").write_text("# Procedure\n", encoding="utf-8")
            self.assertEqual(sync_skills(root), [])
            projected = root / ".claude/skills/repo-recon/SKILL.md"
            self.assertIn(PROJECTION_MARKER, projected.read_text())
            projected.write_text("hand-authored", encoding="utf-8")
            self.assertTrue(sync_skills(root))
            self.assertEqual(projected.read_text(), "hand-authored")

    def test_hook_does_not_persist_tool_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aae = root / ".aae"
            aae.mkdir()
            (aae / "hooks.json").write_text(json.dumps({
                "schema_version": 1,
                "rules": [{
                    "id": "check-python",
                    "on": "files-changed",
                    "paths": ["src/**/*.py"],
                    "run_check": [sys.executable, "-c", "raise SystemExit(0)"],
                }],
            }), encoding="utf-8")
            secret = "never-persist-this-response"
            output = handle_hook(root, {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "tool_use_id": "tool-1",
                "tool_name": "Write",
                "tool_input": {"file_path": "src/aae/core.py"},
                "tool_response": secret,
            })
            self.assertIsNone(output)
            record = next((root / ".aae/runtime/hook-events").glob("*.json")).read_text()
            self.assertNotIn(secret, record)
            self.assertIn('"adapter": "claude-code"', record)

    def test_multi_edit_extracts_each_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aae = root / ".aae"
            aae.mkdir()
            (aae / "hooks.json").write_text(json.dumps({
                "schema_version": 1,
                "rules": [{
                    "id": "check-python",
                    "on": "files-changed",
                    "paths": ["src/**/*.py"],
                    "run_check": [sys.executable, "-c", "raise SystemExit(0)"],
                }],
            }), encoding="utf-8")
            handle_hook(root, {
                "hook_event_name": "PostToolUse",
                "tool_name": "MultiEdit",
                "tool_input": {"edits": [
                    {"file_path": "src/app/one.py"},
                    {"file_path": "src/app/two.py"},
                ]},
            })
            record = json.loads(next((root / ".aae/runtime/hook-events").glob("*.json")).read_text())
            self.assertEqual(
                record["delivery_provenance"]["paths"],
                ["src/app/one.py", "src/app/two.py"],
            )


if __name__ == "__main__":
    unittest.main()
