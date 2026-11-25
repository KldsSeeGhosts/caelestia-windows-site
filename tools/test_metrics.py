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
            {"claude-opus-5-5": {"standard": {"input": 10, "output": 20, "cacheRead": 30,
                                              "cacheWrite": 25, "cacheWrite1h": 15}}}, old)
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
        # 2M input that already contains 1M cache reads and 1M cache writes
        # leaves no uncached input: 1M output, 1M reads and 1M writes remain.
        self.assertEqual(0, models["gpt-6.2-sol"]["inputTokens"])
        self.assertEqual(12.6, models["gpt-6.2-sol"]["apiCostUsd"])
        self.assertEqual(1, models["gpt-6.1-sol"]["apiCostUsd"])
        self.assertEqual(13.6, usage["totals"]["knownRateEstimateUsd"])
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
        self.assertEqual(20, models["claude-opus-5-5"]["standard"]["output"])

    def write_transcript(self, root, name, entries, folder="D--Caelestia-caelestia-windows"):
        path = Path(root) / folder
        path.mkdir(exist_ok=True)
        (path / name).write_text("".join(json.dumps(entry) + "\n" for entry in entries))

    def test_streamed_partial_usage_keeps_the_largest_count_not_the_last(self):
        def chunk(output):
            return {"requestId": "req-1", "message": {"id": "msg-1", "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 5, "output_tokens": output}}}
        with tempfile.TemporaryDirectory() as root:
            self.write_transcript(root, "a.jsonl", [chunk(900), chunk(12)])
            models, _ = metrics.transcript_metrics(root)
        self.assertEqual(900, models["claude-opus-5-5"]["standard"]["output"])
        self.assertEqual(5, models["claude-opus-5-5"]["standard"]["input"])

    def test_task_tool_counts_as_a_delegated_agent(self):
        entry = {"message": {"id": "m", "model": "claude-sonnet-5-5", "usage": {"output_tokens": 1},
                 "content": [{"type": "tool_use", "name": "Task", "id": "t1"},
                             {"type": "tool_use", "name": "Agent", "id": "t2"},
                             {"type": "tool_use", "name": "Bash", "id": "t3"}]}}
        with tempfile.TemporaryDirectory() as root:
            self.write_transcript(root, "a.jsonl", [entry])
            self.assertEqual(2, metrics.transcript_metrics(root)[1])

    def test_one_hour_cache_writes_and_fast_mode_use_their_own_rates(self):
        standard = {"message": {"id": "s", "model": "claude-opus-5-5", "usage": {
            "input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
            "cache_creation_input_tokens": 2_000_000,
            "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 1_000_000}}}}
        fast = {"message": {"id": "f", "model": "claude-opus-5-5", "usage": {
            "output_tokens": 1_000_000, "speed": "fast"}}}
        with tempfile.TemporaryDirectory() as root:
            self.write_transcript(root, "a.jsonl", [standard, fast])
            models, _ = metrics.transcript_metrics(root)
        usage = metrics.usage_metrics([], models, {}, metrics.read(metrics.MODEL_RATES))
        row = usage["byModel"][0]
        # 4 input + 20 output + 0.20 read + 5 (5-minute write) + 8 (1-hour write) + 40 fast output
        self.assertEqual(77.2, row["apiCostUsd"])
        self.assertEqual(2_000_000, row["cacheWriteTokens"])
        self.assertEqual(1_000_000, row["cacheWrite1hTokens"])
        self.assertEqual(1_000_000, row["fastTokens"])

    def test_fast_tokens_without_a_fast_rate_are_not_priced(self):
        fast = {"message": {"id": "f", "model": "claude-sonnet-4-6", "usage": {
            "output_tokens": 10, "speed": "fast"}}}
        with tempfile.TemporaryDirectory() as root:
            self.write_transcript(root, "a.jsonl", [fast])
            models, _ = metrics.transcript_metrics(root)
        usage = metrics.usage_metrics([], models, {}, metrics.read(metrics.MODEL_RATES))
        self.assertIsNone(usage["byModel"][0]["apiCostUsd"])

    def test_reference_rates_correct_a_wrong_carried_forward_rate(self):
        wrong = {"input": 3.0, "output": 15.0, "cacheRead": 0.3, "cacheWrite": 3.75}
        usage = metrics.usage_metrics([], {"claude-sonnet-5-5": {"standard": {
            "input": 0, "output": 1_000_000, "cacheRead": 0, "cacheWrite": 0, "cacheWrite1h": 0}}},
            {"byModel": [{"model": "claude-sonnet-5-5", "pricePerMTok": wrong}]},
            metrics.read(metrics.MODEL_RATES))
        self.assertEqual(10, usage["byModel"][0]["apiCostUsd"])

    def test_ledger_keeps_counts_after_transcripts_are_deleted(self):
        entry = {"message": {"id": "old", "model": "claude-opus-5-5", "usage": {"output_tokens": 70}}}
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as private:
            ledger = Path(private) / "ledger.json"
            self.write_transcript(root, "a.jsonl", [entry])
            metrics.transcript_metrics(root, ledger)
            (Path(root) / "D--Caelestia-caelestia-windows" / "a.jsonl").unlink()
            models, _ = metrics.transcript_metrics(root, ledger)
            self.assertNotIn("content", ledger.read_text())
        self.assertEqual(70, models["claude-opus-5-5"]["standard"]["output"])

    def test_subagent_turns_without_a_run_attempt_are_counted(self):
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript("""
                CREATE TABLE projection_projects(project_id,workspace_root);
                CREATE TABLE orchestration_v2_projection_threads(thread_id,project_id,created_at,default_provider,payload_json);
                CREATE TABLE orchestration_v2_projection_subagents(thread_id,provider);
                CREATE TABLE orchestration_v2_projection_runs(run_id,thread_id,provider,payload_json);
                CREATE TABLE orchestration_v2_projection_run_attempts(attempt_id,run_id);
                CREATE TABLE orchestration_v2_projection_provider_threads(provider_thread_id,provider);
                CREATE TABLE orchestration_v2_projection_provider_turns(provider_turn_id,thread_id,provider_thread_id,run_attempt_id,payload_json);
                CREATE TABLE orchestration_v2_projection_messages(thread_id,role);
            """)
            usage = lambda i, o, c, models=None: json.dumps({"turnTokenUsage": {
                "inputTokens": i, "outputTokens": o, "cachedInputTokens": c, **({"models": models} if models else {})}})
            connection.executemany("INSERT INTO projection_projects VALUES(?,?)", [("p", "D:/Caelestia/caelestia-windows")])
            connection.execute("INSERT INTO orchestration_v2_projection_threads VALUES('t','p','2026-10-25','codex','{}')")
            connection.execute("INSERT INTO orchestration_v2_projection_subagents VALUES('t','codex')")
            connection.execute("""INSERT INTO orchestration_v2_projection_runs VALUES('r','t','codex','{"modelSelection":{"model":"gpt-6.1-sol"}}')""")
            connection.execute("INSERT INTO orchestration_v2_projection_run_attempts VALUES('a','r')")
            connection.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES('main','codex')")
            connection.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES('child','codex')")
            connection.executemany("INSERT INTO orchestration_v2_projection_provider_turns VALUES(?,?,?,?,?)", [
                ("1", "t", "main", "a", usage(100, 10, 60)),
                ("2", "t", "child", None, usage(50, 5, 20, ["gpt-6.2-sol"]))])
            connection.commit()
            connection.close()
            stats, rows = metrics.tally_t3(database, ["D:/Caelestia/caelestia-windows"])
        self.assertEqual(1, stats["subagentTurns"])
        by_model = {row[1]: row for row in rows}
        self.assertEqual(("codex", "gpt-6.1-sol", 1, 100, 10, 60, None), by_model["gpt-6.1-sol"])
        self.assertEqual(("codex", "gpt-6.2-sol", 0, 50, 5, 20, None), by_model["gpt-6.2-sol"])

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
