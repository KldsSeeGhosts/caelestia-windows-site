"""Read project-scoped local metrics. No credentials, remote billing or message text."""
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess

PROVIDERS = {"codex": "Codex", "claudeAgent": "Claude Code",
             "deepseek": "DeepSeek", "zcode": "GLM", "antigravity": "Gemini",
             "cursor": "Cursor"}
MODEL_RATES = Path(__file__).resolve().parents[1] / "source" / "model-rates.json"
TOKEN_RATES = (("inputTokens", "input"), ("outputTokens", "output"),
               ("cacheReadTokens", "cacheRead"), ("cacheWriteTokens", "cacheWrite"))


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def command(args):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          encoding="utf-8", timeout=90).stdout


def report_failure(source, error):
    # Private task log only, never exported into public source-state metadata.
    detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
    import re
    detail = re.sub(r"(?i)(?:gh[pousr]_[A-Za-z0-9]+|sk-[A-Za-z0-9_-]+|bearer\s+\S+)",
                    "[credential redacted]", str(detail or "no command output"))
    print(f"Metric source {source} failed: {type(error).__name__}: {detail[:2000]}")


def project_ids(connection, roots):
    wanted = {str(root).replace("/", "\\").rstrip("\\").casefold() for root in roots}
    return sorted({row[0] for row in connection.execute(
        "SELECT project_id, workspace_root FROM projection_projects")
        if str(row[1]).replace("/", "\\").rstrip("\\").casefold() in wanted})


def tally_t3(database, roots):
    connection = sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        ids = project_ids(connection, roots)
        if not ids:
            raise ValueError("No configured project found")
        slots = ",".join("?" for _ in ids)
        threads = f"(SELECT thread_id FROM orchestration_v2_projection_threads WHERE project_id IN ({slots}))"
        count, first = connection.execute(
            f"SELECT COUNT(*),MIN(created_at) FROM orchestration_v2_projection_threads WHERE project_id IN ({slots})", ids).fetchone()
        grouped = lambda table: {
            PROVIDERS.get(provider, provider): number for provider, number in connection.execute(
                f"SELECT provider,COUNT(*) FROM {table} WHERE thread_id IN {threads} GROUP BY provider", ids)}
        stats = {
            "threads": count, "firstThreadAt": first,
            "delegated": grouped("orchestration_v2_projection_subagents"),
            "runs": grouped("orchestration_v2_projection_runs"),
            "providerTurns": connection.execute(
                f"SELECT COUNT(*) FROM orchestration_v2_projection_provider_turns WHERE thread_id IN {threads}", ids).fetchone()[0],
            "userMessages": connection.execute(
                f"SELECT COUNT(*) FROM orchestration_v2_projection_messages WHERE thread_id IN {threads} AND role='user'", ids).fetchone()[0]}
        # Runs keep their original model attribution.
        runs = connection.execute(f"""
            SELECT COALESCE(r.provider,t.default_provider),
              COALESCE(json_extract(r.payload_json,'$.modelSelection.model'),
                       json_extract(t.payload_json,'$.modelSelection.model')),
              COUNT(DISTINCT r.run_id)
            FROM orchestration_v2_projection_threads t
            LEFT JOIN orchestration_v2_projection_runs r ON t.thread_id=r.thread_id
            WHERE t.project_id IN ({slots}) GROUP BY 1,2""", ids).fetchall()
        # Usage comes from every provider turn in the project's threads. T3 stores
        # delegated (subagent) turns with no run attempt, so joining through
        # run_attempts silently dropped all subagent work. Those turns name their
        # own model; root turns keep the run's model as before.
        usage = connection.execute(f"""
            SELECT COALESCE(r.provider,pth.provider,t.default_provider),
              COALESCE(json_extract(r.payload_json,'$.modelSelection.model'),
                       json_extract(pt.payload_json,'$.model'),
                       json_extract(pt.payload_json,'$.turnTokenUsage.models[#-1]'),
                       json_extract(t.payload_json,'$.modelSelection.model')),
              SUM(json_extract(pt.payload_json,'$.turnTokenUsage.inputTokens')),
              SUM(json_extract(pt.payload_json,'$.turnTokenUsage.outputTokens')),
              SUM(json_extract(pt.payload_json,'$.turnTokenUsage.cachedInputTokens')),
              SUM(json_extract(pt.payload_json,'$.turnTokenUsage.cacheCreationTokens')),
              SUM(pt.run_attempt_id IS NULL)
            FROM orchestration_v2_projection_provider_turns pt
            JOIN orchestration_v2_projection_threads t ON t.thread_id=pt.thread_id
            LEFT JOIN orchestration_v2_projection_run_attempts ra ON ra.attempt_id=pt.run_attempt_id
            LEFT JOIN orchestration_v2_projection_runs r ON r.run_id=ra.run_id
            LEFT JOIN orchestration_v2_projection_provider_threads pth
              ON pth.provider_thread_id=pt.provider_thread_id
            WHERE t.project_id IN ({slots}) GROUP BY 1,2""", ids).fetchall()
        stats["subagentTurns"] = sum(row[6] or 0 for row in usage)
        rows = merge_t3_rows(runs, usage)
        return stats, rows
    finally:
        connection.close()


def merge_t3_rows(runs, usage):
    """Combine run counts and provider-turn usage into (provider, model, runs, in, out, cached, created)."""
    merged = {}
    def key(provider, model):
        return (PROVIDERS.get(provider, provider), canonical_model(model) or model)
    for provider, model, count in runs:
        row = merged.setdefault(key(provider, model), [provider, model, 0, None, None, None, None])
        row[2] += count or 0
    for provider, model, inp, out, cached, created, _children in usage:
        row = merged.setdefault(key(provider, model), [provider, model, 0, None, None, None, None])
        for index, value in zip(range(3, 7), (inp, out, cached, created)):
            if value is not None:
                row[index] = (row[index] or 0) + int(value)
    return [tuple(row) for row in merged.values()]


CHANNELS = ("input", "output", "cacheRead", "cacheWrite", "cacheWrite1h")
AGENT_TOOLS = {"Agent", "Task"}


def _jsonl_lines(file):
    if file.name.endswith(".zst"):
        try:
            from compression import zstd  # Python 3.14+
        except ImportError:
            return
        with zstd.open(file, "rt", encoding="utf-8", errors="replace") as source:
            yield from source
        return
    with file.open(encoding="utf-8", errors="replace") as source:
        yield from source


def _usage_record(model, usage):
    split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    written = int(usage.get("cache_creation_input_tokens") or 0)
    hour = int(split.get("ephemeral_1h_input_tokens") or 0) if split else 0
    hour = min(hour, written)
    return {"model": model, "speed": "fast" if usage.get("speed") == "fast" else "standard",
            "input": int(usage.get("input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
            "cacheRead": int(usage.get("cache_read_input_tokens") or 0),
            "cacheWrite": written - hour, "cacheWrite1h": hour}


def _keep(records, key, record):
    """Streaming and copied transcripts repeat a request with partial usage: keep the max per channel."""
    existing = records.get(key)
    if existing is None:
        records[key] = record
        return
    for channel in CHANNELS:
        existing[channel] = max(existing[channel], record[channel])
    if record["speed"] == "fast":
        existing["speed"] = "fast"


def transcript_metrics(projects, ledger=None):
    """Per-request Claude usage, deduplicated across copied transcripts and worktrees.

    ``ledger`` is a private runtime file of token counts by request key. Claude
    Code deletes old transcripts (30 days by default), so counts already seen
    are kept there and history never shrinks. It holds no message text.
    """
    records, agents = {}, set()
    ledger = Path(ledger) if ledger else None
    if ledger and ledger.exists():
        for key, record in read(ledger).get("requests", {}).items():
            if isinstance(record, dict) and record.get("model"):
                _keep(records, key, {"model": record["model"], "speed": record.get("speed", "standard"),
                                     **{channel: int(record.get(channel) or 0) for channel in CHANNELS}})
        agents.update(read(ledger).get("agents", []))
    for directory in Path(projects).glob("*"):
        if not any(name in directory.name.casefold()
                   for name in ("caelestia-windows", "caelestia-quickshell-windows")):
            continue
        for file in sorted(directory.rglob("*.jsonl")) + sorted(directory.rglob("*.jsonl.zst")):
            for line in _jsonl_lines(file):
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    continue
                message = entry.get("message") if isinstance(entry, dict) else None
                if not isinstance(message, dict):
                    continue
                identifier, usage, model = message.get("id"), message.get("usage"), message.get("model")
                if identifier and isinstance(usage, dict) and model not in (None, "<synthetic>"):
                    key = identifier  # API message ids are unique per request
                    _keep(records, key, _usage_record(model, usage))
                    for index, extra in enumerate(usage.get("iterations") or []):
                        if (isinstance(extra, dict) and extra.get("type") == "advisor_message"
                                and extra.get("model") not in (None, "<synthetic>")):
                            _keep(records, f"{key}:advisor:{index}", _usage_record(extra["model"], extra))
                for block in message.get("content") or []:
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and block.get("name") in AGENT_TOOLS and block.get("id")):
                        agents.add(block["id"])
    if ledger:
        atomic_json(ledger, {"requests": records, "agents": sorted(agents)})
    models = {}
    for record in records.values():
        speeds = models.setdefault(record["model"], {})
        bucket = speeds.setdefault(record["speed"], dict.fromkeys(CHANNELS, 0))
        for channel in CHANNELS:
            bucket[channel] += record[channel]
    return models, len(agents)


def canonical_model(model):
    if isinstance(model, str) and model.startswith("["):
        try:
            model = json.loads(model)[-1]
        except (ValueError, TypeError, IndexError):
            return None
    return model.split("/")[-1] if isinstance(model, str) else None


def _bucket_cost(bucket, rate, multiplier=1.0):
    """USD for one speed bucket, or None when a nonzero counter has no rate."""
    total = 0.0
    for channel in CHANNELS:
        tokens = bucket.get(channel) or 0
        if not tokens:
            continue
        price = rate.get(channel)
        if price is None:
            return None
        total += tokens * float(price) * multiplier
    return total


def usage_metrics(rows, transcripts, previous, reference_prices=None):
    """T3 scoped usage, with deduplicated Claude transcript usage instead of double-counting."""
    reference_prices = reference_prices or {}
    # The committed reference file wins over rates carried forward in the
    # previous snapshot, so a wrong historical rate can be corrected.
    rates = {row["model"]: row for snapshot in (previous, reference_prices)
             for row in snapshot.get("byModel", []) if row.get("pricePerMTok")}
    aliases = reference_prices.get("aliases", {})
    fields = tuple(field for field, _ in TOKEN_RATES)
    models = {}
    for provider, model, runs, inp, out, cached, created in rows:
        model = canonical_model(model)
        if not model or model == "<synthetic>":
            continue
        row = models.setdefault(model, {"provider": PROVIDERS.get(provider, provider), "runs": 0,
                                       "inputTokens": None, "outputTokens": None,
                                       "cacheReadTokens": None, "cacheWriteTokens": None})
        row["runs"] += runs
        # T3's inputTokens already include cache reads and cache writes (Claude
        # and Codex alike), so both come off to leave uncached input.
        uncached = (max(0, int(inp) - int(cached) - int(created or 0))
                    if inp is not None and cached is not None else None)
        counters = {"inputTokens": uncached, "outputTokens": out,
                    "cacheReadTokens": cached, "cacheWriteTokens": created}
        for field, value in counters.items():
            if value is not None:
                row[field] = (row[field] or 0) + int(value)
    speeds_by_model = {}
    for model, speeds in transcripts.items():
        row = models.setdefault(model, {"provider": "Anthropic", "runs": 0})
        merged = {channel: sum(bucket.get(channel, 0) for bucket in speeds.values())
                  for channel in CHANNELS}
        row.update({"inputTokens": merged["input"], "outputTokens": merged["output"],
                    "cacheReadTokens": merged["cacheRead"],
                    "cacheWriteTokens": merged["cacheWrite"] + merged["cacheWrite1h"],
                    "cacheWrite1hTokens": merged["cacheWrite1h"],
                    "fastTokens": sum(speeds.get("fast", {}).values())})
        speeds_by_model[model] = speeds
    result = []
    for model, values in models.items():
        row = {"model": model, **values}
        price = rates.get(model) or rates.get(aliases.get(model))
        row["apiCostUsd"] = None
        if price:
            rate = row["pricePerMTok"] = price["pricePerMTok"]
            row["priceSource"] = price.get("priceSource")
            if model in speeds_by_model:
                # Per-request buckets: 1-hour cache writes and fast mode have
                # their own published rates.
                total = 0.0
                for speed, bucket in speeds_by_model[model].items():
                    multiplier = 1.0
                    if speed == "fast" and any(bucket.values()):
                        multiplier = price.get("fastMultiplier")
                    cost = None if multiplier is None else _bucket_cost(bucket, rate, float(multiplier))
                    if cost is None:
                        total = None
                        break
                    total += cost
                row["apiCostUsd"] = None if total is None else round(total / 1e6, 2)
            # A missing rate only matters for dollars if a recorded counter is
            # nonzero. Missing counters are never treated as zero.
            elif all(row[field] is not None and (row[field] == 0 or rate.get(key) is not None)
                     for field, key in TOKEN_RATES):
                row["apiCostUsd"] = round(sum(row[field] * float(rate[key])
                    for field, key in TOKEN_RATES if row[field] != 0) / 1e6, 2)
        result.append(row)
    totals = {field: sum(row[field] or 0 for row in result) for field in fields}
    # Preserve observed counters for inspection, not a project-wide accounting
    # claim. Repeated cached input is a token event, not unique text. Coverage
    # and historical price semantics have not been independently reconciled.
    totals["recordedTokenEvents"] = sum(totals.values())
    totals["knownRateEstimateUsd"] = round(sum(row["apiCostUsd"] or 0 for row in result), 2)
    totals["totalTokens"] = None
    totals["apiCostUsd"] = None
    return {
        "asOf": now(), "totals": totals, "byModel": result,
        "accountingStatus": "incomplete-not-reconciled",
        "coverage": "Partial recorded token events, not unique tokens or a project-wide total. Includes delegated subagent turns that T3 records. Provider coverage and counter semantics are not fully reconciled. Missing usage remains unknown. Claude transcript counters replace matching T3 model counters and are priced per request, including 1-hour cache writes and fast mode; account-wide proxy totals are excluded. Other estimates use stored reference rates without reconstructing historical changes or plan discounts. They are neither a bill nor a complete project cost.",
        "unpricedModels": sorted(row["model"] for row in result if not row.get("pricePerMTok")),
        "unknownUsageModels": sorted(row["model"] for row in result if any(row[field] is None for field in fields))}


def code_metrics(roots, author):
    """Union contributor commit IDs across old/new repos, never count a copied commit twice."""
    revisions = {}
    for root in roots:
        if not Path(root).exists():
            continue
        for row in command(["git", "-C", root, "log", "--all",
                            f"--author={author}", "--format=%H %aI"]).splitlines():
            identifier, date = row.split(" ", 1)
            revisions.setdefault(identifier, (root, date))
    if not revisions:
        raise ValueError("No project history found")
    return {"startedAt": min(date for _, date in revisions.values()),
            "commits": len(revisions),
            "coverage": "Unique contributor commits in configured current and archived local project histories."}


def public_repository_metrics(repositories):
    snapshots = []
    for repository in repositories:
        owner, name = repository.split("/")
        query = """query($owner:String!,$name:String!){repository(owner:$owner,name:$name){
          nameWithOwner url isArchived
          pullRequests{totalCount} openPulls:pullRequests(states:OPEN){totalCount}
          mergedPulls:pullRequests(states:MERGED){totalCount}
          issues{totalCount} openIssues:issues(states:OPEN){totalCount}
          defaultBranchRef{target{oid}}}}"""
        response = json.loads(command(["gh", "api", "graphql", "-f", f"query={query}",
                                      "-f", f"owner={owner}", "-f", f"name={name}"]))
        if response.get("errors"):
            raise ValueError("Repository query failed")
        snapshots.append(response["data"]["repository"])
    return {"pullRequests": sum(row["pullRequests"]["totalCount"] for row in snapshots),
            "repositories": snapshots, "asOf": now(),
            "coverage": "Public shell and backend repositories only; not private-archive PRs or Epic acceptance."}


def collect(config):
    runtime = Path(config["runtime"])
    previous = read(runtime / "project.json")
    old_usage = read(runtime / "usage.json")
    prices = read(config["pricingSnapshot"]) if config.get("pricingSnapshot") else old_usage
    project = dict(previous)
    states = {}
    home = Path.home()
    rows = None
    try:
        project["t3"], rows = tally_t3(home / ".t3/userdata/statev2.sqlite",
                                      config["workspaceRoots"])
        states["t3"] = {"state": "fresh", "asOf": now()}
    except Exception as error:
        report_failure("t3", error)
        states["t3"] = {"state": "stale", "asOf": previous.get("sources", {}).get("t3", {}).get("asOf", previous.get("asOf"))}
    try:
        transcripts, project["claudeAgents"] = transcript_metrics(
            home / ".claude/projects", runtime / "claude-usage-ledger.json")
        states["claude"] = {"state": "fresh", "asOf": now()}
    except Exception as error:
        report_failure("claude", error)
        transcripts = {}
        states["claude"] = {"state": "stale", "asOf": previous.get("asOf")}
    for key, operation in (
        ("code", lambda: code_metrics(config["workspaceRoots"], config["author"])),
        ("github", lambda: public_repository_metrics(config["publicRepositories"]))):
        try:
            value = operation()
            if key == "github":
                project.update({"pullRequests": value["pullRequests"], "github": value})
            else:
                project[key] = value
            states[key] = {"state": "fresh", "asOf": now()}
        except Exception as error:
            report_failure(key, error)
            states[key] = {"state": "stale", "asOf": previous.get("sources", {}).get(key, {}).get("asOf", previous.get("asOf"))}
    project["sources"] = states
    project["collectedAt"] = now()
    project["asOf"] = now() if all(row["state"] == "fresh" for row in states.values()) else previous.get("asOf")
    usage = (usage_metrics(rows, transcripts, prices, read(MODEL_RATES))
             if rows is not None and states["claude"]["state"] == "fresh" else old_usage)
    return project, usage
