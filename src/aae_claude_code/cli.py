from __future__ import annotations

import argparse
from datetime import date
import hashlib
import importlib.resources
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Iterable, cast

from aae.hooks import find_aae_root, process_event


MAX_PAYLOAD_BYTES = 1_048_576
MAX_CONTEXT_CHARS = 8_000
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
PROJECTION_MARKER = "<!-- aae-adapter-projection: claude-code"


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _portable_path(root: Path, value: str) -> str | None:
    if not value or len(value) > 4096 or any(char in value for char in "\n\r\x00"):
        return None
    candidate = Path(value.replace("\\", "/"))
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(root.resolve())
        except ValueError:
            return None
    portable = candidate.as_posix().removeprefix("./")
    return None if not portable or ".." in Path(portable).parts else portable


def _changed_paths(root: Path, tool_input: object) -> list[str]:
    if not isinstance(tool_input, dict):
        return []
    values: list[str] = []
    for key in ("file_path", "notebook_path", "filePath", "path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            values.append(value)
    for key in ("edits", "files"):
        entries = tool_input.get(key, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str):
                values.append(entry)
            elif isinstance(entry, dict):
                for path_key in ("file_path", "notebook_path", "filePath", "path"):
                    value = entry.get(path_key)
                    if isinstance(value, str):
                        values.append(value)
    return sorted({path for value in values if (path := _portable_path(root, value))})


def _template_root() -> Any:
    return importlib.resources.files("aae_claude_code").joinpath("templates")


def install(root: Path) -> int:
    installed: list[str] = []
    preserved: list[str] = []
    for resource in _template_root().rglob("*"):
        if not resource.is_file() or resource.name.endswith(".pyc"):
            continue
        relative = Path(*resource.relative_to(_template_root()).parts)
        destination = root / relative
        if destination.exists():
            preserved.append(relative.as_posix())
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with importlib.resources.as_file(resource) as source:
            shutil.copyfile(source, destination)
        installed.append(relative.as_posix())
    sync_errors = sync_skills(root)
    print(json.dumps({"installed": installed, "preserved": preserved, "skill_errors": sync_errors}, indent=2))
    return 1 if sync_errors else 0


def sync_skills(root: Path) -> list[str]:
    errors: list[str] = []
    source_root = root / ".aae/skills"
    if not source_root.is_dir():
        return []
    for manifest_path in sorted(source_root.glob("*/skill.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            name = manifest["name"]
            description = manifest["description"]
            procedure_path = manifest_path.parent / str(manifest.get("procedure", "SKILL.md"))
            procedure = procedure_path.read_text(encoding="utf-8")
            if not isinstance(name, str) or not isinstance(description, str):
                raise ValueError("name and description must be strings")
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError) as error:
            errors.append(f"{manifest_path}: {error}")
            continue
        source_sha256 = hashlib.sha256(
            manifest_path.read_bytes() + b"\0" + procedure.encode()
        ).hexdigest()
        destination = root / ".claude/skills" / name / "SKILL.md"
        if destination.exists() and PROJECTION_MARKER not in destination.read_text(encoding="utf-8"):
            errors.append(f"Preserved non-AAE native skill: {destination}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "---\n"
            f"name: {name}\n"
            f"description: {json.dumps(description.replace(chr(10), ' ').strip())}\n"
            "---\n"
            f"{PROJECTION_MARKER} source-sha256: {source_sha256} -->\n\n"
            + procedure.lstrip(),
            encoding="utf-8",
        )
    return errors


def handle_hook(start: Path, native: dict[str, Any]) -> dict[str, Any] | None:
    root = find_aae_root(start)
    if root is None or native.get("hook_event_name") != "PostToolUse":
        return None
    tool_name = native.get("tool_name")
    tool_input = native.get("tool_input", {})
    if tool_name not in EDIT_TOOLS or not isinstance(tool_input, dict):
        return None
    paths = _changed_paths(root, tool_input)
    if not paths:
        return None
    native_sha256 = _digest(native)
    identifiers = {
        key: native[key]
        for key in ("session_id", "tool_use_id")
        if isinstance(native.get(key), str)
    }
    record, procedures, errors = process_event(
        root,
        event="files-changed",
        payload={"paths": paths},
        idempotency_key="claude-code:" + _digest({"payload": native_sha256, **identifiers}),
        record_no_match=False,
        delivery_provenance={
            "adapter": "claude-code",
            "native_event": "PostToolUse",
            "payload_sha256": native_sha256,
            "tool_name": tool_name,
            "paths": paths,
            **identifiers,
        },
    )
    messages = list(procedures.values())
    if errors:
        messages.append("AAE adapter errors: " + "; ".join(errors))
    failed = record.get("status") in {
        "failed",
        "denied",
        "configuration-invalid",
        "chain-depth-denied",
        "action-budget-denied",
    }
    if failed and not errors:
        messages.append(f"AAE event status: {record.get('status')}")
    if not messages:
        return None
    context = "\n\n".join(messages)[:MAX_CONTEXT_CHARS]
    output: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }
    if failed:
        output["decision"] = "block"
        output["reason"] = context
    return output


def hook_command(path: Path) -> int:
    raw = sys.stdin.read(MAX_PAYLOAD_BYTES + 1)
    if len(raw.encode()) > MAX_PAYLOAD_BYTES:
        print("Claude Code hook payload exceeds 1 MiB", file=sys.stderr)
        return 1
    try:
        native = json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"Invalid Claude Code hook JSON: {error}", file=sys.stderr)
        return 1
    if not isinstance(native, dict):
        print("Claude Code hook payload must be an object", file=sys.stderr)
        return 1
    output = handle_hook(path, native)
    if output is not None:
        print(json.dumps(output, separators=(",", ":")))
    return 0


def _capabilities() -> dict[str, Any]:
    resource = importlib.resources.files("aae_claude_code").joinpath("capabilities.json")
    return cast(dict[str, Any], json.loads(resource.read_text(encoding="utf-8")))


def doctor(root: Path, strict: bool) -> int:
    manifest = _capabilities()
    executable = shutil.which("claude")
    version = None
    if executable:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, check=False
        )
        version = result.stdout.strip() or result.stderr.strip()
    tested = manifest["verified_versions"]["cli"]
    verified = bool(version and any(item in version for item in tested))
    age_days = (date.today() - date.fromisoformat(manifest["verified_at"])).days
    project_files = {
        "CLAUDE.md": (root / "CLAUDE.md").is_file(),
        ".claude/settings.json": (root / ".claude/settings.json").is_file(),
        ".claude/agents/aae-independent-reviewer.md": (
            root / ".claude/agents/aae-independent-reviewer.md"
        ).is_file(),
    }
    print(json.dumps({
        "adapter": "claude-code",
        "runtime": {"executable": executable, "version": version},
        "verified_version": verified,
        "verification_age_days": age_days,
        "project_files": project_files,
    }, indent=2))
    healthy = bool(executable and verified and all(project_files.values()))
    return 1 if strict and not healthy else 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="AAE native Claude Code adapter")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("init", "sync-skills", "hook"):
        command = commands.add_parser(name)
        command.add_argument("path", nargs="?", default=".")
    doctor_parser = commands.add_parser("doctor")
    doctor_parser.add_argument("path", nargs="?", default=".")
    doctor_parser.add_argument("--strict", action="store_true")
    return root


def main(argv: Iterable[str] | None = None) -> int:
    arguments = parser().parse_args(list(argv) if argv is not None else None)
    root = Path(arguments.path).resolve()
    if arguments.command == "init":
        return install(root)
    if arguments.command == "sync-skills":
        errors = sync_skills(root)
        print(json.dumps({"errors": errors}, indent=2))
        return 1 if errors else 0
    if arguments.command == "hook":
        return hook_command(root)
    return doctor(root, arguments.strict)
