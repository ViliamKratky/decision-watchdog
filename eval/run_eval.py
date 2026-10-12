#!/usr/bin/env python3
"""Extraction-quality benchmark for Decision Watchdog.

For every scenario in eval/scenarios/ this builds a throwaway git repository and
a throwaway Decision Watchdog home, registers the repository, runs the real
`watchdog.analyze_record` against a local Ollama model, and stores the raw model
output plus the entries that were actually accepted into history.jsonl.

Nothing outside the temporary directories is touched: the home is redirected
with `watchdog.use_home()` and DECISION_WATCHDOG_HOME, and the config written
there has no Obsidian vault and no portfolio archive.

Usage:
    python3 eval/run_eval.py --model qwen3-coder:30b-64k [--variant current|legacy]
                             [--scenario ID ...] [--held-out-only|--no-held-out]
                             [--out DIR] [--skip-existing]
    python3 eval/score.py eval/results/<run-dir>          # metrics (run_eval also prints them)
    python3 eval/score.py eval/results/<run-dir> --dump   # every raw entry, for judging
    python3 eval/compare.py eval/results/<a> eval/results/<b> [--part all|dev|held_out]

Variants live in eval/variants.py. Scenarios with "held_out": true were not
looked at while designing prompt variants; report them separately.
Manual judgments go in eval/results/<run-dir>.judgments.json (see score.py).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(EVAL_DIR))

# Point the module at a scratch home before it is imported, so even the
# import-time default can never resolve to the real installation.
_BOOT_HOME = tempfile.mkdtemp(prefix="dw-eval-boot-")
os.environ["DECISION_WATCHDOG_HOME"] = _BOOT_HOME

import watchdog  # noqa: E402
import variants  # noqa: E402
import score  # noqa: E402

FIXED_DATE = "2026-09-01T10:00:00+02:00"


def git(cwd: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True, env=env
    )
    return result.stdout.strip()


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def build_repo(root: Path, spec: dict) -> None:
    root.mkdir(parents=True)
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Eval",
            "GIT_AUTHOR_EMAIL": "eval@example.invalid",
            "GIT_COMMITTER_NAME": "Eval",
            "GIT_COMMITTER_EMAIL": "eval@example.invalid",
            "GIT_AUTHOR_DATE": FIXED_DATE,
            "GIT_COMMITTER_DATE": FIXED_DATE,
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(root.parent),  # no global git config / hooks
        }
    )
    git(root, "init", "-q", "-b", "main", env=env)
    for commit in spec.get("commits", []):
        write_files(root, commit.get("files", {}))
        for rel in commit.get("delete", []):
            (root / rel).unlink()
        git(root, "add", "-A", env=env)
        git(root, "commit", "-q", "-m", commit["message"], env=env)
    write_files(root, spec.get("uncommitted", {}))


def seed_history(target: Path, entries: list[dict]) -> int:
    if not entries:
        return 0
    rows = []
    for i, entry in enumerate(entries):
        row = {
            "id": f"evt-seed-{i}",
            "timestamp": "2026-08-30T12:00:00+02:00",
            "source": "claude",
            "status": "captured",
            "confidence": 0.9,
            **entry,
        }
        row["fingerprint"] = watchdog.normalized_fingerprint(row)
        rows.append(row)
    watchdog.append_history(target, rows)
    state_path = target / "state.json"
    state = watchdog.load_json(state_path, {})
    state["fingerprints"] = [r["fingerprint"] for r in rows]
    watchdog.atomic_json(state_path, state)
    return len(rows)


def run_scenario(scenario: dict, model: str, variant: str, workdir: Path) -> dict:
    case_dir = Path(tempfile.mkdtemp(prefix=f"{scenario['id']}-", dir=workdir))
    home = case_dir / "home"
    repo = case_dir / scenario["repo"].get("name", "repo")
    os.environ["DECISION_WATCHDOG_HOME"] = str(home)
    watchdog.use_home(home)
    watchdog.ensure_dirs()
    config = dict(watchdog.DEFAULT_CONFIG)
    config.update(
        {
            "model": model,
            "obsidian_vault": "",
            "portfolio_archive": "",
            "min_confidence": 0.62,
        }
    )
    watchdog.atomic_json(watchdog.CONFIG_PATH, config)

    build_repo(repo, scenario["repo"])
    root = watchdog.git_root(repo)
    target = watchdog.register_project(root, name=scenario["repo"].get("name", ""))
    seeded = seed_history(target, scenario.get("existing_entries", []))

    rec = scenario["record"]
    record = {
        "queued_at": "2026-09-01T10:05:00+02:00",
        "source": rec.get("source", "claude"),
        "project_root": str(root),
        "event": rec.get("event", "Stop"),
        "session_id": "eval",
        "turn_id": "",
        "prompt": rec.get("prompt", ""),
        "assistant": rec.get("assistant", ""),
    }
    if rec.get("explicit_note"):
        record["explicit_note"] = rec["explicit_note"]
    commit = ""
    if scenario.get("analyze_commit"):
        commit = git(root, "rev-parse", scenario["analyze_commit"])
        record["commit"] = commit

    post_filter = variants.apply(variant)
    calls: list[dict] = []
    original_chat = watchdog.ollama_chat

    def recording_chat(cfg, messages, schema):
        started = time.monotonic()
        content = original_chat(cfg, messages, schema)
        elapsed = time.monotonic() - started
        call = {"latency_s": round(elapsed, 2), "raw": content, "user_packet_chars": len(messages[-1]["content"])}
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = None
        call["parsed"] = parsed
        if post_filter and isinstance(parsed, dict):
            filtered = post_filter(parsed)
            call["filtered"] = filtered
            content = json.dumps(filtered)
        calls.append(call)
        return content

    watchdog.ollama_chat = recording_chat
    error = ""
    try:
        for attempt in range(2):
            try:
                count = watchdog.analyze_record(record, commit=commit)
                error = ""
                break
            except RuntimeError as exc:
                error = str(exc)
                count = 0
                time.sleep(5)
    finally:
        watchdog.ollama_chat = original_chat
        variants.reset()

    history = watchdog.read_history(target)
    accepted = history[seeded:]
    keep = (
        "type", "title", "summary", "context", "alternatives", "rationale",
        "tradeoffs", "evidence", "files", "tags", "confidence",
    )
    result = {
        "scenario": scenario["id"],
        "model": model,
        "variant": variant,
        "error": error,
        "latency_s": calls[-1]["latency_s"] if calls else None,
        "raw_output": calls[-1]["parsed"] if calls else None,
        "raw_text_if_unparsed": calls[-1]["raw"] if calls and calls[-1]["parsed"] is None else "",
        "filtered_output": calls[-1].get("filtered") if calls else None,
        "accepted": [{k: e.get(k) for k in keep} for e in accepted],
        "accepted_count": count,
        "user_packet_chars": calls[-1]["user_packet_chars"] if calls else None,
    }
    shutil.rmtree(case_dir, ignore_errors=True)
    return result


def load_scenarios(ids: list[str], held_out: str) -> list[dict]:
    scenarios = []
    for path in sorted((EVAL_DIR / "scenarios").glob("*.json")):
        s = json.loads(path.read_text(encoding="utf-8"))
        if ids and s["id"] not in ids:
            continue
        if held_out == "only" and not s.get("held_out"):
            continue
        if held_out == "exclude" and s.get("held_out"):
            continue
        scenarios.append(s)
    return scenarios


def safe(tag: str) -> str:
    return tag.replace(":", "_").replace("/", "_")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True)
    parser.add_argument("--variant", default="current", choices=sorted(variants.VARIANTS))
    parser.add_argument("--scenario", action="append", default=[])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--held-out-only", action="store_true")
    group.add_argument("--no-held-out", action="store_true")
    parser.add_argument("--out", default="", help="results dir (default eval/results/<model>__<variant>)")
    parser.add_argument("--skip-existing", action="store_true", help="resume: skip scenarios already saved")
    args = parser.parse_args()

    held = "only" if args.held_out_only else "exclude" if args.no_held_out else "all"
    scenarios = load_scenarios(args.scenario, held)
    out = Path(args.out) if args.out else EVAL_DIR / "results" / f"{safe(args.model)}__{args.variant}"
    out.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="dw-eval-"))
    print(f"{len(scenarios)} scenarios -> {out}  (scratch {workdir})", flush=True)
    for s in scenarios:
        dest = out / f"{s['id']}.json"
        if args.skip_existing and dest.exists():
            continue
        result = run_scenario(s, args.model, args.variant, workdir)
        dest.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        raw = result["raw_output"] or {}
        print(
            f"{s['id']:<36} expect={'Y' if s['labels']['should_record'] else 'N'} "
            f"accepted={result['accepted_count']} raw={len(raw.get('entries', []) or [])} "
            f"worth={raw.get('worth_recording')} conf={[e.get('confidence') for e in raw.get('entries', []) or []]} "
            f"{result['latency_s']}s {('ERROR ' + result['error']) if result['error'] else ''}",
            flush=True,
        )
    shutil.rmtree(workdir, ignore_errors=True)
    shutil.rmtree(_BOOT_HOME, ignore_errors=True)
    print()
    print(score.render(score.score_dir(out)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
