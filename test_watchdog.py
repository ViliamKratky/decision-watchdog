import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import watchdog


class RepoTestCase(unittest.TestCase):
    """Base fixture: a throwaway installation plus a throwaway Git repository."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-q", str(self.repo), cwd=None)
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.write("README.md", "# Test\n")
        self.commit("initial")
        self.real_home = watchdog.APP_DIR
        watchdog.use_home(self.base / "home")
        watchdog.ensure_dirs()

    def tearDown(self):
        watchdog.use_home(self.real_home)
        self.temp.cleanup()

    def git(self, *args, cwd="repo"):
        prefix = ["-C", str(self.repo)] if cwd == "repo" else []
        subprocess.run(["git", *prefix, *args], check=True, capture_output=True)

    def write(self, name, content):
        (self.repo / name).write_text(content, encoding="utf-8")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def config(self, **overrides):
        return {**watchdog.DEFAULT_CONFIG, "obsidian_vault": "", **overrides}


class RenderingTests(RepoTestCase):
    def test_register_and_render(self):
        target = watchdog.register_project(self.repo)
        entry = {
            "id": "evt-1",
            "timestamp": "2026-09-19T20:00:00+02:00",
            "type": "decision",
            "title": "Keep static frontend",
            "summary": "Avoided a new runtime.",
            "context": "Drupal integration.",
            "source": "codex",
            "git_commit": "abc",
            "confidence": 0.9,
            "alternatives": ["React"],
            "rationale": ["Simpler deployment"],
            "tradeoffs": ["Less abstraction"],
            "evidence": ["Changed HTML and CSS"],
        }
        watchdog.append_history(target, [entry])
        watchdog.render_project(target, self.config())
        content = (target / "Decisions.md").read_text(encoding="utf-8")
        self.assertIn("Keep static frontend", content)
        self.assertIn("Simpler deployment", content)

    def test_an_entry_without_a_commit_renders_no_empty_code_span(self):
        target = watchdog.register_project(self.repo)
        watchdog.append_history(
            target,
            [
                {
                    "id": "evt-2",
                    "timestamp": "2026-09-19T20:00:00+02:00",
                    "type": "problem",
                    "title": "Uncommitted finding",
                    "git_commit": "",
                }
            ],
        )
        watchdog.render_project(target, self.config())
        for name in ("Decisions.md", "Timeline.md"):
            self.assertNotIn("``", (target / name).read_text(encoding="utf-8"))

    def test_fingerprint_is_stable(self):
        event = {"type": "decision", "title": "Use Drupal", "summary": "Lower integration cost"}
        self.assertEqual(
            watchdog.normalized_fingerprint(event), watchdog.normalized_fingerprint(event)
        )

    def test_schema_accepts_all_event_types(self):
        schema = watchdog.extraction_schema()
        values = schema["properties"]["entries"]["items"]["properties"]["type"]["enum"]
        self.assertEqual(values, watchdog.EVENT_TYPES)

    def test_secret_redaction(self):
        text = "api_key=supersecretvalue\nToken: abcdefghijklmnop\nsk-abcdefghijklmnop"
        redacted = watchdog.redact_secrets(text)
        self.assertNotIn("supersecretvalue", redacted)
        self.assertNotIn("abcdefghijklmnop", redacted)
        self.assertIn("[REDACTED", redacted)

    def test_obsidian_sync_copies_generated_markdown(self):
        target = watchdog.register_project(self.repo)
        vault = self.base / "vault"
        (vault / "10 Projects").mkdir(parents=True)
        watchdog.append_history(
            target, [{"id": "e", "type": "decision", "title": "T", "timestamp": "2026-09-19"}]
        )
        watchdog.render_project(target, self.config(obsidian_vault=str(vault)))
        copied = list(vault.rglob("Decisions.md"))
        self.assertEqual(len(copied), 1)
        self.assertIn("T", copied[0].read_text(encoding="utf-8"))

    def test_obsidian_sync_is_skipped_when_unconfigured(self):
        target = watchdog.register_project(self.repo)
        watchdog.render_project(target, self.config(obsidian_vault=""))
        self.assertFalse(list(self.base.glob("vault*")))


class AnalysisTests(RepoTestCase):
    MODEL_RESULT = {
        "worth_recording": True,
        "entries": [
            {
                "type": "decision",
                "title": "Keep the static frontend",
                "summary": "Avoided adding a second runtime.",
                "context": "The page is integrated into Drupal.",
                "alternatives": ["React application"],
                "rationale": ["Simpler deployment"],
                "tradeoffs": ["Less component abstraction"],
                "evidence": ["README and Git diff"],
                "files": ["README.md"],
                "tags": ["architecture"],
                "confidence": 0.91,
            }
        ],
    }

    def test_analysis_appends_once_and_deduplicates(self):
        record = {
            "queued_at": "2026-09-19T20:00:00+02:00",
            "source": "codex",
            "project_root": str(self.repo),
            "event": "Stop",
            "session_id": "session-1",
            "prompt": "Keep this in Drupal.",
            "assistant": "Implemented without a new runtime.",
        }
        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(
                watchdog, "ollama_chat", return_value=json.dumps(self.MODEL_RESULT)
            ),
        ):
            self.assertEqual(watchdog.analyze_record(record), 1)
            self.assertEqual(watchdog.analyze_record(record), 0)
            self.assertEqual(len(watchdog.read_history(watchdog.project_dir(self.repo))), 1)

    def test_evidence_packet_never_contains_the_transcript(self):
        """Only the prompt, the final message and Git data may reach the model."""
        transcript = self.base / "transcript.jsonl"
        transcript.write_text('{"secret":"ENTIRE CONVERSATION HISTORY"}\n', encoding="utf-8")
        record = {
            "queued_at": "2026-09-19T20:00:00+02:00",
            "source": "claude",
            "project_root": str(self.repo),
            "event": "Stop",
            "session_id": "s",
            "prompt": "p",
            "assistant": "a",
            "transcript_path": str(transcript),
        }
        seen = {}

        def capture(config, messages, schema):
            seen["prompt"] = messages[-1]["content"]
            return json.dumps({"worth_recording": False, "entries": []})

        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(watchdog, "ollama_chat", side_effect=capture),
        ):
            watchdog.analyze_record(record)
        self.assertNotIn("ENTIRE CONVERSATION HISTORY", seen["prompt"])
        self.assertNotIn(str(transcript), seen["prompt"])


class CommitMonitoringTests(RepoTestCase):
    def test_new_commits_are_queued_once(self):
        watchdog.register_project(self.repo)
        self.write("feature.py", "value = 1\n")
        self.commit("add feature")
        self.assertEqual(watchdog.scan_commits(), 1)
        self.assertEqual(len(list(watchdog.QUEUE_DIR.glob("*.json"))), 1)
        # A second pass must not re-queue the same commit.
        self.assertEqual(watchdog.scan_commits(), 0)

    def test_queued_commit_record_targets_the_commit(self):
        watchdog.register_project(self.repo)
        self.write("feature.py", "value = 1\n")
        self.commit("add feature")
        watchdog.scan_commits()
        record = json.loads(next(watchdog.QUEUE_DIR.glob("*.json")).read_text())
        self.assertEqual(record["event"], "commit")
        self.assertEqual(record["source"], "git")
        self.assertTrue(record["commit"])

    def test_scanning_does_not_call_the_model(self):
        watchdog.register_project(self.repo)
        self.write("feature.py", "value = 1\n")
        self.commit("add feature")
        with mock.patch.object(watchdog, "ollama_chat", side_effect=AssertionError("called")):
            watchdog.scan_commits()
            watchdog.scan_worktrees(self.config(worktree_debounce_seconds=0))


class WorktreeMonitoringTests(RepoTestCase):
    def test_clean_tree_is_never_queued(self):
        watchdog.register_project(self.repo)
        config = self.config(worktree_debounce_seconds=0)
        self.assertEqual(watchdog.scan_worktrees(config), 0)
        self.assertEqual(watchdog.scan_worktrees(config), 0)

    def test_change_is_queued_only_after_the_debounce_window(self):
        watchdog.register_project(self.repo)
        config = self.config(worktree_debounce_seconds=300)
        self.write("draft.py", "draft = True\n")
        # First pass only records that something moved.
        self.assertEqual(watchdog.scan_worktrees(config, now=1_000.0), 0)
        # Still inside the quiet window.
        self.assertEqual(watchdog.scan_worktrees(config, now=1_100.0), 0)
        # The tree has now been quiet for longer than the window.
        self.assertEqual(watchdog.scan_worktrees(config, now=1_400.0), 1)

    def test_debounce_window_restarts_while_editing_continues(self):
        watchdog.register_project(self.repo)
        config = self.config(worktree_debounce_seconds=300)
        self.write("draft.py", "draft = True\n")
        self.assertEqual(watchdog.scan_worktrees(config, now=1_000.0), 0)
        self.write("draft.py", "draft = True\nmore = 2\n")
        self.assertEqual(watchdog.scan_worktrees(config, now=1_200.0), 0)
        # The window restarted at 1200, so 1400 is still too early.
        self.assertEqual(watchdog.scan_worktrees(config, now=1_400.0), 0)
        self.assertEqual(watchdog.scan_worktrees(config, now=1_600.0), 1)

    def test_unchanged_tree_is_not_queued_twice(self):
        watchdog.register_project(self.repo)
        config = self.config(worktree_debounce_seconds=0, worktree_min_interval_seconds=0)
        self.write("draft.py", "draft = True\n")
        watchdog.scan_worktrees(config, now=1_000.0)
        self.assertEqual(watchdog.scan_worktrees(config, now=2_000.0), 1)
        self.assertEqual(watchdog.scan_worktrees(config, now=3_000.0), 0)

    def test_minimum_interval_limits_repeated_analysis(self):
        watchdog.register_project(self.repo)
        config = self.config(worktree_debounce_seconds=0, worktree_min_interval_seconds=1800)
        self.write("draft.py", "a = 1\n")
        watchdog.scan_worktrees(config, now=1_000.0)
        self.assertEqual(watchdog.scan_worktrees(config, now=1_100.0), 1)
        self.write("draft.py", "a = 2\n")
        # The new content is seen, but the project was analyzed too recently.
        watchdog.scan_worktrees(config, now=1_200.0)
        self.assertEqual(watchdog.scan_worktrees(config, now=1_300.0), 0)
        # Once the minimum interval has passed the pending change is analyzed.
        self.assertEqual(watchdog.scan_worktrees(config, now=4_000.0), 1)
        self.assertEqual(watchdog.scan_worktrees(config, now=4_100.0), 0)

    def test_committing_resets_the_worktree_watcher(self):
        watchdog.register_project(self.repo)
        config = self.config(worktree_debounce_seconds=0, worktree_min_interval_seconds=0)
        self.write("draft.py", "draft = True\n")
        watchdog.scan_worktrees(config, now=1_000.0)
        watchdog.scan_worktrees(config, now=2_000.0)
        self.commit("ship draft")
        watchdog.scan_commits()
        state = json.loads((watchdog.project_dir(self.repo) / "state.json").read_text())
        self.assertEqual(state["analyzed_worktree_signature"], "")

    def test_watch_can_be_disabled_per_project(self):
        watchdog.register_project(self.repo, watch=False)
        config = self.config(worktree_debounce_seconds=0)
        self.write("draft.py", "draft = True\n")
        watchdog.scan_worktrees(config, now=1_000.0)
        self.assertEqual(watchdog.scan_worktrees(config, now=2_000.0), 0)

    def test_watch_can_be_disabled_globally(self):
        watchdog.register_project(self.repo)
        config = self.config(worktree_watch=False, worktree_debounce_seconds=0)
        self.write("draft.py", "draft = True\n")
        self.assertEqual(watchdog.scan_worktrees(config, now=1_000.0), 0)

    def test_signature_is_empty_for_a_clean_tree(self):
        self.assertEqual(watchdog.worktree_signature(self.repo), "")
        self.write("draft.py", "x = 1\n")
        self.assertTrue(watchdog.worktree_signature(self.repo))


class RegisterCommandTests(RepoTestCase):
    def test_register_defaults_to_watching(self):
        watchdog.register_command(str(self.repo), name="", watch=True)
        metadata = json.loads((watchdog.project_dir(self.repo) / "project.json").read_text())
        self.assertTrue(metadata["watch_worktree"])
        self.assertEqual(metadata["path"], str(self.repo.resolve()))

    def test_register_accepts_a_name_and_no_watch(self):
        watchdog.register_command(str(self.repo), name="Portfolio Site", watch=False)
        metadata = json.loads((watchdog.project_dir(self.repo) / "project.json").read_text())
        self.assertEqual(metadata["name"], "Portfolio Site")
        self.assertFalse(metadata["watch_worktree"])

    def test_register_is_idempotent_and_keeps_the_original_timestamp(self):
        watchdog.register_command(str(self.repo), name="First", watch=True)
        first = json.loads((watchdog.project_dir(self.repo) / "project.json").read_text())
        watchdog.register_command(str(self.repo), name="", watch=True)
        second = json.loads((watchdog.project_dir(self.repo) / "project.json").read_text())
        self.assertEqual(first["registered_at"], second["registered_at"])
        self.assertEqual(second["name"], "First")

    def test_register_rejects_a_non_repository(self):
        plain = self.base / "plain"
        plain.mkdir()
        with self.assertRaises(SystemExit):
            watchdog.register_command(str(plain), name="", watch=True)


class HookTests(RepoTestCase):
    def run_hook(self, source, payload):
        return subprocess.run(
            [sys.executable, str(Path(watchdog.__file__).resolve()), "hook", "--source", source],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=30,
            env={**__import__("os").environ, watchdog.HOME_ENV_VAR: str(watchdog.APP_DIR)},
            check=False,
        )

    def test_hook_queues_a_record_for_each_product(self):
        watchdog.register_project(self.repo)
        for source, event in (("claude", "UserPromptSubmit"), ("codex", "Stop")):
            with self.subTest(source=source):
                result = self.run_hook(
                    source,
                    {
                        "hook_event_name": event,
                        "session_id": f"{source}-1",
                        "cwd": str(self.repo),
                        "prompt": "hello",
                        "last_assistant_message": "done",
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                json.loads(result.stdout)  # both products require valid JSON on stdout
        records = [json.loads(p.read_text()) for p in watchdog.QUEUE_DIR.glob("*.json")]
        self.assertEqual({r["source"] for r in records}, {"claude", "codex"})

    def test_hook_uses_the_payload_cwd_not_the_process_cwd(self):
        watchdog.register_project(self.repo)
        self.run_hook(
            "codex",
            {"hook_event_name": "Stop", "session_id": "x", "cwd": str(self.repo)},
        )
        record = json.loads(next(watchdog.QUEUE_DIR.glob("*.json")).read_text())
        self.assertEqual(record["project_root"], str(self.repo.resolve()))

    def test_hook_outside_a_repository_queues_nothing(self):
        plain = self.base / "plain"
        plain.mkdir()
        result = self.run_hook(
            "claude", {"hook_event_name": "Stop", "session_id": "x", "cwd": str(plain)}
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(list(watchdog.QUEUE_DIR.glob("*.json")), [])

    def test_hook_in_an_unregistered_repository_queues_nothing(self):
        """Registration is the only opt-in; other repositories are ignored."""
        result = self.run_hook(
            "claude",
            {"hook_event_name": "Stop", "session_id": "x", "cwd": str(self.repo)},
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(list(watchdog.QUEUE_DIR.glob("*.json")), [])
        self.assertFalse(watchdog.project_dir(self.repo).exists())

    def test_hook_in_a_disabled_repository_queues_nothing(self):
        watchdog.register_project(self.repo, enabled=False)
        self.run_hook(
            "claude",
            {"hook_event_name": "Stop", "session_id": "x", "cwd": str(self.repo)},
        )
        self.assertEqual(list(watchdog.QUEUE_DIR.glob("*.json")), [])

    def test_hook_survives_malformed_input(self):
        result = subprocess.run(
            [sys.executable, str(Path(watchdog.__file__).resolve()), "hook", "--source", "claude"],
            input="not json at all",
            text=True,
            capture_output=True,
            timeout=30,
            env={**__import__("os").environ, watchdog.HOME_ENV_VAR: str(watchdog.APP_DIR)},
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        json.loads(result.stdout)


class QueueProcessingTests(RepoTestCase):
    def test_a_record_for_an_unregistered_project_is_not_analyzed(self):
        watchdog.register_project(self.repo)
        watchdog.queue_record(
            {
                "queued_at": watchdog.now_iso(),
                "source": "worktree",
                "project_root": str(self.repo),
                "event": "worktree",
                "session_id": "",
            }
        )
        watchdog.unregister_project(self.repo)
        with mock.patch.object(watchdog, "ollama_chat", side_effect=AssertionError("called")):
            processed, captured = watchdog.process_queue_once()
        self.assertEqual((processed, captured), (1, 0))

    def test_queue_names_order_records_written_in_the_same_millisecond(self):
        names = [watchdog.queue_record({"event": "x", "session_id": "s"}).name for _ in range(25)]
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(set(names)), len(names))

    def test_records_are_archived_after_processing(self):
        watchdog.register_project(self.repo)
        watchdog.queue_record(
            {
                "queued_at": watchdog.now_iso(),
                "source": "claude",
                "project_root": str(self.repo),
                "event": "UserPromptSubmit",
                "session_id": "s1",
                "prompt": "why did we choose Drupal?",
            }
        )
        processed, captured = watchdog.process_queue_once()
        self.assertEqual((processed, captured), (1, 0))
        self.assertEqual(list(watchdog.QUEUE_DIR.glob("*.json")), [])
        self.assertEqual(len(list((watchdog.project_dir(self.repo) / "raw").glob("*.json"))), 1)

    def test_prompt_is_carried_from_the_prompt_event_to_the_stop_event(self):
        watchdog.register_project(self.repo)
        for record in (
            {
                "queued_at": watchdog.now_iso(),
                "source": "claude",
                "project_root": str(self.repo),
                "event": "UserPromptSubmit",
                "session_id": "s1",
                "prompt": "why did we choose Drupal?",
            },
            {
                "queued_at": watchdog.now_iso(),
                "source": "claude",
                "project_root": str(self.repo),
                "event": "Stop",
                "session_id": "s1",
                "prompt": "",
                "assistant": "Because of the existing CMS.",
            },
        ):
            watchdog.queue_record(record)
        seen = {}

        def capture(config, messages, schema):
            seen["prompt"] = messages[-1]["content"]
            return json.dumps({"worth_recording": False, "entries": []})

        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(watchdog, "ollama_chat", side_effect=capture),
        ):
            watchdog.process_queue_once()
        self.assertIn("why did we choose Drupal?", seen["prompt"])

    def test_a_failing_record_is_parked_and_does_not_block_the_queue(self):
        watchdog.register_project(self.repo)
        watchdog.queue_record(
            {
                "queued_at": watchdog.now_iso(),
                "source": "git",
                "project_root": str(self.repo),
                "event": "commit",
                "session_id": "",
            }
        )
        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(watchdog, "ollama_chat", side_effect=OSError("ollama is down")),
        ):
            watchdog.process_queue_once()
        self.assertEqual(list(watchdog.QUEUE_DIR.glob("*.json")), [])
        self.assertEqual(len(list(watchdog.QUEUE_DIR.glob("*.failed"))), 1)
        self.assertTrue((watchdog.LOG_DIR / "worker-errors.log").exists())

    def queue_commit_record(self):
        watchdog.register_project(self.repo)
        return watchdog.queue_record(
            {
                "queued_at": watchdog.now_iso(),
                "source": "git",
                "project_root": str(self.repo),
                "event": "commit",
                "session_id": "",
            }
        )

    def fail_once(self):
        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(watchdog, "ollama_chat", side_effect=OSError("ollama is down")),
        ):
            watchdog.process_queue_once()

    def test_a_failed_record_counts_its_attempts(self):
        self.queue_commit_record()
        self.fail_once()
        [failed] = watchdog.QUEUE_DIR.glob("*.failed")
        record = json.loads(failed.read_text(encoding="utf-8"))
        self.assertEqual(record["_attempts"], 1)
        self.assertIn("ollama is down", record["_last_error"])
        self.assertEqual(record["event"], "commit")

    def test_a_failed_record_is_retried_after_its_backoff(self):
        self.queue_commit_record()
        self.fail_once()
        [failed] = watchdog.QUEUE_DIR.glob("*.failed")
        parked_at = failed.stat().st_mtime
        self.assertEqual(watchdog.requeue_failed(now=parked_at + 1), 0)
        self.assertEqual(watchdog.requeue_failed(now=parked_at + watchdog.retry_delay(1)), 1)
        self.assertEqual(len(list(watchdog.QUEUE_DIR.glob("*.json"))), 1)

    def test_backoff_grows_and_is_capped(self):
        delays = [watchdog.retry_delay(n) for n in range(1, 12)]
        self.assertEqual(delays, sorted(delays))
        self.assertEqual(delays[-1], watchdog.RETRY_MAX_SECONDS)

    def test_a_retried_record_succeeds_and_is_archived(self):
        self.queue_commit_record()
        self.fail_once()
        watchdog.requeue_failed(force=True)
        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(
                watchdog,
                "ollama_chat",
                return_value=json.dumps({"worth_recording": False, "entries": []}),
            ),
        ):
            processed, _captured = watchdog.process_queue_once()
        self.assertEqual(processed, 1)
        self.assertEqual(watchdog.queue_counts(), {"json": 0, "processing": 0, "failed": 0})

    def test_retry_bookkeeping_does_not_change_record_identity(self):
        self.queue_commit_record()
        self.fail_once()
        watchdog.requeue_failed(force=True)
        [queued] = watchdog.QUEUE_DIR.glob("*.json")
        record = json.loads(queued.read_text(encoding="utf-8"))
        response = json.dumps({"worth_recording": False, "entries": []})
        calls = []
        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(
                watchdog, "ollama_chat", side_effect=lambda *a: calls.append(1) or response
            ),
        ):
            watchdog.analyze_record(record)
            clean = {k: v for k, v in record.items() if not k.startswith("_")}
            watchdog.analyze_record(clean)
        self.assertEqual(len(calls), 1)

    def test_a_record_stops_retrying_after_the_attempt_limit(self):
        self.queue_commit_record()
        self.fail_once()
        [failed] = watchdog.QUEUE_DIR.glob("*.failed")
        record = json.loads(failed.read_text(encoding="utf-8"))
        record["_attempts"] = watchdog.MAX_ATTEMPTS
        failed.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(watchdog.requeue_failed(now=failed.stat().st_mtime + 10**9), 0)
        self.assertEqual(watchdog.requeue_failed(force=True), 1)

    def test_a_record_parked_before_attempt_counting_is_retried(self):
        legacy = watchdog.QUEUE_DIR / "00000000000000000001-legacy.failed"
        legacy.write_text(json.dumps({"event": "commit"}), encoding="utf-8")
        self.assertEqual(watchdog.requeue_failed(now=legacy.stat().st_mtime + 10**6), 1)

    def test_an_unreadable_failed_record_is_only_requeued_by_hand(self):
        broken = watchdog.QUEUE_DIR / "00000000000000000001-broken.failed"
        broken.write_text("{not json", encoding="utf-8")
        self.assertEqual(watchdog.requeue_failed(now=broken.stat().st_mtime + 10**9), 0)
        self.assertEqual(watchdog.requeue_failed(force=True), 1)

    def test_a_stale_claim_is_returned_to_the_queue(self):
        path = self.queue_commit_record()
        claimed = path.with_suffix(".processing")
        path.rename(claimed)
        claimed_at = claimed.stat().st_mtime
        self.assertEqual(watchdog.recover_stale_processing(now=claimed_at + 60), 0)
        self.assertEqual(
            watchdog.recover_stale_processing(
                now=claimed_at + watchdog.STALE_PROCESSING_SECONDS + 1
            ),
            1,
        )
        self.assertEqual(list(watchdog.QUEUE_DIR.glob("*.json")), [path])

    def test_claiming_a_record_refreshes_its_timestamp(self):
        path = self.queue_commit_record()
        old = 1_000_000_000
        os.utime(path, (old, old))
        seen = {}

        def capture(config, messages, schema):
            [claimed] = watchdog.QUEUE_DIR.glob("*.processing")
            seen["mtime"] = claimed.stat().st_mtime
            return json.dumps({"worth_recording": False, "entries": []})

        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(watchdog, "ollama_chat", side_effect=capture),
        ):
            watchdog.process_queue_once()
        self.assertGreater(seen["mtime"], old)

    def test_worktree_records_are_analyzed(self):
        watchdog.register_project(self.repo)
        self.write("draft.py", "draft = True\n")
        watchdog.scan_worktrees(self.config(worktree_debounce_seconds=0), now=1.0)
        watchdog.scan_worktrees(self.config(worktree_debounce_seconds=0), now=2.0)
        calls = []

        def capture(config, messages, schema):
            calls.append(messages[-1]["content"])
            return json.dumps({"worth_recording": False, "entries": []})

        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(watchdog, "ollama_chat", side_effect=capture),
        ):
            watchdog.process_queue_once()
        self.assertEqual(len(calls), 1)
        self.assertIn("draft.py", calls[0])


class RobustnessTests(RepoTestCase):
    NOTHING = json.dumps({"worth_recording": False, "entries": []})

    def stop_record(self, session="s1"):
        return {
            "queued_at": watchdog.now_iso(),
            "source": "claude",
            "project_root": str(self.repo),
            "event": "Stop",
            "session_id": session,
            "assistant": "done",
        }

    def test_a_commit_made_during_an_agent_turn_is_still_queued(self):
        watchdog.register_project(self.repo)
        watchdog.scan_commits()
        self.write("feature.py", "x = 1\n")
        self.commit("feature")
        watchdog.queue_record(self.stop_record())
        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(watchdog, "ollama_chat", return_value=self.NOTHING),
        ):
            watchdog.process_queue_once()
        self.assertEqual(watchdog.scan_commits(), 1)

    def test_commits_by_someone_else_are_not_queued(self):
        watchdog.register_project(self.repo)
        watchdog.scan_commits()
        self.write("theirs.py", "y = 2\n")
        self.git("add", "-A")
        self.git(
            "-c", "user.email=teammate@example.com", "-c", "user.name=Mate", "commit", "-qm", "x"
        )
        self.assertEqual(watchdog.scan_commits(), 0)

    def test_checking_out_an_old_branch_queues_nothing(self):
        self.git("checkout", "-q", "-b", "old")
        self.write("old.py", "z = 3\n")
        self.git("add", "-A")
        old_date = "2000-01-01T00:00:00"
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "old work", f"--date={old_date}"],
            check=True,
            env={**os.environ, "GIT_COMMITTER_DATE": old_date},
        )
        self.git("checkout", "-q", "-")
        watchdog.register_project(self.repo)
        watchdog.scan_commits()
        self.git("checkout", "-q", "old")
        self.assertEqual(watchdog.scan_commits(), 0)

    def test_the_first_commit_of_an_empty_repository_is_queued(self):
        empty = self.base / "empty"
        subprocess.run(["git", "init", "-q", str(empty)], check=True)
        for key, value in (("user.email", "test@example.com"), ("user.name", "Test")):
            subprocess.run(["git", "-C", str(empty), "config", key, value], check=True)
        watchdog.register_project(empty)
        watchdog.scan_commits()
        (empty / "a.txt").write_text("a\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(empty), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(empty), "commit", "-qm", "first"], check=True)
        self.assertEqual(watchdog.scan_commits(), 1)

    def test_a_retried_turn_keeps_its_own_prompt(self):
        watchdog.register_project(self.repo)
        first = dict(self.stop_record(), event="UserPromptSubmit", prompt="PROMPT ONE")
        watchdog.queue_record(first)
        watchdog.queue_record(self.stop_record())
        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(watchdog, "ollama_chat", side_effect=ValueError("bad output")),
        ):
            watchdog.process_queue_once()
        watchdog.queue_record(dict(first, prompt="PROMPT TWO"))
        with mock.patch.object(watchdog, "load_config", return_value=self.config()):
            watchdog.process_queue_once()
        watchdog.requeue_failed(force=True)
        seen = []
        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(
                watchdog,
                "ollama_chat",
                side_effect=lambda c, m, s: seen.append(m[-1]["content"]) or self.NOTHING,
            ),
        ):
            watchdog.process_queue_once()
        self.assertIn("PROMPT ONE", seen[0])
        self.assertNotIn("PROMPT TWO", seen[0])

    def test_an_ollama_outage_does_not_use_up_attempts_or_the_queue(self):
        watchdog.register_project(self.repo)
        watchdog.queue_record(self.stop_record("a"))
        watchdog.queue_record(self.stop_record("b"))
        refused = urllib.error.URLError(ConnectionRefusedError(61, "refused"))
        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(watchdog, "ollama_chat", side_effect=refused),
        ):
            watchdog.process_queue_once()
        [failed] = watchdog.QUEUE_DIR.glob("*.failed")
        self.assertEqual(json.loads(failed.read_text(encoding="utf-8"))["_attempts"], 0)
        self.assertEqual(len(list(watchdog.QUEUE_DIR.glob("*.json"))), 1)

    def test_concurrent_analyzers_do_not_lose_each_others_state(self):
        target = watchdog.register_project(self.repo)

        def concurrent_writer(config, messages, schema):
            with watchdog.locked_state(target) as state:
                state["processed_events"] = ["from-another-worker"]
            return self.NOTHING

        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(watchdog, "ollama_chat", side_effect=concurrent_writer),
        ):
            watchdog.analyze_record(self.stop_record())
        state = json.loads((target / "state.json").read_text(encoding="utf-8"))
        self.assertIn("from-another-worker", state["processed_events"])
        self.assertEqual(len(state["processed_events"]), 2)

    def test_a_second_worker_exits_immediately(self):
        lock = (watchdog.APP_DIR / ".worker.lock").open("a")
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with mock.patch.object(watchdog, "process_queue_once", side_effect=AssertionError):
            self.assertEqual(watchdog.worker(), 0)

    def test_an_export_failure_does_not_rerun_the_model(self):
        target = watchdog.register_project(self.repo)
        response = json.dumps(
            {
                "worth_recording": True,
                "entries": [
                    {
                        "type": "decision",
                        "title": "Chose SQLite",
                        "summary": "s",
                        "rationale": ["single user"],
                        "confidence": 0.9,
                    }
                ],
            }
        )
        watchdog.queue_record(self.stop_record())
        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(watchdog, "ollama_chat", return_value=response),
            mock.patch.object(watchdog, "render_project", side_effect=PermissionError("ro")),
        ):
            watchdog.process_queue_once()
        self.assertEqual(watchdog.queue_counts()["failed"], 0)
        self.assertTrue(json.loads((target / "state.json").read_text())["render_pending"])
        watchdog.retry_pending_renders(self.config())
        self.assertTrue((target / "Decisions.md").exists())
        self.assertNotIn("render_pending", json.loads((target / "state.json").read_text()))

    def test_a_torn_history_line_does_not_swallow_the_next_entry(self):
        target = watchdog.register_project(self.repo)
        (target / "history.jsonl").write_text('{"id": "torn"', encoding="utf-8")
        watchdog.append_history(target, [{"id": "next", "title": "Next"}])
        self.assertEqual([e["id"] for e in watchdog.read_history(target)], ["next"])

    def test_bad_config_values_fall_back_to_defaults(self):
        watchdog.atomic_json(
            watchdog.CONFIG_PATH,
            {"poll_seconds": "8s", "min_confidence": None, "worktree_watch": "yes"},
        )
        config = watchdog.load_config()
        self.assertEqual(config["poll_seconds"], watchdog.DEFAULT_CONFIG["poll_seconds"])
        self.assertEqual(config["min_confidence"], watchdog.DEFAULT_CONFIG["min_confidence"])
        self.assertIs(config["worktree_watch"], True)

    def test_a_lone_surrogate_in_a_hook_payload_is_still_queued(self):
        watchdog.register_project(self.repo)
        path = watchdog.enqueue(
            {"cwd": str(self.repo), "hook_event_name": "Stop", "last_assistant_message": "a\ud83d"}
        )
        self.assertTrue(path.exists())

    def test_one_malformed_entry_does_not_discard_its_neighbours(self):
        good = {"type": "decision", "title": "Keep", "summary": "s", "confidence": 0.9}
        for bad in (
            {"type": "decision", "title": "x", "confidence": "high"},
            {"type": "refactor", "title": "x", "confidence": 0.9},
            {"type": "decision", "title": None, "confidence": 0.9},
            "not an entry",
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(watchdog.normalize_entry(bad))
        self.assertEqual(watchdog.normalize_entry(good)["title"], "Keep")

    def test_a_string_worth_recording_is_not_true(self):
        watchdog.register_project(self.repo)
        response = json.dumps(
            {
                "worth_recording": "false",
                "entries": [{"type": "decision", "title": "No", "confidence": 0.9}],
            }
        )
        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(watchdog, "ollama_chat", return_value=response),
        ):
            self.assertEqual(watchdog.analyze_record(self.stop_record()), 0)

    def test_a_decision_without_reason_or_alternative_is_dropped(self):
        watchdog.register_project(self.repo)
        bare = {"type": "decision", "title": "Renamed helper", "summary": "s", "confidence": 0.9}
        milestone = {"type": "milestone", "title": "First deploy", "summary": "s", "confidence": 0.9}
        response = json.dumps({"worth_recording": True, "entries": [bare, milestone]})
        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(watchdog, "ollama_chat", return_value=response),
        ):
            self.assertEqual(watchdog.analyze_record(self.stop_record()), 1)
        titles = [e["title"] for e in watchdog.read_history(watchdog.project_dir(self.repo))]
        self.assertEqual(titles, ["First deploy"])

    def test_a_non_ascii_untracked_file_is_hashed(self):
        name = "café notes.py"
        self.write(name, "v1\n")
        first = watchdog.worktree_signature(self.repo)
        self.write(name, "v2\n")
        self.assertNotEqual(first, watchdog.worktree_signature(self.repo))


class HookConfigurationTests(unittest.TestCase):
    def test_our_handlers_are_recognised_in_both_products(self):
        settings = {
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "echo unrelated"}]},
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python watchdog.py hook --source codex "
                                "# decision-watchdog",
                            }
                        ]
                    },
                ],
                "SessionEnd": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": '"/x/python" "/y/Decision Watchdog/app/watchdog.py" '
                                "hook --source claude",
                            }
                        ]
                    }
                ],
            }
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            path.write_text(json.dumps(settings), encoding="utf-8")
            found = watchdog.installed_hook_commands(path)
        self.assertEqual(sorted(found), ["SessionEnd", "Stop"])
        self.assertEqual(len(found["Stop"]), 1)

    def test_missing_or_invalid_file_is_reported_as_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "nope.json"
            self.assertEqual(watchdog.installed_hook_commands(missing), {})
            broken = Path(temp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            self.assertEqual(watchdog.installed_hook_commands(broken), {})


class PortfolioArchiveTests(RepoTestCase):
    ENTRY = {
        "id": "evt-1",
        "fingerprint": "fp1",
        "timestamp": "2026-09-19T20:00:00+02:00",
        "type": "decision",
        "title": "Keep the static frontend",
        "summary": "Avoided a second runtime.",
        "repo_path": "/somewhere/repo",
        "git_commit": "abc123",
    }

    def archive(self):
        path = self.base / "archive"
        path.mkdir(exist_ok=True)
        return path

    def test_the_archive_path_is_configurable_and_off_by_default(self):
        self.assertEqual(watchdog.DEFAULT_CONFIG["portfolio_archive"], "")
        self.assertIsNone(watchdog.portfolio_archive_dir(self.config()))
        archive = self.archive()
        self.assertEqual(
            watchdog.portfolio_archive_dir(self.config(portfolio_archive=str(archive))), archive
        )

    def test_a_configured_but_missing_archive_is_ignored(self):
        config = self.config(portfolio_archive=str(self.base / "does-not-exist"))
        self.assertIsNone(watchdog.portfolio_archive_dir(config))
        target = watchdog.register_project(self.repo)
        watchdog.append_history(target, [self.ENTRY])
        self.assertIsNone(watchdog.export_portfolio(target, config))

    def test_export_writes_distilled_evidence_under_the_project_slug(self):
        archive = self.archive()
        watchdog.register_command(str(self.repo), name="Client Site", watch=True)
        target = watchdog.project_dir(self.repo)
        watchdog.append_history(target, [self.ENTRY])
        watchdog.render_project(target, self.config(portfolio_archive=str(archive)))
        destination = archive / "projects" / "client-site" / "private" / "watchdog"
        self.assertTrue((destination / "evidence.jsonl").exists())
        self.assertTrue((destination / "Decisions.md").exists())
        self.assertTrue((destination / "Timeline.md").exists())
        evidence = json.loads((destination / "evidence.jsonl").read_text().splitlines()[0])
        self.assertEqual(evidence["title"], "Keep the static frontend")
        self.assertEqual(evidence["git_commit"], "abc123")
        provenance = json.loads((destination / "source.json").read_text())
        self.assertEqual(provenance["portfolio_slug"], "client-site")
        self.assertEqual(provenance["repository_path"], str(self.repo.resolve()))

    def test_an_explicit_slug_overrides_the_derived_one(self):
        archive = self.archive()
        target = watchdog.register_project(self.repo, name="Repo", slug="Acme Portal")
        watchdog.append_history(target, [self.ENTRY])
        watchdog.export_portfolio(target, self.config(portfolio_archive=str(archive)))
        self.assertTrue((archive / "projects" / "acme-portal").is_dir())

    def test_export_never_touches_curated_archive_files(self):
        archive = self.archive()
        curated = archive / "projects" / "repo"
        (curated / "decisions").mkdir(parents=True)
        (curated / "case-study.md").write_text("hand written\n", encoding="utf-8")
        (curated / "project.yaml").write_text("slug: repo\n", encoding="utf-8")
        (curated / "decisions" / "0001-adr.md").write_text("adr\n", encoding="utf-8")
        target = watchdog.register_project(self.repo)
        watchdog.append_history(target, [self.ENTRY])
        watchdog.export_portfolio(target, self.config(portfolio_archive=str(archive)))
        self.assertEqual((curated / "case-study.md").read_text(), "hand written\n")
        self.assertEqual((curated / "project.yaml").read_text(), "slug: repo\n")
        self.assertEqual(sorted(p.name for p in (curated / "decisions").iterdir()), ["0001-adr.md"])

    def test_repeated_exports_do_not_duplicate_entries(self):
        archive = self.archive()
        config = self.config(portfolio_archive=str(archive))
        target = watchdog.register_project(self.repo)
        watchdog.append_history(target, [self.ENTRY])
        for _ in range(3):
            destination = watchdog.export_portfolio(target, config)
        lines = (destination / "evidence.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)

    def test_a_captured_event_reaches_the_archive(self):
        archive = self.archive()
        config = self.config(portfolio_archive=str(archive))
        record = {
            "queued_at": "2026-09-19T20:00:00+02:00",
            "source": "claude",
            "project_root": str(self.repo),
            "event": "Stop",
            "session_id": "s",
            "prompt": "why static?",
            "assistant": "kept static",
        }
        with (
            mock.patch.object(watchdog, "load_config", return_value=config),
            mock.patch.object(
                watchdog, "ollama_chat", return_value=json.dumps(AnalysisTests.MODEL_RESULT)
            ),
        ):
            self.assertEqual(watchdog.analyze_record(record), 1)
        exported = list(archive.rglob("evidence.jsonl"))
        self.assertEqual(len(exported), 1)
        entry = json.loads(exported[0].read_text().splitlines()[0])
        self.assertEqual(entry["repo_path"], str(self.repo.resolve()))
        self.assertEqual(entry["source"], "claude")
        self.assertTrue(entry["project_slug"])

    def test_slugify_is_filesystem_safe(self):
        self.assertEqual(watchdog.slugify("Acme / Portal (2026)"), "acme-portal-2026")
        self.assertEqual(watchdog.slugify("../../etc"), "etc")
        self.assertEqual(watchdog.slugify("   "), "unnamed-project")


class ExternalRegistryTests(RepoTestCase):
    def test_registration_records_a_slug_and_an_enabled_flag(self):
        watchdog.register_command(str(self.repo), name="Decision Watchdog", watch=True)
        target = watchdog.project_dir(self.repo)
        metadata = json.loads((target / "project.json").read_text())
        self.assertEqual(metadata["portfolio_slug"], "decision-watchdog")
        self.assertTrue(metadata["enabled"])
        self.assertEqual(metadata["path"], str(self.repo.resolve()))

    def test_the_registry_lives_outside_the_repository(self):
        target = watchdog.register_project(self.repo)
        self.assertTrue(str(target).startswith(str(watchdog.APP_DIR)))
        self.assertNotIn(str(self.repo), str(target))

    def test_unregister_keeps_captured_history_by_default(self):
        target = watchdog.register_project(self.repo)
        watchdog.append_history(target, [{"id": "e", "type": "decision", "title": "T"}])
        watchdog.unregister_command(str(self.repo), purge=False)
        self.assertFalse((target / "project.json").exists())
        self.assertEqual(watchdog.registered_projects(), [])
        self.assertTrue((target / "history.jsonl").exists())

    def test_unregister_with_purge_removes_the_local_data(self):
        target = watchdog.register_project(self.repo)
        watchdog.unregister_command(str(self.repo), purge=True)
        self.assertFalse(target.exists())

    def test_unregister_reports_an_unknown_repository(self):
        with self.assertRaises(SystemExit):
            watchdog.unregister_command(str(self.repo), purge=False)

    def test_register_rejects_a_path_that_does_not_exist(self):
        with self.assertRaises(SystemExit):
            watchdog.register_command(str(self.base / "nope"), name="", watch=True)
        self.assertEqual(watchdog.registered_projects(), [])

    def test_projects_lists_every_registered_repository(self):
        target = watchdog.register_project(self.repo, name="Alpha")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(watchdog.projects_command(), 0)
        self.assertIn("Alpha", out.getvalue())
        # The setup tutorial points users here to find Decisions.md.
        self.assertIn(str(target), out.getvalue())
        self.assertEqual(len(watchdog.registered_projects()), 1)

    def test_a_disabled_project_is_not_monitored(self):
        watchdog.register_project(self.repo, enabled=False)
        self.write("feature.py", "value = 1\n")
        self.commit("add feature")
        self.assertEqual(watchdog.scan_commits(), 0)
        self.write("draft.py", "draft = True\n")
        config = self.config(worktree_debounce_seconds=0)
        watchdog.scan_worktrees(config)
        self.assertEqual(watchdog.scan_worktrees(config), 0)


class RepositoryIsolationTests(RepoTestCase):
    """A registered repository may be shared with other contributors."""

    def snapshot(self):
        return watchdog.repository_snapshot(self.repo)

    def test_registration_and_monitoring_leave_the_repository_untouched(self):
        self.write("draft.py", "draft = True\n")
        before = self.snapshot()
        watchdog.register_command(str(self.repo), name="Shared", watch=True)
        config = self.config(
            worktree_debounce_seconds=0, portfolio_archive=str(self.base / "archive")
        )
        (self.base / "archive").mkdir()
        watchdog.scan_commits()
        watchdog.scan_worktrees(config)
        watchdog.scan_worktrees(config)
        with (
            mock.patch.object(watchdog, "load_config", return_value=config),
            mock.patch.object(
                watchdog, "ollama_chat", return_value=json.dumps(AnalysisTests.MODEL_RESULT)
            ),
        ):
            watchdog.process_queue_once()
        self.assertEqual(self.snapshot(), before)

    def test_no_portfolio_directory_is_ever_created(self):
        watchdog.register_command(str(self.repo), name="", watch=True)
        watchdog.scan_commits()
        watchdog.scan_worktrees(self.config(worktree_debounce_seconds=0))
        self.assertFalse((self.repo / ".portfolio").exists())
        self.assertEqual(list(self.repo.rglob(".portfolio")), [])
        self.assertEqual(list(self.repo.glob("*watchdog*")), [])

    def test_git_status_is_unchanged_by_registration(self):
        status_before = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        watchdog.register_command(str(self.repo), name="", watch=True)
        status_after = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertEqual(status_before, status_after)
        self.assertEqual(status_after, "")

    def test_the_source_tree_never_creates_a_portfolio_directory(self):
        """`.portfolio` may only ever be probed, never written."""
        for name in ("watchdog.py", "install.py"):
            text = (Path(watchdog.__file__).parent / name).read_text(encoding="utf-8")
            self.assertNotIn("WORKLOG.md", text)
            for line in text.splitlines():
                if ".portfolio" in line:
                    with self.subTest(source=name, line=line.strip()):
                        self.assertIn("exists(", line)


class CrossSourceDeduplicationTests(RepoTestCase):
    """One change seen by a hook, the worktree watcher and a commit is one entry."""

    def records(self):
        common = {
            "queued_at": "2026-09-19T20:00:00+02:00",
            "project_root": str(self.repo),
            "session_id": "",
            "prompt": "",
            "assistant": "",
        }
        return [
            {**common, "source": "claude", "event": "Stop", "session_id": "s1"},
            {**common, "source": "worktree", "event": "worktree", "worktree_signature": "abc"},
            {**common, "source": "git", "event": "commit"},
        ]

    def test_the_same_change_is_recorded_once_across_every_source(self):
        archive = self.base / "archive"
        archive.mkdir()
        config = self.config(portfolio_archive=str(archive))
        captured = 0
        with (
            mock.patch.object(watchdog, "load_config", return_value=config),
            mock.patch.object(
                watchdog, "ollama_chat", return_value=json.dumps(AnalysisTests.MODEL_RESULT)
            ),
        ):
            for record in self.records():
                captured += watchdog.analyze_record(record)
        self.assertEqual(captured, 1)
        self.assertEqual(len(watchdog.read_history(watchdog.project_dir(self.repo))), 1)
        exported = list(archive.rglob("evidence.jsonl"))[0]
        self.assertEqual(len(exported.read_text().strip().splitlines()), 1)

    def test_a_worktree_signature_is_analyzed_only_once(self):
        watchdog.register_project(self.repo)
        config = self.config(worktree_debounce_seconds=0, worktree_min_interval_seconds=0)
        self.write("draft.py", "draft = True\n")
        watchdog.scan_worktrees(config)
        self.assertEqual(watchdog.scan_worktrees(config), 1)
        self.assertEqual(watchdog.scan_worktrees(config), 0)
        self.assertEqual(watchdog.scan_worktrees(config), 0)


class SensitivePathTests(RepoTestCase):
    SENSITIVE = (
        ".env",
        ".env.local",
        ".env.production",
        ".envrc",
        "config/.env.test",
        "server.pem",
        "private.key",
        "id_rsa",
        "secrets.json",
        "secret.yaml",
        "prod.env",
        "env.local",
        "id_ecdsa",
        "credentials.json",
        "service-account.json",
        ".aws/credentials",
        ".npmrc",
        ".netrc",
        ".git-credentials",
        "kubeconfig",
        "terraform.tfstate",
        "release.keystore",
        "AuthKey_ABC123.p8",
        "secret.txt",
    )

    def test_the_pattern_matches_the_files_it_must(self):
        for name in self.SENSITIVE:
            with self.subTest(name=name):
                self.assertTrue(watchdog.SENSITIVE_NAME_RE.search(name))
        for name in (
            "README.md",
            "environment.py",
            "keyboard.ts",
            "monkey.py",
            "src/env.ts",
            "config/settings.json",
            "id_rsa.pub",
            "secrets_manager.py",
            "credential_helpers/README.md",
        ):
            with self.subTest(name=name):
                self.assertIsNone(watchdog.SENSITIVE_NAME_RE.search(name))

    def test_untracked_secrets_never_reach_the_evidence_packet(self):
        self.write(".env", "DATABASE_PASSWORD=HUNTER2SECRETVALUE\n")
        self.write("notes.md", "a readable untracked note\n")
        context = watchdog.git_context(self.repo, self.config())
        self.assertNotIn("HUNTER2SECRETVALUE", context["diff"])
        self.assertIn("a readable untracked note", context["diff"])

    def test_the_worktree_fingerprint_never_reads_a_sensitive_file(self):
        """Editing a secret still registers as a change, without hashing it."""
        self.write(".env", "TOKEN=first\n")
        first = watchdog.worktree_signature(self.repo)
        self.write(".env", "TOKEN=second-and-much-longer\n")
        self.assertEqual(watchdog.worktree_signature(self.repo), first)
        self.write("app.py", "x = 1\n")
        self.assertNotEqual(watchdog.worktree_signature(self.repo), first)

    def test_secrets_in_a_tracked_diff_are_redacted_before_analysis(self):
        self.write("config.yaml", "api_key: REALSECRETMATERIAL\n")
        seen = {}

        def capture(config, messages, schema):
            seen["prompt"] = messages[-1]["content"]
            return json.dumps({"worth_recording": False, "entries": []})

        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(watchdog, "ollama_chat", side_effect=capture),
        ):
            watchdog.analyze_record(
                {
                    "queued_at": watchdog.now_iso(),
                    "source": "manual",
                    "project_root": str(self.repo),
                    "event": "manual",
                    "session_id": "s",
                    "prompt": "",
                    "assistant": "",
                }
            )
        self.assertNotIn("REALSECRETMATERIAL", seen["prompt"])


class RedactionTests(RepoTestCase):
    # Canaries are assembled at runtime so this file never contains a string
    # that a secret scanner would flag.
    AWS = "AKIA" + "Q" * 16
    CASES = {
        "aws access key": f"key = {AWS}",
        "aws secret env": "AWS_SECRET_ACCESS_KEY=" + "c4n4ry" * 6,
        "gcp api key": "AIza" + "c4n4ry" + "x" * 29,
        "stripe live key": "sk_" + "live_" + "c4n4ry" * 4,
        "github fine-grained pat": "github_" + "pat_" + "c4n4ry" * 5,
        "jwt": "eyJ" + "a" * 20 + ".eyJ" + "b" * 20 + "." + "c4n4ry" * 3,
        "bearer header": "Authorization: Bearer " + "c4n4ry" * 4,
        "database url": "postgres://app:" + "c4n4ry" * 2 + "@db.internal/app",
        "npm token": "//registry.npmjs.org/:_authToken=" + "c4n4ry" * 4,
        "export": 'export SECRET_KEY="' + "c4n4ry" * 3 + '"',
        "prefixed env": "DB_PASSWORD=" + "c4n4ry" * 2,
        "json": '{"password": "' + "c4n4ry" * 2 + '"}',
        "diff line": "+password=" + "c4n4ry" * 2,
        "netrc": "machine example.com login me password " + "c4n4ry" * 2,
        "pgp": (
            "-----BEGIN PGP PRIVATE KEY BLOCK-----\nc4n4ry\n"
            "-----END PGP PRIVATE KEY BLOCK-----"
        ),
    }

    def test_known_secret_formats_are_redacted(self):
        for name, text in self.CASES.items():
            with self.subTest(name=name):
                redacted = watchdog.redact_secrets(text)
                self.assertNotIn("c4n4ry", redacted)
                self.assertNotIn(self.AWS, redacted)

    def test_ordinary_code_survives_redaction(self):
        for line in (
            "token = hashlib.sha256(seed).hexdigest()",
            "Author: Test <test@example.com>",
            "def tokens(self) -> int:",
        ):
            with self.subTest(line=line):
                self.assertEqual(watchdog.redact_secrets(line), line)

    def test_a_private_key_cut_by_a_clip_is_still_redacted(self):
        body = "\n".join("MIIEc4n4ry" + "A" * 54 for _ in range(30))
        key = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"
        self.assertNotIn("c4n4ry", watchdog.redact_clipped(key, 600))
        self.assertNotIn("c4n4ry", watchdog.redact_secrets(watchdog.clipped(key, 600)))

    def test_credential_files_are_left_out_of_diffs(self):
        diff = (
            "diff --git a/.env b/.env\n+STRIPE=c4n4ry\n"
            "diff --git a/app.py b/app.py\n+print('hi')\n"
        )
        result = watchdog.omit_sensitive_diffs(diff)
        self.assertNotIn("c4n4ry", result)
        self.assertIn("print('hi')", result)

    def test_a_tracked_credential_file_never_reaches_the_model(self):
        self.write(".env", "PLAIN=one\n")
        self.commit("env")
        self.write(".env", "PLAIN=c4n4ry\n")
        context = watchdog.git_context(self.repo, self.config())
        self.assertNotIn("c4n4ry", context["diff"])

    def test_untracked_symlinks_are_not_followed(self):
        outside = self.base / "outside.txt"
        outside.write_text("c4n4ry outside the repository\n", encoding="utf-8")
        (self.repo / "notes.txt").symlink_to(outside)
        context = watchdog.git_context(self.repo, self.config())
        self.assertNotIn("c4n4ry", context["diff"])

    def test_remote_credentials_are_never_stored(self):
        self.git("remote", "add", "origin", "https://bob:c4n4ry@example.com/team/app.git")
        target = watchdog.register_project(self.repo)
        metadata = json.loads((target / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["remote"], "https://example.com/team/app.git")

    def test_model_output_is_redacted_and_titles_stay_on_one_line(self):
        watchdog.register_project(self.repo)
        response = {
            "worth_recording": True,
            "entries": [
                {
                    "type": "decision",
                    "title": "Rotate keys\n# Injected heading",
                    "summary": f"The leaked key was {self.AWS}.",
                    "context": "",
                    "alternatives": [],
                    "rationale": ["The key was exposed"],
                    "tradeoffs": [],
                    "evidence": [],
                    "files": [],
                    "tags": [],
                    "confidence": 0.9,
                }
            ],
        }
        with (
            mock.patch.object(watchdog, "load_config", return_value=self.config()),
            mock.patch.object(watchdog, "ollama_chat", return_value=json.dumps(response)),
        ):
            watchdog.analyze_record({"project_root": str(self.repo), "event": "manual"})
        target = watchdog.project_dir(self.repo)
        for name in ("history.jsonl", "Decisions.md"):
            self.assertNotIn(self.AWS, (target / name).read_text(encoding="utf-8"))
        decisions = (target / "Decisions.md").read_text(encoding="utf-8")
        self.assertNotIn("\n# Injected heading", decisions)

    def test_model_markdown_cannot_embed_remote_images_or_html(self):
        rendered = watchdog.md_text("![x](https://example.com/?d=1) <img src=x>")
        self.assertNotIn("![", rendered)
        self.assertNotIn("<img", rendered)

    def test_queued_prompts_are_redacted_before_they_touch_the_disk(self):
        watchdog.register_project(self.repo)
        path = watchdog.enqueue(
            {
                "cwd": str(self.repo),
                "hook_event_name": "UserPromptSubmit",
                "prompt": f"use {self.AWS} for now",
            }
        )
        self.assertNotIn(self.AWS, path.read_text(encoding="utf-8"))


class NetworkTests(unittest.TestCase):
    def test_only_loopback_ollama_urls_are_accepted(self):
        for url in ("http://127.0.0.1:11434/api/chat", "http://localhost:11434", "http://[::1]:1"):
            with self.subTest(url=url):
                self.assertTrue(watchdog.is_loopback_url(url))
        for url in ("https://ollama.example.com/api/chat", "http://10.0.0.5:11434", ""):
            with self.subTest(url=url):
                self.assertFalse(watchdog.is_loopback_url(url))

    def test_a_remote_ollama_url_is_refused_before_any_request(self):
        config = {**watchdog.DEFAULT_CONFIG, "ollama_url": "https://ollama.example.com/api/chat"}
        with mock.patch.object(watchdog.LOCAL_OPENER, "open", side_effect=AssertionError("sent")):
            with self.assertRaises(RuntimeError):
                watchdog.ollama_chat(config, [], {})

    def test_the_ollama_client_ignores_proxy_settings(self):
        # An empty ProxyHandler replaces the default one, which would read
        # http_proxy from the environment, and then registers no handler at all.
        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9"}):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for candidate in (opener, watchdog.LOCAL_OPENER):
            self.assertFalse(
                any(isinstance(h, urllib.request.ProxyHandler) for h in candidate.handlers)
            )


class RetentionTests(RepoTestCase):
    def test_retention_also_clears_failed_records_and_cached_prompts(self):
        target = watchdog.register_project(self.repo)
        failed = watchdog.QUEUE_DIR / "00000000000000000001-old.failed"
        failed.write_text("{}", encoding="utf-8")
        os.utime(failed, (1, 1))
        watchdog.atomic_json(
            target / "session-context.json",
            {
                "old": {"prompt": "x", "updated_at": "2000-01-01T00:00:00+00:00"},
                "new": {"prompt": "y", "updated_at": watchdog.now_iso()},
            },
        )
        watchdog.prune_raw(self.config())
        self.assertFalse(failed.exists())
        contexts = json.loads((target / "session-context.json").read_text(encoding="utf-8"))
        self.assertEqual(list(contexts), ["new"])


class ExplicitCommandTests(RepoTestCase):
    def test_mark_refuses_an_unregistered_repository(self):
        with mock.patch.object(watchdog.Path, "cwd", return_value=self.repo):
            with self.assertRaises(SystemExit):
                watchdog.manual_mark("note")
        self.assertFalse(watchdog.is_registered(self.repo))


class GitLockTests(RepoTestCase):
    def test_reading_a_repository_never_rewrites_its_index(self):
        watchdog.register_project(self.repo)
        self.write("notes.md", "draft\n")
        # Same content, newer timestamp: exactly what makes `git diff` refresh the index.
        os.utime(self.repo / "README.md", (2_000_000_000, 2_000_000_000))
        index = self.repo / ".git" / "index"
        before = (index.read_bytes(), index.stat().st_mtime_ns)
        watchdog.git_context(self.repo, self.config())
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)


class PrivacyTests(unittest.TestCase):
    SOURCES = ("watchdog.py", "install.py")

    def test_no_remote_endpoint_is_referenced(self):
        import re

        allowed = {"127.0.0.1", "localhost"}
        for name in self.SOURCES:
            text = (Path(watchdog.__file__).parent / name).read_text(encoding="utf-8")
            for url in re.findall(r"https?://([A-Za-z0-9.\-]+)", text):
                with self.subTest(source=name, host=url):
                    self.assertIn(url, allowed)

    def test_ollama_url_default_is_loopback(self):
        self.assertTrue(watchdog.DEFAULT_CONFIG["ollama_url"].startswith("http://127.0.0.1:"))

    def test_transcript_file_is_never_read(self):
        text = (Path(watchdog.__file__)).read_text(encoding="utf-8")
        self.assertNotIn("transcript_path\"]).read_text", text)
        self.assertNotIn("Path(record[\"transcript_path\"])", text)

    def test_the_evidence_packet_has_no_transcript_key(self):
        text = (Path(watchdog.__file__)).read_text(encoding="utf-8")
        packet = text[text.index("evidence_packet = {") : text.index("commit_being_analyzed")]
        self.assertNotIn("transcript", packet)

    def test_the_portfolio_archive_is_a_local_path_not_a_service(self):
        self.assertEqual(watchdog.DEFAULT_CONFIG["portfolio_archive"], "")
        self.assertNotIn("://", watchdog.DEFAULT_CONFIG["portfolio_projects_dir"])


if __name__ == "__main__":
    unittest.main()
