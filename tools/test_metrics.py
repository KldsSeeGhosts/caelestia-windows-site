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

    def test_site_publishes_the_release_resolver_and_metrics_together(self):
        page, download = publish.public_site("https://kldsseeghosts.github.io/caelestia-windows/")
        self.assertIn('src="release-download.js"', page)
        self.assertIn("status-data.js", page)
        self.assertIn("nexus-channels/v1/development.dsse.json", download)
        self.assertNotIn("latestDownload()", page)
        self.assertTrue({"index.html", "release-download.js", "status-data.js"} <= set(publish.PUBLIC_FILES))

    def test_usage_does_not_double_count_claude_or_copy_proxy_totals(self):
        old = {"byModel": [{"model": "deepseek-v4.1-flash", "inputTokens": 999999}]}
        usage = metrics.usage_metrics([
            ("codex", "gpt-6.1-sol", 2, 100, 30, 80, 0),
            ("claudeAgent", "claude-opus-5-5", 1, 900, 50, 0, 0)],
            {"claude-opus-5-5": [10, 20, 30, 40]}, old)
        self.assertEqual(230, usage["totals"]["recordedTokenEvents"])
        self.assertIsNone(usage["totals"]["totalTokens"])
        self.assertIsNone(usage["totals"]["apiCostUsd"])
        self.assertNotIn("deepseek-v4.1-flash", [r["model"] for r in usage["byModel"]])
        self.assertEqual(["claude-opus-5-5", "gpt-6.1-sol"], usage["unpricedModels"])

    def test_missing_usage_is_unknown_and_not_free_or_zero_tokens(self):
        usage = metrics.usage_metrics([
            ("antigravity", "gemini-unreported", 3, None, None, None, None),
            ("codex", "gpt-partial", 2, 100, 20, None, None)], {}, {"byModel": []})
        models = {row["model"]: row for row in usage["byModel"]}
        self.assertIsNone(models["gemini-unreported"]["inputTokens"])
        self.assertIsNone(models["gemini-unreported"]["outputTokens"])
        self.assertIsNone(models["gemini-unreported"]["apiCostUsd"])
        self.assertIsNone(models["gpt-partial"]["inputTokens"])
        self.assertEqual(20, models["gpt-partial"]["outputTokens"])
        self.assertEqual(["gemini-unreported", "gpt-partial"], usage["unknownUsageModels"])
        self.assertEqual("incomplete-not-reconciled", usage["accountingStatus"])

    def test_reference_estimates_do_not_replace_unrelated_historical_rates(self):
        historical = {"input": 1, "output": 2, "cacheRead": 0.5, "cacheWrite": 0}
        usage = metrics.usage_metrics([
            ("codex", "gpt-6.2-sol", 1, 2_000_000, 1_000_000, 1_000_000, 1_000_000),
            ("codex", "gpt-6.1-sol", 1, 1_000_000, 0, 0, 0)],
            {}, {"byModel": [{"model": "gpt-6.1-sol", "pricePerMTok": historical}]},
            metrics.read(metrics.MODEL_RATES))
        models = {row["model"]: row for row in usage["byModel"]}
        self.assertEqual(14.6, models["gpt-6.2-sol"]["apiCostUsd"])
        self.assertEqual(1, models["gpt-6.1-sol"]["apiCostUsd"])
        self.assertEqual(15.6, usage["totals"]["knownRateEstimateUsd"])
        self.assertIsNone(usage["totals"]["apiCostUsd"])

    def test_price_only_aliases_and_provider_variants_keep_separate_usage(self):
        historical = {"input": 0.1, "output": 0.3, "cacheRead": 0.01, "cacheWrite": 0}
        glm = r"builtin:zai-coding-plan\GLM-5.3"
        usage = metrics.usage_metrics([
            ("zcode", glm, 2, None, None, None, None),
            ("zcode", "GLM-5.3", 1, 1_000_000, 0, 0, 0),
            ("deepseek", "deepseek-v4.1-flash", 1, 1_000_000, 0, 0, 0),
            ("cursor", "deepseek-v41-flash-1017", 1, 1_000_000, 0, 0, 0),
            ("deepseek", "default", 1, 1_000_000, 0, 0, 0)],
            {}, {"byModel": [{"model": model, "pricePerMTok": historical}
                             for model in ("GLM-5.3", "deepseek-v4.1-flash")]},
            metrics.read(metrics.MODEL_RATES))
        models = {row["model"]: row for row in usage["byModel"]}
        self.assertEqual(historical, models[glm]["pricePerMTok"])
        self.assertEqual(2, models[glm]["runs"])
        self.assertIsNone(models[glm]["apiCostUsd"])
        self.assertEqual(1, models["GLM-5.3"]["runs"])
        self.assertEqual(0.1, models["deepseek-v4.1-flash"]["apiCostUsd"])
        self.assertEqual(0.3, models["deepseek-v41-flash-1017"]["apiCostUsd"])
        self.assertEqual([glm], usage["unknownUsageModels"])
        self.assertEqual(["default"], usage["unpricedModels"])
        self.assertIn("pricePerMTok", publish.public_payload({}, usage))

    def test_missing_rate_prevents_estimate_only_for_nonzero_recorded_counter(self):
        usage = metrics.usage_metrics([
            ("codex", "swe-2", 1, 2_000_000, 1_000_000, 1_000_000, 0),
            ("acpRegistry_devin", "swe-2-high", 1, 2_000_000, 1_000_000, 1_000_000, 1),
            ("acpRegistry_devin", "swe-2-flash-medium", 1, None, None, None, None)],
            {}, {}, metrics.read(metrics.MODEL_RATES))
        models = {row["model"]: row for row in usage["byModel"]}
        self.assertEqual(18.3, models["swe-2"]["apiCostUsd"])
        self.assertIsNone(models["swe-2-high"]["pricePerMTok"]["cacheWrite"])
        self.assertIsNone(models["swe-2-high"]["apiCostUsd"])
        self.assertIsNotNone(models["swe-2-flash-medium"]["pricePerMTok"])
        self.assertIsNone(models["swe-2-flash-medium"]["apiCostUsd"])

    def test_synthetic_model_rows_are_not_usage(self):
        usage = metrics.usage_metrics([("claudeAgent", "<synthetic>", 1, 1000, 1000, 0, 0)],
                                      {}, {"byModel": []})
        self.assertEqual([], usage["byModel"])
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "D--Caelestia-caelestia-windows"
            path.mkdir()
            (path / "session.jsonl").write_text(json.dumps({"message": {"id": "synthetic",
                "model": "<synthetic>", "usage": {"input_tokens": 1000}}}) + "\n")
            self.assertEqual(({}, 0), metrics.transcript_metrics(root))

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
