#!/usr/bin/env python3
"""Safe macOS installer for Decision Watchdog."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


APP_NAME = "Decision Watchdog"
APP_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
INSTALL_DIR = APP_DIR / "app"
BIN_DIR = Path.home() / ".local" / "bin"
COMMAND_PATH = BIN_DIR / "decision-watchdog"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.developeros.decision-watchdog.plist"
CODEX_HOOKS = Path.home() / ".codex" / "hooks.json"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
MARKER = "decision-watchdog"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Cannot safely edit invalid JSON: {path}\n{exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"Expected a JSON object in {path}")
    return value


def backup(path: Path) -> None:
    if path.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(f"{path.name}.backup-{stamp}"))


def save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def is_ours(handler: dict[str, Any]) -> bool:
    command = str(handler.get("command", ""))
    return MARKER in command or "Decision Watchdog/app/watchdog.py" in command


def remove_ours(settings: dict[str, Any]) -> dict[str, Any]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        cleaned_groups = []
        for group in groups:
            if not isinstance(group, dict):
                cleaned_groups.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                cleaned_groups.append(group)
                continue
            remaining = [h for h in handlers if not (isinstance(h, dict) and is_ours(h))]
            if remaining:
                updated = dict(group)
                updated["hooks"] = remaining
                cleaned_groups.append(updated)
        if cleaned_groups:
            hooks[event] = cleaned_groups
        else:
            hooks.pop(event, None)
    return settings


def merge_hooks(path: Path, source: str) -> None:
    settings = remove_ours(load_json(path))
    hooks = settings.setdefault("hooks", {})
    script = INSTALL_DIR / "watchdog.py"
    command = f'"{sys.executable}" "{script}" hook --source {source} # {MARKER}'
    for event in ("UserPromptSubmit", "Stop", "SessionEnd"):
        handler: dict[str, Any] = {"type": "command", "command": command}
        if event == "SessionEnd":
            handler["timeout"] = 3
        group = {"hooks": [handler]}
        hooks.setdefault(event, []).append(group)
    save_json(path, settings)


def install_launch_agent() -> None:
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    APP_DIR.joinpath("logs").mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": "com.developeros.decision-watchdog",
        "ProgramArguments": [sys.executable, str(INSTALL_DIR / "watchdog.py"), "worker"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(APP_DIR / "logs" / "launchagent.log"),
        "StandardErrorPath": str(APP_DIR / "logs" / "launchagent-error.log"),
    }
    backup(LAUNCH_AGENT)
    with LAUNCH_AGENT.open("wb") as handle:
        plistlib.dump(payload, handle)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(LAUNCH_AGENT)], capture_output=True)
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(LAUNCH_AGENT)], text=True, capture_output=True
    )
    if result.returncode != 0 and "already loaded" not in result.stderr.lower():
        print(f"Warning: LaunchAgent did not start: {result.stderr.strip()}", file=sys.stderr)


def choose_model() -> str:
    preferred = "qwen3.5:35b-a3b-coding-nvfp4"
    try:
        result = subprocess.run(
            ["ollama", "list"], text=True, capture_output=True, timeout=10, check=False
        )
        names = [line.split()[0] for line in result.stdout.splitlines()[1:] if line.split()]
    except (OSError, subprocess.TimeoutExpired):
        names = []
    if preferred in names:
        return preferred
    coder = next((name for name in names if "coder" in name.lower()), "")
    if coder:
        print(f"Preferred model is not installed; using detected coding model: {coder}")
        return coder
    print(f"No installed coding model detected; configured default: {preferred}")
    print(f"Install it with: ollama pull {preferred}")
    return preferred


DEFAULT_ARCHIVE = Path.home() / "Documents" / "portfolio-archive"


def detect_portfolio_archive() -> str:
    """Pre-fill the archive path only when that directory really exists."""
    return str(DEFAULT_ARCHIVE) if DEFAULT_ARCHIVE.is_dir() else ""


def configure_defaults() -> None:
    """Create config.json on a first install; never overwrite an existing one.

    An upgrade must keep the user's model, vault, archive path and thresholds,
    so missing keys are filled in and existing values are left alone.
    """
    config_path = APP_DIR / "config.json"
    added = {
        "portfolio_archive": detect_portfolio_archive(),
        "portfolio_projects_dir": "projects",
        "portfolio_evidence_dir": "private/watchdog",
        "worktree_watch": True,
        "worktree_poll_seconds": 45,
        "worktree_debounce_seconds": 300,
        "worktree_min_interval_seconds": 1800,
    }
    if config_path.exists():
        existing = load_json(config_path)
        missing = {key: value for key, value in added.items() if key not in existing}
        if missing:
            save_json(config_path, {**existing, **missing})
        return
    config = {
        "version": 1,
        "ollama_url": "http://127.0.0.1:11434/api/chat",
        "model": choose_model(),
        "obsidian_vault": "",
        "obsidian_projects_dir": "10 Projects",
        "raw_retention_days": 30,
        "poll_seconds": 8,
        "min_confidence": 0.62,
        "max_diff_chars": 60000,
        **added,
    }
    save_json(config_path, config)


def install() -> int:
    if sys.platform != "darwin":
        raise SystemExit("This installer currently supports macOS only.")
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required.")
    source_dir = Path(__file__).resolve().parent
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    for filename in ("watchdog.py", "install.py", "README.md"):
        source = source_dir / filename
        destination = INSTALL_DIR / filename
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
    os.chmod(INSTALL_DIR / "watchdog.py", 0o755)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    if COMMAND_PATH.exists() or COMMAND_PATH.is_symlink():
        if COMMAND_PATH.is_dir() and not COMMAND_PATH.is_symlink():
            raise SystemExit(f"Cannot replace directory: {COMMAND_PATH}")
        COMMAND_PATH.unlink()
    COMMAND_PATH.symlink_to(INSTALL_DIR / "watchdog.py")
    configure_defaults()
    merge_hooks(CODEX_HOOKS, "codex")
    merge_hooks(CLAUDE_SETTINGS, "claude")
    install_launch_agent()
    print("Decision Watchdog installed.")
    print(f"Command: {COMMAND_PATH}")
    if str(BIN_DIR) not in os.environ.get("PATH", "").split(":"):
        print('Add this to ~/.zshrc: export PATH="$HOME/.local/bin:$PATH"')
    print("Next:")
    print("1. Run: decision-watchdog doctor")
    print("2. In Codex, run /hooks and trust the Decision Watchdog hooks once.")
    print("3. Register a repository: decision-watchdog register /path/to/repo")
    print('4. Optional: decision-watchdog config --vault "/path/to/your/Obsidian vault"')
    print('   Portfolio archive: decision-watchdog config --archive "/path/to/archive"')
    return 0


def uninstall(remove_data: bool) -> int:
    domain = f"gui/{os.getuid()}"
    if LAUNCH_AGENT.exists():
        subprocess.run(["launchctl", "bootout", domain, str(LAUNCH_AGENT)], capture_output=True)
        LAUNCH_AGENT.unlink()
    for path in (CODEX_HOOKS, CLAUDE_SETTINGS):
        if path.exists():
            save_json(path, remove_ours(load_json(path)))
    if COMMAND_PATH.is_symlink() or COMMAND_PATH.is_file():
        COMMAND_PATH.unlink()
    if remove_data:
        shutil.rmtree(APP_DIR, ignore_errors=True)
    else:
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)
    print("Decision Watchdog uninstalled.")
    if not remove_data:
        print(f"Captured data was preserved in: {APP_DIR}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--remove-data", action="store_true")
    args = parser.parse_args()
    return uninstall(args.remove_data) if args.uninstall else install()


if __name__ == "__main__":
    raise SystemExit(main())
