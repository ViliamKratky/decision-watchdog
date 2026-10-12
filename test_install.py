import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import install


class RemoveOursTests(unittest.TestCase):
    def test_remove_ours_preserves_unrelated_hooks(self):
        settings = {
            "theme": "dark",
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "echo keep"}]},
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python watchdog.py hook --source codex "
                                "# decision-watchdog",
                            }
                        ]
                    },
                ]
            },
        }
        result = install.remove_ours(settings)
        self.assertEqual(result["theme"], "dark")
        self.assertEqual(len(result["hooks"]["Stop"]), 1)
        self.assertEqual(result["hooks"]["Stop"][0]["hooks"][0]["command"], "echo keep")

    def test_event_is_dropped_only_when_nothing_else_remains(self):
        settings = {
            "hooks": {
                "SessionEnd": [
                    {
                        "hooks": [
                            {"type": "command", "command": "watchdog.py # decision-watchdog"}
                        ]
                    }
                ],
                "PreToolUse": [{"hooks": [{"type": "command", "command": "echo other"}]}],
            }
        }
        result = install.remove_ours(settings)
        self.assertNotIn("SessionEnd", result["hooks"])
        self.assertIn("PreToolUse", result["hooks"])


class MergeHooksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "settings.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_existing_settings_and_hooks_are_preserved(self):
        original = {
            "model": "opus",
            "permissions": {"additionalDirectories": ["/somewhere"]},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]},
        }
        self.path.write_text(json.dumps(original), encoding="utf-8")
        install.merge_hooks(self.path, "claude")
        updated = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(updated["model"], "opus")
        self.assertEqual(updated["permissions"], original["permissions"])
        commands = [
            handler["command"]
            for group in updated["hooks"]["Stop"]
            for handler in group["hooks"]
        ]
        self.assertIn("echo mine", commands)
        self.assertEqual(sum("decision-watchdog" in c for c in commands), 1)

    def test_a_timestamped_backup_is_written(self):
        self.path.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
        install.merge_hooks(self.path, "codex")
        backups = list(self.path.parent.glob("settings.json.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), {"model": "opus"})

    def test_reinstalling_does_not_duplicate_our_handlers(self):
        install.merge_hooks(self.path, "claude")
        install.merge_hooks(self.path, "claude")
        updated = json.loads(self.path.read_text(encoding="utf-8"))
        for event in ("UserPromptSubmit", "Stop", "SessionEnd"):
            commands = [
                handler["command"]
                for group in updated["hooks"][event]
                for handler in group["hooks"]
            ]
            self.assertEqual(sum("decision-watchdog" in c for c in commands), 1, event)

    def test_all_three_events_are_installed_with_a_session_end_timeout(self):
        install.merge_hooks(self.path, "codex")
        updated = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(updated["hooks"]), ["SessionEnd", "Stop", "UserPromptSubmit"]
        )
        session_end = updated["hooks"]["SessionEnd"][0]["hooks"][0]
        self.assertEqual(session_end["timeout"], 3)
        self.assertIn("--source codex", session_end["command"])

    def test_invalid_json_is_refused_rather_than_overwritten(self):
        self.path.write_text("{ definitely not json", encoding="utf-8")
        with self.assertRaises(SystemExit):
            install.merge_hooks(self.path, "claude")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{ definitely not json")


class ConfigureDefaultsTests(unittest.TestCase):
    def test_an_upgrade_keeps_every_existing_value(self):
        with tempfile.TemporaryDirectory() as temp:
            app_dir = Path(temp)
            existing = {
                "model": "my-model",
                "obsidian_vault": "/my/vault",
                "portfolio_archive": "/my/archive",
                "min_confidence": 0.9,
            }
            (app_dir / "config.json").write_text(json.dumps(existing), encoding="utf-8")
            with mock.patch.object(install, "APP_DIR", app_dir):
                install.configure_defaults()
            config = json.loads((app_dir / "config.json").read_text(encoding="utf-8"))
            for key, value in existing.items():
                self.assertEqual(config[key], value)

    def test_an_upgrade_backfills_only_missing_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            app_dir = Path(temp)
            (app_dir / "config.json").write_text(
                json.dumps({"model": "my-model"}), encoding="utf-8"
            )
            with mock.patch.object(install, "APP_DIR", app_dir):
                install.configure_defaults()
            config = json.loads((app_dir / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["model"], "my-model")
            self.assertIn("portfolio_archive", config)
            self.assertTrue(config["worktree_watch"])
            # A backup of the pre-upgrade file is kept.
            self.assertTrue(list(app_dir.glob("config.json.backup-*")))

    def test_the_archive_path_is_only_prefilled_when_it_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "nope"
            with mock.patch.object(install, "DEFAULT_ARCHIVE", missing):
                self.assertEqual(install.detect_portfolio_archive(), "")
            present = Path(temp) / "archive"
            present.mkdir()
            with mock.patch.object(install, "DEFAULT_ARCHIVE", present):
                self.assertEqual(install.detect_portfolio_archive(), str(present))

    def test_a_missing_config_is_created(self):
        with tempfile.TemporaryDirectory() as temp:
            app_dir = Path(temp)
            with (
                mock.patch.object(install, "APP_DIR", app_dir),
                mock.patch.object(install, "choose_model", return_value="test-model"),
            ):
                install.configure_defaults()
            config = json.loads((app_dir / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["model"], "test-model")
            self.assertTrue(config["ollama_url"].startswith("http://127.0.0.1:"))
            self.assertIn("portfolio_archive", config)
            self.assertEqual(config["portfolio_evidence_dir"], "private/watchdog")


if __name__ == "__main__":
    unittest.main()
