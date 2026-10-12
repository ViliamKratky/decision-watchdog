#!/usr/bin/env python3
"""Local-first decision history collector and case-study builder."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


APP_NAME = "Decision Watchdog"
HOME_ENV_VAR = "DECISION_WATCHDOG_HOME"
MAX_TEXT = 60_000
VERSION = "0.4.0"


def default_home() -> Path:
    override = os.environ.get(HOME_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / APP_NAME


def use_home(home: Path) -> None:
    """Re-point every derived path at `home`.

    The self-test and the unit tests both need a throwaway installation that
    cannot touch the real captured history.
    """
    global APP_DIR, CONFIG_PATH, QUEUE_DIR, DATA_DIR, LOG_DIR
    APP_DIR = Path(home)
    CONFIG_PATH = APP_DIR / "config.json"
    QUEUE_DIR = APP_DIR / "queue"
    DATA_DIR = APP_DIR / "data"
    LOG_DIR = APP_DIR / "logs"


APP_DIR = CONFIG_PATH = QUEUE_DIR = DATA_DIR = LOG_DIR = Path()
use_home(default_home())

# Files whose content is never read, and whose diffs are never sent, because
# they usually hold credentials. `.env.local` and `.env.production` are as
# sensitive as `.env` itself, so the suffix is part of the pattern.
SENSITIVE_NAME_RE = re.compile(
    r"(^|/)("
    r"\.env(\.[^/]*)?|[^/]*\.env|env\.(local|dev|development|prod|production|staging|test)"
    r"|\.envrc|[^/]*\.(pem|key|p12|pfx|p8|keystore|jks|ovpn|kdbx|tfstate|tfstate\.backup)"
    r"|id_[a-z0-9]+|credentials[^/]*|service[-_]?account[^/]*\.json"
    r"|secrets?(\.(json|ya?ml|toml|ini|txt))?"
    r"|\.npmrc|\.pypirc|\.netrc|\.git-credentials|\.htpasswd|kubeconfig"
    r"|\.kube/config|\.docker/config\.json|\.aws/(credentials|config)"
    r")$",
    re.IGNORECASE,
)
_SENSITIVE_KEY = (
    r"[\w.-]*(?:password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key"
    r"|private[_-]?key|auth[_-]?token|authorization|credential)s?[\w.-]*"
)
# (pattern, replacement) pairs, applied in order.
SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----.*?"
            r"-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----",
            re.DOTALL,
        ),
        "[REDACTED SECRET]",
    ),
    # A key cut in half by an earlier clip has no END marker left.
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----(?:\n[+\- ]?[A-Za-z0-9+/=]*)*"),
        "[REDACTED SECRET]",
    ),
    (
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{20,}"
            r"|glpat-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{16,}|(?:AKIA|ASIA)[0-9A-Z]{16}"
            r"|AIza[0-9A-Za-z_-]{35}|[rs]k_(?:live|test)_[0-9A-Za-z]{10,}|npm_[A-Za-z0-9]{36}"
            r"|eyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]{8,})"
        ),
        "[REDACTED SECRET]",
    ),
    # user:password@ in any URL, e.g. database URLs and git remotes.
    (re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@", re.IGNORECASE), r"\1[REDACTED]@"),
    (re.compile(r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), r"\1 [REDACTED]"),
    # KEY=value, key: value, "key": "value", export KEY="value", also on diff lines.
    (
        re.compile(
            r"(?im)((?:^|(?<=[\s\"'{,;:(+-]))(?:export\s+)?[\"']?" + _SENSITIVE_KEY
            + r"[\"']?\s*[:=]\s*)(\"[^\"\n]*\"|'[^'\n]*'|(?![\w.]*\()[^\s,;#'\"]+)"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?im)^([+\- ]?\s*machine\s+\S+.*?\bpassword\s+)\S+"), r"\1[REDACTED]"),
]

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "ollama_url": "http://127.0.0.1:11434/api/chat",
    "model": "qwen3.5:35b-a3b-coding-nvfp4",
    "obsidian_vault": "",
    "obsidian_projects_dir": "10 Projects",
    # Central SWE portfolio archive. Distilled evidence for every registered
    # repository is exported here; the tracked repositories themselves are only
    # ever read. Empty means "keep the history local only".
    "portfolio_archive": "",
    "portfolio_projects_dir": "projects",
    # Written inside the archive's per-project directory. The private zone is
    # deliberate: raw captured evidence is never published as-is, and the
    # curated case-study.md / decisions/ files stay hand-written.
    "portfolio_evidence_dir": "private/watchdog",
    "raw_retention_days": 30,
    "poll_seconds": 8,
    "min_confidence": 0.62,
    "max_diff_chars": 60_000,
    # Uncommitted-change monitoring. A working tree is analyzed only once it has
    # been quiet for `worktree_debounce_seconds`, so an edit in progress is never
    # treated as a finished piece of work.
    "worktree_watch": True,
    "worktree_poll_seconds": 45,
    "worktree_debounce_seconds": 300,
    "worktree_min_interval_seconds": 1800,
}

EVENT_TYPES = [
    "decision",
    "discovery",
    "problem",
    "failed_attempt",
    "constraint",
    "milestone",
    "metric",
    "lesson",
]


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def log_error(filename: str, message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / filename).open("a", encoding="utf-8") as handle:
            handle.write(f"{now_iso()} {message}\n")
    except OSError:
        pass


def ensure_dirs() -> None:
    for path in (APP_DIR, QUEUE_DIR, DATA_DIR / "projects", LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
        with os.fdopen(fd, "wb") as handle:
            # A lone surrogate (an emoji cut in half) is legal JSON but not UTF-8.
            handle.write(text.encode("utf-8", "replace"))
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def load_dict(path: Path) -> dict[str, Any]:
    """Load a JSON object; anything else (missing, corrupt, a list) reads as {}."""
    value = load_json(path, {})
    return value if isinstance(value, dict) else {}


@contextlib.contextmanager
def locked_state(target: Path):
    """Read-modify-write a project's state.json under an exclusive lock.

    The worker, its scanners and explicit commands such as `mark` can update the
    same state concurrently; without the lock one would silently drop the
    other's processed events and fingerprints.
    """
    target.mkdir(parents=True, exist_ok=True)
    with (target / ".state.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = load_dict(target / "state.json")
            yield state
            atomic_json(target / "state.json", state)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load_config() -> dict[str, Any]:
    ensure_dirs()
    stored = load_json(CONFIG_PATH, {})
    merged = dict(DEFAULT_CONFIG)
    if isinstance(stored, dict):
        merged.update(stored)
    # A hand-edited value of the wrong type ("8s", null) must not crash the
    # worker or fail every record; it falls back to the default.
    for key, default in DEFAULT_CONFIG.items():
        if isinstance(default, bool):
            if not isinstance(merged.get(key), bool):
                merged[key] = default
        elif isinstance(default, (int, float)):
            try:
                merged[key] = type(default)(merged.get(key))
            except (TypeError, ValueError):
                merged[key] = default
        elif isinstance(default, str) and not isinstance(merged.get(key), str):
            merged[key] = default
    if not CONFIG_PATH.exists():
        atomic_json(CONFIG_PATH, merged)
    return merged


def run_git(cwd: Path, args: list[str], timeout: int = 12) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            # Read-only commands must not refresh .git/index in someone's repository.
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def git_root(cwd: str | Path) -> Path | None:
    candidate = Path(cwd).expanduser()
    if not candidate.exists():
        return None
    root = run_git(candidate, ["rev-parse", "--show-toplevel"])
    return Path(root).resolve() if root else None


def project_id(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]


def project_dir(root: Path) -> Path:
    return DATA_DIR / "projects" / project_id(root)


def remote_slug(root: Path) -> str:
    remote = run_git(root, ["remote", "get-url", "origin"])
    if not remote:
        return ""
    cleaned = re.sub(r"\.git$", "", remote.rstrip("/"))
    return cleaned.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def slugify(value: str) -> str:
    """Stable, filesystem-safe portfolio identifier."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return cleaned or "unnamed-project"


def register_project(
    root: Path,
    name: str = "",
    watch: bool | None = None,
    slug: str = "",
    enabled: bool | None = None,
) -> Path:
    """Record a repository in the external registry.

    Everything written here lives under the application support directory. A
    registered repository is only ever read, so registration cannot dirty the
    working tree of a repository shared with other contributors.
    """
    target = project_dir(root)
    target.mkdir(parents=True, exist_ok=True)
    metadata_path = target / "project.json"
    current = load_json(metadata_path, {})
    resolved_name = name or current.get("name") or remote_slug(root) or root.name
    current.update(
        {
            "id": project_id(root),
            "name": resolved_name,
            "portfolio_slug": slugify(slug or current.get("portfolio_slug") or resolved_name),
            "path": str(root),
            "remote": strip_userinfo(run_git(root, ["remote", "get-url", "origin"])),
            "registered_at": current.get("registered_at", now_iso()),
            "last_seen_at": now_iso(),
            "enabled": current.get("enabled", True) if enabled is None else enabled,
            "watch_worktree": current.get("watch_worktree", True) if watch is None else watch,
        }
    )
    atomic_json(metadata_path, current)
    state_path = target / "state.json"
    if not state_path.exists():
        atomic_json(
            state_path,
            {
                "last_head": run_git(root, ["rev-parse", "HEAD"]),
                "processed_events": [],
                "fingerprints": [],
            },
        )
    return target


def is_registered(root: Path) -> bool:
    """True when `root` was explicitly registered and is still enabled.

    Registration is the only thing that opts a repository in. An agent hook that
    fires in some unrelated repository must therefore capture nothing, which is
    what keeps this tool from quietly collecting history everywhere the user
    happens to run Claude or Codex.
    """
    metadata = load_json(project_dir(root) / "project.json", {})
    return bool(metadata) and bool(metadata.get("enabled", True))


def unregister_project(root: Path, purge: bool = False) -> tuple[bool, Path]:
    """Stop monitoring a repository.

    Only the watchdog's own external state is touched; the repository itself is
    never opened for writing. Captured history is kept unless `purge` is set, so
    an accidental unregister cannot destroy evidence.
    """
    target = project_dir(root)
    metadata_path = target / "project.json"
    existed = metadata_path.exists()
    if purge:
        shutil.rmtree(target, ignore_errors=True)
    elif existed:
        metadata_path.unlink()
    return existed, target


def clipped(value: Any, limit: int = MAX_TEXT) -> str:
    text = value if isinstance(value, str) else ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[clipped {len(text) - limit} characters]"


def redact_secrets(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any) -> Any:
    """Redact every string inside a JSON-like value."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    return value


def redact_clipped(value: Any, limit: int) -> str:
    """Redact before clipping, so a clip can never cut a secret in half."""
    return clipped(redact_secrets(value), limit)


def strip_userinfo(url: str) -> str:
    """Drop `user:password@` from a remote URL before it is stored anywhere."""
    return re.sub(r"^([a-z][a-z0-9+.-]*://)[^/@]*@", r"\1", url, flags=re.IGNORECASE)


def omit_sensitive_diffs(diff: str) -> str:
    """Replace the diff sections of credential-like files with a placeholder."""
    sections = re.split(r"(?m)^(?=diff --git )", diff)
    kept = []
    for section in sections:
        match = re.match(r"diff --git a/(\S+) b/(\S+)", section)
        if match and (
            SENSITIVE_NAME_RE.search(match.group(1)) or SENSITIVE_NAME_RE.search(match.group(2))
        ):
            kept.append(f"diff --git a/{match.group(1)} b/{match.group(2)}\n"
                        "[diff of a credential file omitted]\n")
        else:
            kept.append(section)
    return "".join(kept)


def queue_record(record: dict[str, Any]) -> Path:
    """Write one record into the queue for the worker to pick up.

    The worker drains the queue in filename order, and a prompt event must be
    seen before the turn it belongs to. Millisecond precision is not enough for
    that: two hooks can fire inside the same millisecond. The name therefore
    carries a zero-padded nanosecond stamp, which orders correctly both within a
    process and between the several processes that write here.
    """
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.time_ns()
    token = hashlib.sha256(
        f"{stamp}:{os.getpid()}:{record.get('session_id', '')}".encode()
    ).hexdigest()[:20]
    path = QUEUE_DIR / f"{stamp:020d}-{token}.json"
    atomic_json(path, record)
    return path


def enqueue(payload: dict[str, Any]) -> Path | None:
    root = git_root(payload.get("cwd") or os.getcwd())
    if not root or not is_registered(root):
        return None
    record = {
        "queued_at": now_iso(),
        "source": payload.pop("_watchdog_source", "unknown"),
        "project_root": str(root),
        "event": payload.get("hook_event_name", "unknown"),
        "session_id": payload.get("session_id", ""),
        "turn_id": payload.get("turn_id", ""),
        # Redacted at the door: queue, raw archive and session cache never hold
        # a secret, not only what is sent to the model.
        "prompt": redact_clipped(payload.get("prompt") or payload.get("user_prompt"), 20_000),
        "assistant": redact_clipped(payload.get("last_assistant_message"), 30_000),
        "reason": payload.get("reason", ""),
        "transcript_path": str(payload.get("transcript_path", "")),
    }
    return queue_record(record)


def hook_command(source: str) -> int:
    try:
        ensure_dirs()
        # Read everything: a truncated payload is invalid JSON and would be lost.
        # Fields are clipped after parsing.
        raw = sys.stdin.read(50_000_000)
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        payload["_watchdog_source"] = source
        enqueue(payload)
    except Exception as exc:  # hooks must never block the parent agent
        log_error("hook-errors.log", f"{source}: {exc}")
    # Stop hooks require valid JSON on stdout in both products.
    print('{"continue":true,"suppressOutput":true}')
    return 0


def read_history(target: Path) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    path = target / "history.jsonl"
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        history.append(value)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return history


def append_jsonl_safely(path: Path) -> None:
    """Terminate a torn last line, so the next entry is not glued onto it."""
    try:
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            torn = handle.read(1) != b"\n"
    except OSError:
        return
    if torn:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n")


def append_history(target: Path, entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    append_jsonl_safely(target / "history.jsonl")
    with (target / "history.jsonl").open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def git_context(root: Path, config: dict[str, Any], commit: str = "") -> dict[str, str]:
    max_chars = int(config.get("max_diff_chars", MAX_TEXT))
    if commit:
        diff = omit_sensitive_diffs(
            run_git(root, ["show", "--format=fuller", "--find-renames", commit], timeout=30)
        )
        stat = run_git(root, ["show", "--stat", "--oneline", commit])
    else:
        # Plumbing (`diff-files`, `diff-index`) instead of `git diff`: the porcelain
        # refreshes and rewrites .git/index even with GIT_OPTIONAL_LOCKS=0.
        unstaged = omit_sensitive_diffs(
            run_git(root, ["diff-files", "-p", "--find-renames"], timeout=30)
        )
        staged = omit_sensitive_diffs(
            run_git(root, ["diff", "--cached", "--find-renames"], timeout=30)
        )
        untracked_names = [
            name
            for name in run_git(root, ["ls-files", "-z", "--others", "--exclude-standard"]).split("\0")
            if name
        ]
        untracked_parts: list[str] = []
        remaining = min(20_000, max_chars // 3)
        for relative in untracked_names[:30]:
            if SENSITIVE_NAME_RE.search(relative):
                continue
            path = root / relative
            try:
                # A symlink can point outside the repository or at a credential
                # file under an innocent name, so links are never followed.
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 200_000:
                    continue
                content = path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeError):
                continue
            excerpt = redact_clipped(content, min(4_000, remaining))
            untracked_parts.append(f"--- UNTRACKED: {relative} ---\n{excerpt}")
            remaining -= len(excerpt)
            if remaining <= 0:
                break
        diff = (
            f"UNSTAGED\n{unstaged}\n\nSTAGED\n{staged}\n\n"
            f"UNTRACKED FILES\n{chr(10).join(untracked_parts)}"
        )
        stat = run_git(root, ["diff-index", "--stat", "HEAD"])
    return {
        "head": run_git(root, ["rev-parse", "HEAD"]),
        "branch": redact_secrets(run_git(root, ["branch", "--show-current"])),
        "status": redact_clipped(run_git(root, ["status", "--short"]), 10_000),
        "stat": redact_clipped(stat, 12_000),
        "diff": redact_clipped(diff, max_chars),
    }


def extraction_schema() -> dict[str, Any]:
    entry = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": EVENT_TYPES},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "context": {"type": "string"},
            "alternatives": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "array", "items": {"type": "string"}},
            "tradeoffs": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "files": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": [
            "type",
            "title",
            "summary",
            "context",
            "alternatives",
            "rationale",
            "tradeoffs",
            "evidence",
            "files",
            "tags",
            "confidence",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "worth_recording": {"type": "boolean"},
            "entries": {"type": "array", "items": entry, "maxItems": 4},
        },
        "required": ["worth_recording", "entries"],
        "additionalProperties": False,
    }


# Tuned against the labelled scenarios in eval/: the earlier, shorter prompt
# recorded informational answers, status reports and routine edits as
# decisions, with invented rationale and a confidence of 1.0.
SYSTEM_PROMPT = """You extract engineering history for a later portfolio case study.
Each entry must help a reader answer "why was it built this way?". Most inputs
contain nothing worth keeping: worth_recording=false with an empty entries
array is the normal, correct answer, and it costs nothing.

Record an entry ONLY for one of these, and only when it actually happened in
this input (carried out, agreed, or confirmed - not merely proposed):
- decision: a choice between real alternatives that was made or carried out
  (library, architecture, data model, hosting, protocol, algorithm). Also a
  deliberate decision NOT to build or fix something, and a requirement change
  that reshaped the work.
- constraint: an external limit that was discovered and shaped the work (API
  rate limit, platform timeout, budget, legal or data-residency rule).
- problem: a non-obvious bug together with its identified root cause.
- failed_attempt: an approach that was tried and abandoned, with the reason.
- milestone: first deploy, release, feature shipped, handover.
- discovery, lesson, metric: a non-obvious finding or measured result that
  changed what was built.

Do NOT record any of the following. If nothing else remains, return
worth_recording=false:
- answers to informational questions: where data is stored, sizes, how code
  works, what something does, whether it is ready;
- status or progress reports, to-do lists, plans, next steps;
- review findings, audit results or "issues I noticed" that nobody has acted
  on yet;
- recommendations or options the user has not accepted ("I'd lean towards X,
  let me know");
- debugging that ended without a root cause;
- routine work: renames, formatting, tests, dependency bumps, small fixes,
  helper functions, or implementation details of a larger decision;
- anything already covered by recent_existing_entries, even if worded
  differently. A commit that implements an already-recorded decision adds
  nothing. A REVERSAL of an existing entry is new: record it and name the
  earlier choice it replaces.

Judge by what the diff and conversation show, not by labels: a commit message
can be wrong. In a long answer, keep only the parts that meet the bar.

Fill alternatives, rationale and tradeoffs ONLY with what the evidence states:
- alternatives: options the evidence names as considered, rejected or tried.
  Never list the chosen option itself, "keep the old way", or options you
  thought of yourself.
- rationale: reasons the evidence gives. Each item must be traceable to a
  sentence in the prompt, answer, commit message or note. No reasons from
  general knowledge ("X is more secure", "standard practice", "easier").
Empty lists are correct and expected when the evidence does not say. A
decision whose evidence gives neither a reason nor an alternative is usually
routine work, not a decision.

Usually 0 or 1 entries; 2 only for genuinely separate events (for example a
constraint and the decision it forced). Never one entry per detail.

Write every field in concise English, even when the user writes in another
language such as Slovak.

confidence = probability that this entry is a real, case-study-worthy event
of the stated type that a reader would want in the project history:
- 0.9-1.0: explicitly carried out, alternatives or reasons stated in evidence;
- 0.7-0.85: clearly happened, but the reason is only implied;
- 0.4-0.6: borderline: might be routine, informational or not yet decided;
- below 0.4: probably not worth recording.
Do not default to 1.0."""


# No proxy: an `http_proxy` in the environment would otherwise receive every
# evidence packet, even for 127.0.0.1.
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_loopback_url(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def ollama_chat(config: dict[str, Any], messages: list[dict[str, str]], schema: Any) -> str:
    if not is_loopback_url(str(config["ollama_url"])):
        raise RuntimeError(
            f"Refusing to send evidence to a non-local ollama_url: {config['ollama_url']}"
        )
    body = {
        "model": config["model"],
        "messages": messages,
        "stream": False,
        "format": schema,
        "options": {"temperature": 0.1},
        "keep_alive": "20m",
    }
    request = urllib.request.Request(
        config["ollama_url"],
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with LOCAL_OPENER.open(request, timeout=900) as response:
        outer = json.load(response)
    message = outer.get("message") if isinstance(outer, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ValueError("Ollama returned no message content")
    return content


def normalized_fingerprint(entry: dict[str, Any]) -> str:
    basis = f"{entry.get('type','')}|{entry.get('title','')}|{entry.get('summary','')}"
    basis = re.sub(r"\W+", " ", basis.lower()).strip()
    return hashlib.sha256(basis.encode()).hexdigest()[:20]


class OllamaUnavailable(RuntimeError):
    """Ollama is not running. Not the record's fault, so not a failed attempt."""


ENTRY_TEXT_LIMITS = {"title": 200, "summary": 2_000, "context": 2_000}
MAX_ENTRIES_PER_RECORD = 8


def normalize_entry(extracted: Any) -> dict[str, Any] | None:
    """Validate one model-extracted entry; None drops only that entry."""
    if not isinstance(extracted, dict):
        return None
    try:
        confidence = float(extracted.get("confidence"))
    except (TypeError, ValueError):
        return None
    if extracted.get("type") not in EVENT_TYPES:
        return None
    # Model output is untrusted and may quote a secret back.
    entry = redact_value(extracted)
    for key, limit in ENTRY_TEXT_LIMITS.items():
        value = entry.get(key)
        entry[key] = clipped(value if isinstance(value, str) else "", limit).strip()
    entry["title"] = " ".join(entry["title"].split())
    if not entry["title"]:
        return None
    for key in ("alternatives", "rationale", "tradeoffs", "evidence", "files", "tags"):
        values = entry.get(key)
        entry[key] = [clipped(str(v), 500) for v in values[:20]] if isinstance(values, list) else []
    entry["confidence"] = max(0.0, min(1.0, confidence))
    return entry


def analyze_record(record: dict[str, Any], commit: str = "") -> int:
    config = load_config()
    project_root = str(record.get("project_root", ""))
    root = git_root(project_root)
    if not root:
        if project_root and Path(project_root).is_dir() and not shutil.which("git"):
            # Retry later instead of silently discarding the record.
            raise RuntimeError("git is not available")
        if project_root and not Path(project_root).exists():
            log_error("worker-errors.log", f"repository no longer exists: {project_root}")
        return 0
    target = register_project(root)
    state = load_dict(target / "state.json")
    # Queue bookkeeping (`_attempts`, `_last_error`) must not change the
    # identity of a record, or a retried record would be analyzed twice.
    identity = {key: value for key, value in record.items() if not key.startswith("_")}
    unique_key = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]
    if unique_key in state.get("processed_events", []):
        return 0

    git_info = git_context(root, config, commit=commit)
    history = read_history(target)
    recent = [
        {
            "type": x.get("type"),
            "title": clipped(x.get("title"), 200),
            "summary": clipped(x.get("summary"), 600),
        }
        for x in history[-25:]
    ]
    evidence_packet = {
        "project": redact_value(load_json(target / "project.json", {})),
        "source": record.get("source"),
        "hook_event": record.get("event"),
        "timestamp": record.get("queued_at"),
        "explicit_note": redact_secrets(record.get("explicit_note", "")),
        "user_prompt": redact_secrets(clipped(record.get("prompt"), 20_000)),
        "assistant_final_message": redact_secrets(clipped(record.get("assistant"), 30_000)),
        "git": {key: redact_secrets(value) for key, value in git_info.items()},
        "commit_being_analyzed": commit,
        "recent_existing_entries": recent,
    }
    prompt = (
        "Extract only new events that are not duplicates of recent entries. "
        "Every entry must cite concrete evidence visible in this packet.\n\n"
        + json.dumps(evidence_packet, ensure_ascii=False, indent=2)
    )
    try:
        content = ollama_chat(
            config,
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            extraction_schema(),
        )
        result = json.loads(content)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (ConnectionRefusedError, FileNotFoundError)):
            raise OllamaUnavailable(f"Ollama is not running: {exc}") from exc
        raise RuntimeError(f"Ollama analysis failed: {exc}") from exc
    except (TimeoutError, ValueError, OSError) as exc:
        raise RuntimeError(f"Ollama analysis failed: {exc}") from exc
    if not isinstance(result, dict):
        result = {}

    metadata = load_dict(target / "project.json")
    if not metadata:
        # Unregistered with --purge while the model was running.
        return 0
    candidates: list[dict[str, Any]] = []
    entries = result.get("entries")
    if result.get("worth_recording") is True and isinstance(entries, list):
        for extracted in entries[:MAX_ENTRIES_PER_RECORD]:
            entry = normalize_entry(extracted)
            if not entry or entry["confidence"] < float(config["min_confidence"]):
                continue
            # A "decision" that names neither a reason nor an alternative is
            # almost always routine work the model over-labelled. Milestones and
            # root-caused bugs legitimately have neither, so only decisions.
            if entry["type"] == "decision" and not entry["alternatives"] and not entry["rationale"]:
                continue
            candidates.append(entry)

    accepted: list[dict[str, Any]] = []
    # Merge into the current state under the lock: another analyzer may have
    # finished while the model was running.
    with locked_state(target) as state:
        if unique_key in state.get("processed_events", []):
            return 0
        fingerprints = list(state.get("fingerprints", []))
        for extracted in candidates:
            fingerprint = normalized_fingerprint(extracted)
            if fingerprint in fingerprints:
                continue
            event_id = f"evt-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{fingerprint[:6]}"
            accepted.append(
                {
                    "id": event_id,
                    "timestamp": now_iso(),
                    "project": metadata.get("name", root.name),
                    "project_slug": metadata.get("portfolio_slug", slugify(root.name)),
                    "repo_path": str(root),
                    "source": record.get("source", "unknown"),
                    "session_id": record.get("session_id", ""),
                    "type": extracted.get("type"),
                    "title": extracted.get("title", "").strip(),
                    "summary": extracted.get("summary", "").strip(),
                    "context": extracted.get("context", "").strip(),
                    "alternatives": extracted.get("alternatives", []),
                    "rationale": extracted.get("rationale", []),
                    "tradeoffs": extracted.get("tradeoffs", []),
                    "evidence": extracted.get("evidence", []),
                    "files": extracted.get("files", []),
                    "git_commit": commit or git_info.get("head", ""),
                    "branch": git_info.get("branch", ""),
                    "tags": extracted.get("tags", []),
                    "confidence": extracted.get("confidence"),
                    "status": "captured",
                    "fingerprint": fingerprint,
                }
            )
            fingerprints.append(fingerprint)

        append_history(target, accepted)
        processed = list(state.get("processed_events", [])) + [unique_key]
        state["processed_events"] = processed[-2000:]
        state["fingerprints"] = fingerprints[-5000:]
        # `last_head` belongs to the commit scanner alone. Advancing it here hid
        # every commit an agent made during a turn from that scanner.
        state["last_analysis_at"] = now_iso()
    if accepted:
        # The entries are safely stored; a failing export must not turn the
        # record into a retry that runs the model a second time.
        try:
            render_project(target, config)
        except Exception as exc:
            log_error("worker-errors.log", f"render/export for {root}: {exc}")
            with locked_state(target) as state:
                state["render_pending"] = True
    return len(accepted)


def retry_pending_renders(config: dict[str, Any]) -> None:
    """Re-run rendering and export that failed after entries were stored."""
    for target, _metadata in registered_projects():
        if not load_dict(target / "state.json").get("render_pending"):
            continue
        try:
            render_project(target, config)
        except Exception as exc:
            log_error("worker-errors.log", f"render/export retry for {target.name}: {exc}")
            continue
        with locked_state(target) as state:
            state.pop("render_pending", None)


def md_text(value: Any) -> str:
    """Neutralise model output before it becomes Markdown.

    Images and raw HTML would make Obsidian or a Markdown preview fetch a URL
    the model chose, so they are rendered as plain text.
    """
    return str(value if value is not None else "").replace("<", "&lt;").replace("![", "!\\[")


def md_list(values: Any, fallback: str = "None recorded") -> str:
    if not isinstance(values, list) or not values:
        return f"- {fallback}"
    items = [" ".join(md_text(item).split()) for item in values]
    return "\n".join(f"- {item}" for item in items if item) or f"- {fallback}"


def render_project(target: Path, config: dict[str, Any] | None = None) -> None:
    config = config or load_config()
    metadata = load_json(target / "project.json", {})
    history = read_history(target)
    grouped: dict[str, list[dict[str, Any]]] = {kind: [] for kind in EVENT_TYPES}
    for event in history:
        grouped.setdefault(str(event.get("type", "other")), []).append(event)

    decisions = [f"# {metadata.get('name', 'Project')} - Engineering history", ""]
    decisions.append(
        "> Generated from an append-only local evidence log. Review before external publication.\n"
    )
    for kind in EVENT_TYPES:
        items = grouped.get(kind, [])
        if not items:
            continue
        decisions.extend([f"## {kind.replace('_', ' ').title()}", ""])
        for event in reversed(items):
            decisions.extend(
                [
                    f"### {' '.join(md_text(event.get('title') or 'Untitled').split())}",
                    "",
                    f"- Date: {event.get('timestamp', '')}",
                    f"- Source: {event.get('source', '')}",
                    f"- Commit: `{event.get('git_commit')}`"
                    if event.get("git_commit")
                    else "- Commit: (uncommitted)",
                    f"- Confidence: {event.get('confidence', '')}",
                    "",
                    md_text(event.get("summary", "")),
                    "",
                    f"Context: {md_text(event.get('context', ''))}",
                    "",
                    "Alternatives:",
                    md_list(event.get("alternatives")),
                    "",
                    "Rationale:",
                    md_list(event.get("rationale")),
                    "",
                    "Trade-offs:",
                    md_list(event.get("tradeoffs")),
                    "",
                    "Evidence:",
                    md_list(event.get("evidence")),
                    "",
                ]
            )
    (target / "Decisions.md").write_text("\n".join(decisions).rstrip() + "\n", encoding="utf-8")

    timeline = [f"# {metadata.get('name', 'Project')} - Timeline", ""]
    for event in history:
        commit = str(event.get("git_commit") or "")[:8]
        timeline.append(
            f"- {event.get('timestamp', '')[:10]} - **{event.get('type', '')}** - "
            f"{' '.join(md_text(event.get('title', '')).split())}"
            + (f" (`{commit}`)" if commit else "")
        )
    (target / "Timeline.md").write_text("\n".join(timeline).rstrip() + "\n", encoding="utf-8")
    sync_obsidian(target, config)
    export_portfolio(target, config)


def portfolio_archive_dir(config: dict[str, Any]) -> Path | None:
    value = str(config.get("portfolio_archive", "")).strip()
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_dir() else None


def portfolio_project_dir(config: dict[str, Any], metadata: dict[str, Any]) -> Path | None:
    archive = portfolio_archive_dir(config)
    if archive is None:
        return None
    slug = slugify(metadata.get("portfolio_slug") or metadata.get("name") or "unnamed-project")
    return archive / str(config.get("portfolio_projects_dir", "projects")) / slug


def export_portfolio(target: Path, config: dict[str, Any]) -> Path | None:
    """Mirror distilled evidence for one project into the central archive.

    Only the watchdog's own sub-directory is written. The curated files an
    archive project already owns - `project.yaml`, `case-study.md`, `decisions/`
    - are never created or modified here: turning captured evidence into a
    polished case study stays a deliberate manual step.
    """
    metadata = load_json(target / "project.json", {})
    project_root_dir = portfolio_project_dir(config, metadata)
    if project_root_dir is None:
        return None
    destination = project_root_dir / str(config.get("portfolio_evidence_dir", "private/watchdog"))
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log_error("export-errors.log", f"portfolio archive not writable: {exc}")
        return None

    evidence_path = destination / "evidence.jsonl"
    known_ids: set[str] = set()
    known_fingerprints: set[str] = set()
    try:
        with evidence_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    known_ids.add(str(value.get("id", "")))
                    if value.get("fingerprint"):
                        known_fingerprints.add(str(value["fingerprint"]))
    except FileNotFoundError:
        pass

    # An empty id or fingerprint must never match another empty one, or the
    # first entry that lacks one would suppress every later entry.
    fresh = []
    for entry in read_history(target):
        identifier = str(entry.get("id", ""))
        fingerprint = str(entry.get("fingerprint", ""))
        if identifier and identifier in known_ids:
            continue
        if fingerprint and fingerprint in known_fingerprints:
            continue
        fresh.append(entry)
        if identifier:
            known_ids.add(identifier)
        if fingerprint:
            known_fingerprints.add(fingerprint)
    if fresh:
        append_jsonl_safely(evidence_path)
        with evidence_path.open("a", encoding="utf-8") as handle:
            for entry in fresh:
                handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")

    for filename in ("Decisions.md", "Timeline.md", "Case Study.md"):
        source = target / filename
        if source.exists():
            shutil.copy2(source, destination / filename)

    atomic_json(
        destination / "source.json",
        {
            "generated_by": f"Decision Watchdog {VERSION}",
            "generated_at": now_iso(),
            "project": metadata.get("name", ""),
            "portfolio_slug": metadata.get("portfolio_slug", ""),
            "repository_path": metadata.get("path", ""),
            "repository_remote": strip_userinfo(str(metadata.get("remote", ""))),
            "entries": len(known_ids),
            "note": (
                "Generated from locally captured evidence. The tracked repository is "
                "read-only to the watchdog and contains no portfolio metadata."
            ),
        },
    )
    return destination


def safe_note_dir_name(name: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]", "-", name).strip(" .")
    return cleaned or "Unnamed Project"


def sync_obsidian(target: Path, config: dict[str, Any]) -> None:
    vault_value = str(config.get("obsidian_vault", "")).strip()
    if not vault_value:
        return
    vault = Path(vault_value).expanduser()
    if not vault.exists():
        return
    metadata = load_json(target / "project.json", {})
    destination = vault / str(config.get("obsidian_projects_dir", "10 Projects"))
    destination /= safe_note_dir_name(str(metadata.get("name", "Project")))
    destination.mkdir(parents=True, exist_ok=True)
    for filename in ("Decisions.md", "Timeline.md", "Case Study.md"):
        source = target / filename
        if source.exists():
            shutil.copy2(source, destination / filename)


def process_queue_once() -> tuple[int, int]:
    processed = 0
    captured = 0
    ensure_dirs()
    for path in sorted(QUEUE_DIR.glob("*.json")):
        processing = path.with_suffix(".processing")
        try:
            path.rename(processing)
        except FileNotFoundError:
            continue
        # The claim time, not the queue time, decides when a claim is stale.
        try:
            os.utime(processing)
        except OSError:
            pass
        try:
            record = load_dict(processing)
            if not record.get("project_root"):
                raise ValueError("unreadable queue record")
            target = project_dir(Path(record["project_root"]))
            context_path = target / "session-context.json"
            contexts = load_dict(context_path)
            session_id = str(record.get("session_id", ""))
            if record.get("event") == "UserPromptSubmit" and session_id:
                contexts[session_id] = {
                    "prompt": record.get("prompt", ""),
                    "updated_at": record.get("queued_at", now_iso()),
                }
                # Keep this cache bounded even for long-lived installations.
                if len(contexts) > 200:
                    oldest = sorted(
                        contexts,
                        key=lambda key: str(contexts[key].get("updated_at", "")),
                    )[: len(contexts) - 200]
                    for key in oldest:
                        contexts.pop(key, None)
                atomic_json(context_path, contexts)
            elif session_id and not record.get("prompt"):
                context = contexts.get(session_id)
                record["prompt"] = context.get("prompt", "") if isinstance(context, dict) else ""
                # Persist it: if analysis fails, the retry must see this turn's
                # prompt, not whatever prompt the session has moved on to.
                atomic_json(processing, record)
            if record.get("event") in {
                "Stop",
                "SessionEnd",
                "manual",
                "commit",
                "worktree",
            } and is_registered(Path(record["project_root"])):
                # A record queued just before an unregister must not resurrect
                # the project.
                captured += analyze_record(record, commit=str(record.get("commit", "")))
            archive_root = target / "raw"
            archive_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(processing), archive_root / processing.with_suffix(".json").name)
            processed += 1
        except OllamaUnavailable as exc:
            park_failed(processing, exc, count_attempt=False)
            log_error("worker-errors.log", f"{processing.name}: {exc}")
            # Every other record would fail the same way; leave them queued.
            break
        except Exception as exc:
            park_failed(processing, exc)
            log_error("worker-errors.log", f"{processing.name}: {exc}")
    return processed, captured


# A failure is usually transient (Ollama not running, model still loading), so
# a parked record is retried with exponential backoff. After the last attempt
# it stays parked until `decision-watchdog retry` requeues it by hand.
RETRY_BASE_SECONDS = 120
RETRY_MAX_SECONDS = 6 * 3600
MAX_ATTEMPTS = 8
# Longer than the Ollama request timeout, so a slow analysis is never stolen.
STALE_PROCESSING_SECONDS = 2 * 3600


def retry_delay(attempts: int) -> int:
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** max(0, attempts - 1))


def attempts_of(record: dict[str, Any], default: int) -> int:
    try:
        return int(record.get("_attempts", default))
    except (TypeError, ValueError):
        return default


def park_failed(processing: Path, exc: Exception, count_attempt: bool = True) -> None:
    """Move a record that failed analysis aside and count the attempt.

    An Ollama outage is not counted: a weekend with Ollama stopped must not use
    up every record's attempts.
    """
    failed = processing.with_suffix(".failed")
    record = load_json(processing, None)
    if isinstance(record, dict):
        record["_attempts"] = attempts_of(record, 0) + (1 if count_attempt else 0)
        record["_last_error"] = clipped(str(exc), 500)
        try:
            atomic_json(failed, record)
            processing.unlink()
            return
        except OSError:
            pass
    try:
        processing.rename(failed)
    except OSError:
        pass


def requeue_failed(now: float | None = None, force: bool = False) -> int:
    """Return parked records to the queue once their backoff has elapsed.

    `force` ignores both the backoff and the attempt limit; it backs the manual
    `retry` command. Unreadable records are only ever requeued by hand.
    """
    now = time.time() if now is None else now
    requeued = 0
    for failed in sorted(QUEUE_DIR.glob("*.failed")):
        record = load_json(failed, None)
        if not force:
            if not isinstance(record, dict):
                continue
            attempts = max(1, attempts_of(record, 1))
            if attempts >= MAX_ATTEMPTS:
                continue
            try:
                if failed.stat().st_mtime + retry_delay(attempts) > now:
                    continue
            except OSError:
                continue
        try:
            failed.rename(failed.with_suffix(".json"))
            requeued += 1
        except OSError:
            pass
    return requeued


def recover_stale_processing(
    now: float | None = None, max_age: float = STALE_PROCESSING_SECONDS
) -> int:
    """Requeue records whose worker died between claiming and finishing them.

    Analysis is idempotent per record, so replaying one that had in fact been
    analyzed costs one skipped lookup, never a duplicate history entry.
    """
    now = time.time() if now is None else now
    recovered = 0
    for processing in sorted(QUEUE_DIR.glob("*.processing")):
        try:
            if processing.stat().st_mtime + max_age > now:
                continue
            processing.rename(processing.with_suffix(".json"))
            recovered += 1
        except OSError:
            pass
    return recovered


def queue_counts() -> dict[str, int]:
    return {
        state: len(list(QUEUE_DIR.glob(f"*.{state}")))
        for state in ("json", "processing", "failed")
    }


def retry_command() -> int:
    ensure_dirs()
    count = requeue_failed(force=True)
    print(f"Requeued {count} failed record{'' if count == 1 else 's'}.")
    if count:
        print("The background worker will analyze them on its next pass.")
    return 0


def registered_projects() -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    for metadata_path in sorted((DATA_DIR / "projects").glob("*/project.json")):
        found.append((metadata_path.parent, load_json(metadata_path, {})))
    return found


def scan_commits() -> int:
    """Queue the user's own commits that appeared since the last scan.

    Detection is deliberately separate from analysis: the scanner only writes
    queue records, so a slow local model can never stall commit monitoring.
    Only commits that are new to this repository, authored by the configured
    Git user and committed after the previous scan count: checking out an old
    branch or pulling a teammate's work is not new work of yours.
    """
    queued = 0
    for target, metadata in registered_projects():
        if not metadata.get("enabled", True):
            continue
        root = git_root(metadata.get("path", ""))
        if not root:
            continue
        head = run_git(root, ["rev-parse", "--verify", "--quiet", "HEAD"])
        with locked_state(target) as state:
            previous = str(state.get("last_head", ""))
            last_scan = str(state.get("last_commit_scan_at", ""))
            state["last_commit_scan_at"] = now_iso()
            if not head or head == previous:
                continue
            state["last_head"] = head
            # The very first scan only establishes a baseline, unless an earlier
            # scan saw the repository before its first commit.
            if not previous and not last_scan:
                continue
            args = ["rev-list", "--reverse", "--no-merges", head]
            if previous and run_git(root, ["rev-parse", "--verify", "--quiet", f"{previous}^{{commit}}"]):
                args += ["--not", previous]
            if last_scan:
                args.append(f"--since={last_scan}")
            email = run_git(root, ["config", "user.email"])
            if email:
                args.append(f"--author=<{re.escape(email)}>")
            commits = run_git(root, args).splitlines()
            if len(commits) > MAX_COMMITS_PER_SCAN:
                log_error(
                    "worker-errors.log",
                    f"{root}: {len(commits)} new commits, analyzing the newest {MAX_COMMITS_PER_SCAN}",
                )
            for commit in commits[-MAX_COMMITS_PER_SCAN:]:
                queue_record(
                    {
                        "queued_at": now_iso(),
                        "source": "git",
                        "project_root": str(root),
                        "event": "commit",
                        "session_id": "",
                        "turn_id": "",
                        "prompt": "",
                        "assistant": "",
                        "commit": commit,
                    }
                )
                queued += 1
            # A commit resets the working-tree watcher: whatever was pending is
            # now recorded as part of the commit instead.
            state.pop("worktree_signature", None)
            state.pop("worktree_changed_at", None)
            state["analyzed_worktree_signature"] = ""
    return queued


WORKTREE_HASH_MAX_BYTES = 1_000_000
MAX_COMMITS_PER_SCAN = 20
WORKTREE_MAX_FILES = 200


def worktree_signature(root: Path) -> str:
    """Content-accurate fingerprint of the uncommitted state of a repository.

    Empty string means a clean tree. `git status` alone is not enough: an
    untracked file reports as `?? name` no matter how its contents change, so the
    debounce window would expire while the file was still being edited. Small
    files are therefore hashed; oversized ones fall back to their size.
    """
    status = run_git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=normal"])
    if not status.strip("\0").strip():
        return ""
    digest = hashlib.sha256(status.encode())
    # -z keeps non-ASCII and space-containing names verbatim. A rename entry is
    # followed by an extra field holding the old path, which is skipped.
    fields = status.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4:
            continue
        paths.append(field[3:])
        if field[0] in "RC" or field[1] in "RC":
            index += 1
    for relative in paths[:WORKTREE_MAX_FILES]:
        if SENSITIVE_NAME_RE.search(relative):
            digest.update(f"{relative}:sensitive".encode())
            continue
        path = root / relative
        try:
            info = path.stat()
            if not path.is_file():
                digest.update(f"{relative}:dir:{int(info.st_mtime)}".encode())
            elif info.st_size <= WORKTREE_HASH_MAX_BYTES:
                digest.update(f"{relative}:".encode())
                digest.update(hashlib.sha256(path.read_bytes()).digest())
            else:
                digest.update(f"{relative}:{info.st_size}:{int(info.st_mtime)}".encode())
        except OSError:
            digest.update(f"{relative}:missing".encode())
    return digest.hexdigest()[:24]


def scan_worktrees(config: dict[str, Any] | None = None, now: float | None = None) -> int:
    """Queue analysis for working trees that changed and then went quiet.

    A project is analyzed when its fingerprint has stopped moving for
    `worktree_debounce_seconds`, differs from the last analyzed fingerprint, and
    the project was not analyzed within `worktree_min_interval_seconds`.
    """
    config = config or load_config()
    if not config.get("worktree_watch", True):
        return 0
    now = time.time() if now is None else now
    debounce = float(config.get("worktree_debounce_seconds", 300))
    min_interval = float(config.get("worktree_min_interval_seconds", 1800))
    queued = 0
    for target, metadata in registered_projects():
        if not metadata.get("enabled", True) or not metadata.get("watch_worktree", True):
            continue
        root = git_root(metadata.get("path", ""))
        if not root:
            continue
        signature = worktree_signature(root)
        with locked_state(target) as state:
            if signature != state.get("worktree_signature", ""):
                # Still moving. Restart the debounce window and wait.
                state["worktree_signature"] = signature
                state["worktree_changed_at"] = now
                continue
            if not signature:
                continue
            changed_at = float(state.get("worktree_changed_at") or now)
            if now - changed_at < debounce:
                continue
            if signature == state.get("analyzed_worktree_signature", ""):
                continue
            last_run = float(state.get("worktree_analyzed_at") or 0.0)
            if last_run and now - last_run < min_interval:
                continue
            queue_record(
                {
                    "queued_at": now_iso(),
                    "source": "worktree",
                    "project_root": str(root),
                    "event": "worktree",
                    "session_id": "",
                    "turn_id": "",
                    "prompt": "",
                    "assistant": "",
                    "worktree_signature": signature,
                }
            )
            state["analyzed_worktree_signature"] = signature
            state["worktree_analyzed_at"] = now
            queued += 1
    return queued


def prune_raw(config: dict[str, Any]) -> None:
    """Delete raw captured text older than `raw_retention_days`.

    That covers archived queue records, records that never stopped failing, and
    the cached prompts in `session-context.json`.
    """
    cutoff = time.time() - int(config.get("raw_retention_days", 30)) * 86400
    paths = [*(DATA_DIR / "projects").glob("*/raw/*.json"), *QUEUE_DIR.glob("*.failed")]
    for path in paths:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass
    for context_path in (DATA_DIR / "projects").glob("*/session-context.json"):
        contexts = load_json(context_path, {})
        if not isinstance(contexts, dict):
            continue
        kept = {
            key: value
            for key, value in contexts.items()
            if isinstance(value, dict) and iso_timestamp(value.get("updated_at")) >= cutoff
        }
        if len(kept) != len(contexts):
            atomic_json(context_path, kept)


def iso_timestamp(value: Any) -> float:
    try:
        return dt.datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0


def worker() -> int:
    ensure_dirs()
    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    lock = (APP_DIR / ".worker.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another Decision Watchdog worker is already running.", file=sys.stderr)
        return 0
    # Holding the lock means no other worker exists, so every claim on disk
    # belongs to a worker that died. Recover them now instead of in two hours.
    recover_stale_processing(max_age=0)
    last_commit_scan = 0.0
    last_worktree_scan = 0.0
    while not stop:
        try:
            recover_stale_processing()
            requeue_failed()
        except Exception as exc:
            log_error("worker-errors.log", f"queue recovery: {exc}")
        try:
            process_queue_once()
        except Exception as exc:
            log_error("worker-errors.log", f"queue: {exc}")
        config = load_config()
        if time.time() - last_commit_scan > 30:
            try:
                scan_commits()
                prune_raw(config)
                retry_pending_renders(config)
            except Exception as exc:
                log_error("worker-errors.log", f"commit scan: {exc}")
            last_commit_scan = time.time()
        if time.time() - last_worktree_scan > max(10, int(config.get("worktree_poll_seconds", 45))):
            try:
                scan_worktrees(config)
            except Exception as exc:
                log_error("worker-errors.log", f"worktree scan: {exc}")
            last_worktree_scan = time.time()
        time.sleep(max(2, int(config.get("poll_seconds", 8))))
    return 0


def current_project_or_exit() -> tuple[Path, Path]:
    root = git_root(Path.cwd())
    if not root:
        raise SystemExit("Run this command inside a Git repository.")
    if not is_registered(root):
        raise SystemExit(
            "This repository is not registered. Run 'decision-watchdog register' first."
        )
    return root, register_project(root)


def manual_mark(note: str) -> int:
    root, _target = current_project_or_exit()
    record = {
        "queued_at": now_iso(),
        "source": "manual",
        "project_root": str(root),
        "event": "manual",
        "session_id": "manual",
        "turn_id": "",
        "prompt": "",
        "assistant": "",
        "explicit_note": note,
    }
    count = analyze_record(record)
    print(f"Captured {count} new history entr{'y' if count == 1 else 'ies'}.")
    return 0


def analyze_now() -> int:
    root, _target = current_project_or_exit()
    record = {
        "queued_at": now_iso(),
        "source": "manual-analysis",
        "project_root": str(root),
        "event": "manual",
        "session_id": "manual",
        "turn_id": "",
        "prompt": "Analyze the current working tree for meaningful engineering history.",
        "assistant": "",
    }
    count = analyze_record(record)
    print(f"Captured {count} new history entr{'y' if count == 1 else 'ies'}.")
    return 0


def generate_case_study() -> int:
    root, target = current_project_or_exit()
    config = load_config()
    metadata = load_json(target / "project.json", {})
    history = read_history(target)
    if not history:
        raise SystemExit("No captured history exists for this project yet.")
    packet = {
        "project": redact_value(metadata),
        "readme_excerpt": redact_secrets(clipped((root / "README.md").read_text(encoding="utf-8", errors="replace"), 20_000))
        if (root / "README.md").exists()
        else "",
        "history": history,
        "git_log": redact_clipped(run_git(root, ["log", "--oneline", "--decorate", "-40"]), 12_000),
    }
    prompt = """Write a truthful SWE portfolio case-study draft in Markdown from the
provided evidence. Use these sections: Problem, Context and constraints, Approach,
Key engineering decisions, Problems and failed approaches, Implementation,
Results, Lessons, and What I would improve. Do not invent metrics, ownership,
team size, users, or outcomes. Mark genuinely missing result data with a short
[ADD EVIDENCE] placeholder. Focus on reasoning and trade-offs, not marketing.

EVIDENCE:
""" + json.dumps(packet, ensure_ascii=False, indent=2)
    case_study_schema = {
        "type": "object",
        "properties": {"markdown": {"type": "string"}},
        "required": ["markdown"],
        "additionalProperties": False,
    }
    content = ollama_chat(
        config,
        [
            {"role": "system", "content": "You write evidence-grounded software engineering case studies."},
            {"role": "user", "content": prompt},
        ],
        case_study_schema,
    )
    try:
        parsed = json.loads(content)
        markdown = parsed.get("markdown") or parsed.get("content") or content
    except json.JSONDecodeError:
        markdown = content
    if not markdown.lstrip().startswith("#"):
        markdown = f"# {metadata.get('name', root.name)} - Case Study\n\n" + markdown
    (target / "Case Study.md").write_text(markdown.rstrip() + "\n", encoding="utf-8")
    sync_obsidian(target, config)
    export_portfolio(target, config)
    print(target / "Case Study.md")
    return 0


def configure(args: argparse.Namespace) -> int:
    config = load_config()
    if args.model:
        config["model"] = args.model
    if args.vault is not None:
        path = Path(args.vault).expanduser().resolve() if args.vault else None
        if path and not path.exists():
            raise SystemExit(f"Vault path does not exist: {path}")
        config["obsidian_vault"] = str(path) if path else ""
    if args.archive is not None:
        path = Path(args.archive).expanduser().resolve() if args.archive else None
        if path and not path.is_dir():
            raise SystemExit(f"Portfolio archive directory does not exist: {path}")
        config["portfolio_archive"] = str(path) if path else ""
    atomic_json(CONFIG_PATH, config)
    for target in (DATA_DIR / "projects").glob("*"):
        if target.is_dir():
            render_project(target, config)
    print(f"Model: {config['model']}")
    print(f"Obsidian vault: {config.get('obsidian_vault') or '(not configured)'}")
    print(f"Portfolio archive: {config.get('portfolio_archive') or '(not configured)'}")
    return 0


def ollama_models() -> list[str]:
    try:
        parts = urllib.parse.urlsplit(str(load_config().get("ollama_url", "")))
        if not is_loopback_url(parts.geturl()):
            return []
        request = urllib.request.Request(f"{parts.scheme}://{parts.netloc}/api/tags")
        with LOCAL_OPENER.open(request, timeout=3) as response:
            result = json.load(response)
        return [m.get("name", "") for m in result.get("models", []) if m.get("name")]
    except Exception:
        return []


CODEX_HOOKS_PATH = Path.home() / ".codex" / "hooks.json"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
HOOK_EVENTS = ("UserPromptSubmit", "Stop", "SessionEnd")


def installed_hook_commands(path: Path) -> dict[str, list[str]]:
    """Map hook event -> our command strings configured in `path`."""
    settings = load_json(path, {})
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    found: dict[str, list[str]] = {}
    if not isinstance(hooks, dict):
        return found
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                command = str(handler.get("command", "")) if isinstance(handler, dict) else ""
                if "decision-watchdog" in command or "Decision Watchdog" in command:
                    found.setdefault(str(event), []).append(command)
    return found


def codex_hook_report(cwd: Path, timeout: int = 45) -> tuple[list[dict[str, Any]], str]:
    """Ask Codex itself which of our hooks it loaded and whether they are trusted.

    Codex silently skips a command hook whose `trustStatus` is not `trusted`, so
    the presence of an entry in hooks.json proves nothing on its own.
    """
    if not shutil.which("codex"):
        return [], "codex executable not found"
    try:
        proc = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        return [], f"could not start codex app-server: {exc}"
    try:
        assert proc.stdin and proc.stdout
        for message in (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "decision-watchdog",
                        "title": "Decision Watchdog",
                        "version": VERSION,
                    }
                },
            },
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "hooks/list", "params": {"cwds": [str(cwd)]}},
        ):
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()

        result: dict[str, Any] = {}
        deadline = time.time() + timeout

        def reader() -> None:
            for line in proc.stdout:  # type: ignore[union-attr]
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") == 2:
                    result.update(message)
                    return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(max(1.0, deadline - time.time()))
        if not result:
            return [], "codex app-server did not answer hooks/list in time"
        if "error" in result:
            return [], f"codex hooks/list failed: {result['error']}"
        entries = result.get("result", {}).get("data", [])
        hooks = [
            hook
            for entry in entries
            if isinstance(entry, dict)
            for hook in entry.get("hooks", [])
            if isinstance(hook, dict) and "decision-watchdog" in str(hook.get("command", ""))
        ]
        return hooks, ""
    finally:
        proc.kill()


def launch_agent_state() -> tuple[bool, str]:
    plist = Path.home() / "Library" / "LaunchAgents" / "com.developeros.decision-watchdog.plist"
    if not plist.exists():
        return False, "LaunchAgent plist is not installed"
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/com.developeros.decision-watchdog"],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"launchctl unavailable: {exc}"
    if result.returncode != 0:
        return False, "LaunchAgent is not loaded"
    match = re.search(r"^\s*state\s*=\s*(\S+)", result.stdout, re.MULTILINE)
    state = match.group(1) if match else "unknown"
    return state == "running", f"LaunchAgent state: {state}"


VERIFIED = "verified"
CONFIGURED = "configured"
UNVERIFIED = "unverified"


def repository_snapshot(root: Path) -> dict[str, Any]:
    """Everything needed to prove a repository was left exactly as it was."""
    files = {}
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts or not path.is_file():
            continue
        try:
            files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            files[str(path.relative_to(root))] = "unreadable"
    return {
        "files": files,
        "status": run_git(root, ["status", "--porcelain=v1", "--untracked-files=all"]),
        "head": run_git(root, ["rev-parse", "HEAD"]),
    }


def self_test(verbose: bool = False) -> int:
    """Exercise every capture path end to end without touching real history.

    Every line says how much it actually proves: `verified` means this run
    executed the path, `configured` means a config file says so but the other
    product is the only thing that can confirm it, and `unverified` means the
    check could not be completed.
    """
    config = load_config()
    results: list[tuple[str, bool, str, str]] = []

    def check(name: str, ok: bool, detail: str = "", level: str = VERIFIED) -> None:
        results.append((name, ok, detail, level))

    # 1. Registered repositories, read from the real installation.
    real_projects = registered_projects()
    missing = [
        str(metadata.get("path", ""))
        for _target, metadata in real_projects
        if not Path(str(metadata.get("path", ""))).is_dir()
    ]
    check(
        "Registered repositories",
        bool(real_projects) and not missing,
        f"{len(real_projects)} registered"
        + (f"; missing path(s): {', '.join(missing)}" if missing else "")
        + ("" if real_projects else " - run: decision-watchdog register /path/to/repo"),
    )

    # 2. Central portfolio archive.
    archive_value = str(config.get("portfolio_archive", "")).strip()
    archive = Path(archive_value).expanduser() if archive_value else None
    check(
        "Portfolio archive writable",
        bool(archive and archive.is_dir() and os.access(archive, os.W_OK)),
        archive_value or "not configured - run: decision-watchdog config --archive /path",
    )

    # 3. Hook wiring, per product. Presence in a config file is not proof that
    #    the product loaded the hook, so these are reported as `configured`.
    claude_hooks = installed_hook_commands(CLAUDE_SETTINGS_PATH)
    check(
        "Claude Code hooks configured",
        all(event in claude_hooks for event in HOOK_EVENTS),
        f"events: {sorted(claude_hooks) or 'none'}",
        CONFIGURED,
    )
    codex_hooks = installed_hook_commands(CODEX_HOOKS_PATH)
    check(
        "Codex hooks configured",
        all(event in codex_hooks for event in HOOK_EVENTS),
        f"events: {sorted(codex_hooks) or 'none'}",
        CONFIGURED,
    )
    loaded, problem = codex_hook_report(Path.cwd())
    if problem:
        check("Codex hooks loaded and trusted", False, problem, UNVERIFIED)
    else:
        statuses = {str(h.get("eventName")): str(h.get("trustStatus")) for h in loaded}
        trusted = bool(statuses) and all(value == "trusted" for value in statuses.values())
        check(
            "Codex hooks loaded and trusted",
            trusted,
            f"{statuses or 'codex loaded none of our hooks'}"
            + ("" if trusted else " - run /hooks inside Codex once and trust them"),
        )

    # 4. Capture plumbing, inside a throwaway installation.
    real_home = APP_DIR
    with tempfile.TemporaryDirectory(prefix="dw-selftest-") as sandbox:
        base = Path(sandbox)
        repo = base / "repo"
        repo.mkdir()
        try:
            use_home(base / "home")
            ensure_dirs()
            sandbox_archive = base / "archive"
            sandbox_archive.mkdir()
            atomic_json(
                CONFIG_PATH,
                {
                    **config,
                    "worktree_debounce_seconds": 0,
                    "portfolio_archive": str(sandbox_archive),
                },
            )

            for args in (
                ["init", "-q", str(repo)],
                ["-C", str(repo), "config", "user.email", "selftest@localhost"],
                ["-C", str(repo), "config", "user.name", "Decision Watchdog Self Test"],
            ):
                subprocess.run(["git", *args], check=True, capture_output=True)
            (repo / "README.md").write_text("# self test\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-qm", "initial"], check=True, capture_output=True
            )

            before = repository_snapshot(repo)
            target = register_project(repo)
            check("Project registration", (target / "project.json").exists(), str(target.name))
            check(
                "Registration leaves the repository untouched",
                repository_snapshot(repo) == before
                and not (repo / ".portfolio").exists(),
                "no files added, no status change, no portfolio directory",
            )

            # The hook binary runs as a separate process, exactly as the agents run it.
            payload = json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "self-test",
                    "cwd": str(repo),
                    "prompt": "self test",
                }
            )
            hook = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "hook", "--source", "claude"],
                input=payload,
                text=True,
                capture_output=True,
                timeout=20,
                env={**os.environ, HOME_ENV_VAR: str(APP_DIR)},
                check=False,
            )
            queued = list(QUEUE_DIR.glob("*.json"))
            check(
                "Hook handler writes a queue record",
                hook.returncode == 0 and len(queued) == 1,
                hook.stderr.strip()[:200] or f"{len(queued)} record(s)",
            )

            processed, _captured = process_queue_once()
            check(
                "Queue processing",
                processed == 1 and not list(QUEUE_DIR.glob("*.json")),
                f"{processed} record(s) processed and archived",
            )

            (repo / "feature.py").write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-qm", "add feature"],
                check=True,
                capture_output=True,
            )
            commits = scan_commits()
            check("Git commit monitoring", commits == 1, f"{commits} commit(s) queued")
            for path in QUEUE_DIR.glob("*.json"):
                path.unlink()

            (repo / "draft.py").write_text("draft = True\n", encoding="utf-8")
            first = scan_worktrees(load_config())
            second = scan_worktrees(load_config())
            check(
                "Uncommitted-change monitoring (debounced)",
                first == 0 and second == 1,
                "first pass starts the debounce window, second pass queues the analysis",
            )

            vault = base / "vault"
            (vault / "10 Projects").mkdir(parents=True)
            append_history(
                target,
                [
                    {
                        "id": "selftest",
                        "type": "decision",
                        "title": "Self test",
                        "timestamp": now_iso(),
                        "fingerprint": "selftest",
                    }
                ],
            )
            render_project(
                target,
                {
                    **load_config(),
                    "obsidian_vault": str(vault),
                    "portfolio_archive": str(sandbox_archive),
                },
            )
            synced = list(vault.rglob("Decisions.md"))
            vault_configured = bool(str(config.get("obsidian_vault", "")).strip())
            check(
                "Obsidian sync",
                len(synced) == 1,
                (str(synced[0].parent) if synced else "no file")
                + ("" if vault_configured else " - optional, no vault configured"),
            )
            exported = list(sandbox_archive.rglob("evidence.jsonl"))
            check(
                "Portfolio archive export",
                len(exported) == 1 and "Self test" in exported[0].read_text(encoding="utf-8"),
                str(exported[0].parent) if exported else "nothing exported",
            )
        finally:
            use_home(real_home)

    # 5. Local model and background service.
    models = ollama_models()
    check("Ollama reachable on 127.0.0.1", bool(models), f"{len(models)} model(s)")
    check(
        f"Configured model installed ({config['model']})",
        config["model"] in models,
        "" if config["model"] in models else f"available: {', '.join(models) or 'none'}",
    )
    running, detail = launch_agent_state()
    check("LaunchAgent running", running, detail)

    width = max(len(name) for name, _ok, _detail, _level in results)
    level_width = max(len(level) for _name, _ok, _detail, level in results)
    for name, ok, detail, level in results:
        line = f"{'OK  ' if ok else 'FAIL'} [{level.ljust(level_width)}] {name.ljust(width)}"
        if detail and (verbose or not ok):
            line += f"  - {detail}"
        print(line)
    failures = [name for name, ok, _detail, _level in results if not ok]
    optional = {"Obsidian sync"}
    if not archive_value:
        # Exporting to a central archive is opt-in; a misconfigured one is not.
        optional.add("Portfolio archive writable")
    blocking = [name for name in failures if name not in optional]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} checks passed.")
    print(
        "verified = executed by this run; configured = read from a config file and only the "
        "other product can confirm it; unverified = could not be checked."
    )
    return 1 if blocking else 0


def register_command(path: str, name: str, watch: bool, slug: str = "") -> int:
    candidate = Path(path).expanduser() if path else Path.cwd()
    if path and not candidate.exists():
        raise SystemExit(f"Path does not exist: {candidate}")
    root = git_root(candidate)
    if not root:
        raise SystemExit(f"Not a Git repository: {candidate}")
    target = register_project(root, name=name, watch=watch, slug=slug)
    metadata = load_json(target / "project.json", {})
    print(f"Registered: {metadata.get('name')}")
    print(f"Path:       {metadata.get('path')}")
    print(f"Id:         {metadata.get('id')}")
    print(f"Portfolio:  {metadata.get('portfolio_slug')}")
    print(f"Watch uncommitted changes: {'yes' if metadata.get('watch_worktree') else 'no'}")
    print(f"Data:       {target}")
    print("The repository itself was not modified.")
    return 0


def unregister_command(path: str, purge: bool) -> int:
    candidate = Path(path).expanduser() if path else Path.cwd()
    root = git_root(candidate)
    if not root:
        # A repository that has been moved or deleted must still be removable.
        root = candidate.resolve()
    existed, target = unregister_project(root, purge=purge)
    if not existed and not purge:
        raise SystemExit(f"Not registered: {root}")
    print(f"Unregistered: {root}")
    print("Captured history removed." if purge else f"Captured history kept in: {target}")
    return 0


def projects_command() -> int:
    entries = registered_projects()
    if not entries:
        print("No repositories are registered. Run: decision-watchdog register /path/to/repo")
        return 0
    for target, metadata in entries:
        path = str(metadata.get("path", ""))
        flags = []
        if not metadata.get("enabled", True):
            flags.append("disabled")
        if not metadata.get("watch_worktree", True):
            flags.append("commits only")
        if path and not Path(path).exists():
            flags.append("path missing")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        print(
            f"{metadata.get('name', '?')} ({metadata.get('portfolio_slug', '?')}) "
            f"- {len(read_history(target))} entries{suffix}"
        )
        print(f"    repository: {path}")
        print(f"    data:       {target}")
    return 0


def doctor() -> int:
    config = load_config()
    checks = {
        "Python 3.10+": sys.version_info >= (3, 10),
        "Git": bool(shutil.which("git")),
        "Ollama URL is local": is_loopback_url(str(config.get("ollama_url", ""))),
        "Ollama reachable": bool(ollama_models()),
        f"Configured model installed ({config['model']})": config["model"] in ollama_models(),
        "Codex hooks installed": len(installed_hook_commands(CODEX_HOOKS_PATH)) == len(HOOK_EVENTS),
        "Claude Code hooks installed": (
            len(installed_hook_commands(CLAUDE_SETTINGS_PATH)) == len(HOOK_EVENTS)
        ),
        "LaunchAgent running": launch_agent_state()[0],
        "Portfolio archive reachable": portfolio_archive_dir(config) is not None,
    }
    for name, ok in checks.items():
        print(f"{'OK ' if ok else 'FAIL'} {name}")
    print("Run 'decision-watchdog self-test' for an end-to-end check of every capture path.")
    return 0 if all(checks.values()) else 1


def status() -> int:
    config = load_config()
    print(f"Decision Watchdog {VERSION}")
    print(f"Model: {config['model']}")
    print(f"Obsidian: {config.get('obsidian_vault') or '(not configured)'}")
    print(f"Portfolio archive: {config.get('portfolio_archive') or '(not configured)'}")
    counts = queue_counts()
    print(
        f"Queue: {counts['json']} waiting, {counts['processing']} in progress,"
        f" {counts['failed']} failed"
    )
    if counts["failed"]:
        print("  Failed records retry automatically; 'decision-watchdog retry' forces it now.")
    projects = list((DATA_DIR / "projects").glob("*/project.json"))
    print(f"Projects: {len(projects)}")
    for path in projects:
        metadata = load_json(path, {})
        count = len(read_history(path.parent))
        watching = "watched" if metadata.get("watch_worktree", True) else "commits only"
        print(
            f"- {metadata.get('name', '?')}: {count} entries, {watching}"
            f" - {metadata.get('path', '')}"
        )
    return 0


def uninstall(remove_data: bool) -> int:
    # Import lazily so the installed copy remains a single executable entrypoint.
    installer = APP_DIR / "app" / "install.py"
    if not installer.exists():
        raise SystemExit("Installed uninstaller was not found. Re-run install.py, then uninstall.")
    command = [sys.executable, str(installer), "--uninstall"]
    if remove_data:
        command.append("--remove-data")
    return subprocess.call(command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="decision-watchdog")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    hook_parser = sub.add_parser("hook", help=argparse.SUPPRESS)
    hook_parser.add_argument("--source", required=True, choices=["codex", "claude"])
    sub.add_parser("worker", help=argparse.SUPPRESS)
    sub.add_parser("doctor", help="Check prerequisites, hooks and the background worker")
    self_test_parser = sub.add_parser("self-test", help="End-to-end check of every capture path")
    self_test_parser.add_argument("--verbose", action="store_true")
    register_parser = sub.add_parser("register", help="Register a repository for monitoring")
    register_parser.add_argument("path", nargs="?", default="")
    register_parser.add_argument("--name", default="")
    register_parser.add_argument(
        "--slug", default="", help="Portfolio identifier used inside the archive"
    )
    register_parser.add_argument(
        "--no-watch",
        dest="watch",
        action="store_false",
        default=True,
        help="Register without monitoring uncommitted changes",
    )
    unregister_parser = sub.add_parser("unregister", help="Stop monitoring a repository")
    unregister_parser.add_argument("path", nargs="?", default="")
    unregister_parser.add_argument(
        "--purge", action="store_true", help="Also delete the locally captured history"
    )
    sub.add_parser("projects", help="List registered repositories")
    sub.add_parser("status", help="Show configuration, queue state and projects")
    sub.add_parser("retry", help="Requeue every failed record immediately")
    sub.add_parser("analyze", help="Analyze the current repository now")
    mark_parser = sub.add_parser("mark", help="Record an explicit note for the current repository")
    mark_parser.add_argument("note")
    sub.add_parser("generate", help="Draft a case study for the current repository")
    config_parser = sub.add_parser("config", help="Set the model, Obsidian vault or archive")
    config_parser.add_argument("--model")
    config_parser.add_argument("--vault")
    config_parser.add_argument("--archive", help="Path to the central SWE portfolio archive")
    uninstall_parser = sub.add_parser("uninstall", help="Remove hooks, worker and command")
    uninstall_parser.add_argument("--remove-data", action="store_true")
    # argparse lists suppressed subcommands as "==SUPPRESS==" instead of hiding them.
    sub._choices_actions = [
        action for action in sub._choices_actions if action.help != argparse.SUPPRESS
    ]
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "hook":
        return hook_command(args.source)
    if args.command == "worker":
        return worker()
    if args.command == "doctor":
        return doctor()
    if args.command == "self-test":
        return self_test(args.verbose)
    if args.command == "register":
        return register_command(args.path, args.name, args.watch, args.slug)
    if args.command == "unregister":
        return unregister_command(args.path, args.purge)
    if args.command == "projects":
        return projects_command()
    if args.command == "status":
        return status()
    if args.command == "retry":
        return retry_command()
    if args.command == "analyze":
        return analyze_now()
    if args.command == "mark":
        return manual_mark(args.note)
    if args.command == "generate":
        return generate_case_study()
    if args.command == "config":
        return configure(args)
    if args.command == "uninstall":
        return uninstall(args.remove_data)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
