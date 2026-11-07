import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import metrics
import publish


class MetricsTests(unittest.TestCase):
    def test_project_roots_include_old_and_new_without_duplicates(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE projection_projects(project_id,workspace_root)")
        connection.executemany("INSERT INTO projection_projects VALUES(?,?)", [
            ("old", "D:\\AiStack\\caelestia-windows"),
            ("new", "D:\\Caelestia\\caelestia-windows"),
            ("new", "d:\\caelestia\\caelestia-windows"),
            ("other", "D:\\another-project")])
        self.assertEqual(["new", "old"], metrics.project_ids(connection, [
            "D:/AiStack/caelestia-windows", "D:/Caelestia/caelestia-windows"]))

    def test_public_export_drops_historical_milestones(self):
        text = publish.public_payload({"asOf": "2026-11-07T00:00:00Z"}, {"totals": {}})
        self.assertNotIn("milestones", text)
        self.assertIn("publisherRefreshedAt", text)
        self.assertIn("Not installed-version", text)

    def test_privacy_refuses_old_and_new_paths(self):
        for value in ("D:\\AiStack\\repo", "D:/Caelestia/repo", "C:\\Users\\person",
                      "ghp_abcdefgh1234", "https://linear.app/private"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    publish.public_payload({"value": value}, {})

    def test_public_https_urls_are_not_mistaken_for_drive_paths(self):
        self.assertIn("https://github.com/", publish.public_payload(
            {"url": "https://github.com/KldsSeeGhosts/caelestia-windows"}, {}))

    def test_usage_does_not_double_count_claude_or_copy_proxy_totals(self):
        old = {"byModel": [{"model": "deepseek-v4.1-flash", "inputTokens": 999999}]}
        usage = metrics.usage_metrics([
            ("codex", "gpt-6.1-sol", 2, 100, 30, 80, 0),
            ("claudeAgent", "claude-opus-5-5", 1, 900, 50, 0, 0)],
            {"claude-opus-5-5": [10, 20, 30, 40]}, old)
        self.assertEqual(230, usage["totals"]["totalTokens"])
        self.assertNotIn("deepseek-v4.1-flash", [r["model"] for r in usage["byModel"]])
        self.assertEqual(["claude-opus-5-5", "gpt-6.1-sol"], usage["unpricedModels"])

    def test_copied_history_is_counted_once(self):
        with patch.object(metrics.Path, "exists", return_value=True), patch.object(
                metrics, "command", side_effect=[
                    "aaa 2026-10-24T00:00:00Z\nbbb 2026-10-25T00:00:00Z\n",
                    "bbb 2026-10-25T00:00:00Z\nccc 2026-11-07T00:00:00Z\n"]):
            result = metrics.code_metrics(["old", "new"], "contributor")
        self.assertEqual(3, result["commits"])

    def test_empty_transcript_directory_has_no_fabricated_counts(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(({}, 0), metrics.transcript_metrics(root))

    def test_failed_sources_preserve_freshness_not_false_current_timestamp(self):
        previous = {"asOf": "2026-10-25T00:00:00Z", "t3": {"threads": 4},
                    "code": {"commits": 2}, "pullRequests": 1}
        old_usage = {"asOf": "2026-10-25T00:00:00Z", "byModel": []}
        with patch.object(metrics, "read", side_effect=[previous, old_usage]), \
             patch.object(metrics, "tally_t3", side_effect=ValueError("private failure")), \
             patch.object(metrics, "transcript_metrics", return_value=({}, 0)), \
             patch.object(metrics, "code_metrics", side_effect=ValueError("private failure")), \
             patch.object(metrics, "public_repository_metrics", side_effect=ValueError("private failure")):
            project, usage = metrics.collect({
                "runtime": "unused", "workspaceRoots": [], "author": "contributor",
                "publicRepositories": []})
        self.assertEqual(previous["asOf"], project["asOf"])
        self.assertEqual(old_usage, usage)
        self.assertEqual("stale", project["sources"]["t3"]["state"])
        self.assertNotIn("private failure", json.dumps(project))

    def test_transcript_message_and_agent_ids_are_deduplicated(self):
        message = {"message": {"id": "message-1", "model": "claude-opus-5-5",
                   "usage": {"output_tokens": 20},
                   "content": [{"type": "tool_use", "name": "Agent", "id": "tool-1"}]}}
        with tempfile.TemporaryDirectory() as root:
            for directory in ("D--AiStack-caelestia-windows", "D--Caelestia-caelestia-windows"):
                path = Path(root) / directory
                path.mkdir()
                (path / "session.jsonl").write_text(json.dumps(message) + "\n")
            models, agents = metrics.transcript_metrics(root)
        self.assertEqual(1, agents)
        self.assertEqual(20, models["claude-opus-5-5"][1])

    @unittest.skipUnless(sys.platform == "win32", "Windows byte lock")
    def test_concurrent_publisher_is_refused_and_lock_is_released(self):
        with tempfile.TemporaryDirectory() as root:
            with publish.exclusive_run(Path(root)):
                with self.assertRaises(OSError):
                    with publish.exclusive_run(Path(root)):
                        self.fail("A second publisher acquired the same lock")
            with publish.exclusive_run(Path(root)):
                pass


if __name__ == "__main__":
    unittest.main()
