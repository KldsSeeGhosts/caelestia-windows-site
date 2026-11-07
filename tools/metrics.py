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
        rows = connection.execute(f"""
            SELECT COALESCE(r.provider,t.default_provider),
              COALESCE(json_extract(r.payload_json,'$.modelSelection.model'),
                       json_extract(t.payload_json,'$.modelSelection.model')),
              COUNT(DISTINCT r.run_id),
              SUM(json_extract(pt.payload_json,'$.turnTokenUsage.inputTokens')),
              SUM(json_extract(pt.payload_json,'$.turnTokenUsage.outputTokens')),
              SUM(json_extract(pt.payload_json,'$.turnTokenUsage.cachedInputTokens')),
              SUM(json_extract(pt.payload_json,'$.turnTokenUsage.cacheCreationTokens'))
            FROM orchestration_v2_projection_threads t
            LEFT JOIN orchestration_v2_projection_runs r ON t.thread_id=r.thread_id
            LEFT JOIN orchestration_v2_projection_run_attempts ra ON r.run_id=ra.run_id
            LEFT JOIN orchestration_v2_projection_provider_turns pt ON ra.attempt_id=pt.run_attempt_id
            WHERE t.project_id IN ({slots}) GROUP BY 1,2""", ids).fetchall()
        return stats, rows
    finally:
        connection.close()


def transcript_metrics(projects):
    """Deduplicate message/tool-call IDs across copied transcripts/worktrees."""
    messages, agents = {}, set()
    for directory in Path(projects).glob("*"):
        if not any(name in directory.name.casefold()
                   for name in ("caelestia-windows", "caelestia-quickshell-windows")):
            continue
        for file in directory.rglob("*.jsonl"):
            with file.open(encoding="utf-8", errors="replace") as source:
                for line in source:
                    try:
                        entry = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    message = entry.get("message")
                    if not isinstance(message, dict):
                        continue
                    identifier = message.get("id")
                    if identifier and message.get("usage") and message.get("model"):
                        messages[identifier] = (message["model"], message["usage"])
                    for block in message.get("content", []):
                        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Agent" and block.get("id"):
                            agents.add(block["id"])
    models = {}
    for model, usage in messages.values():
        row = models.setdefault(model, [0, 0, 0, 0])
        for index, key in enumerate(("input_tokens", "output_tokens",
                                     "cache_read_input_tokens", "cache_creation_input_tokens")):
            row[index] += int(usage.get(key, 0))
    return models, len(agents)


def canonical_model(model):
    if isinstance(model, str) and model.startswith("["):
        try:
            model = json.loads(model)[-1]
        except (ValueError, TypeError, IndexError):
            return None
    return model.split("/")[-1] if isinstance(model, str) else None


def usage_metrics(rows, transcripts, previous):
    """T3 scoped usage, with deduplicated Claude transcript usage instead of double-counting."""
    rates = {row["model"]: row for row in previous.get("byModel", [])}
    models = {}
    for provider, model, runs, inp, out, cached, created in rows:
        model = canonical_model(model)
        if not model:
            continue
        row = models.setdefault(model, {"provider": PROVIDERS.get(provider, provider), "runs": 0,
                                       "inputTokens": 0, "outputTokens": 0,
                                       "cacheReadTokens": 0, "cacheWriteTokens": 0})
        row["runs"] += runs
        row["inputTokens"] += max(0, int(inp or 0) - int(cached or 0))
        row["outputTokens"] += int(out or 0)
        row["cacheReadTokens"] += int(cached or 0)
        row["cacheWriteTokens"] += int(created or 0)
    for model, tokens in transcripts.items():
        row = models.setdefault(model, {"provider": "Anthropic", "runs": 0})
        row.update(zip(("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"), tokens))
    result = []
    for model, values in models.items():
        row = {"model": model, **values}
        rate = rates.get(model, {}).get("pricePerMTok")
        if rate:
            row["pricePerMTok"] = rate
            row["priceSource"] = rates[model].get("priceSource")
            row["apiCostUsd"] = round(sum(
                row[key] * float(rate[rkey]) for key, rkey in (
                    ("inputTokens", "input"), ("outputTokens", "output"),
                    ("cacheReadTokens", "cacheRead"), ("cacheWriteTokens", "cacheWrite"))) / 1e6, 2)
        else:
            row["apiCostUsd"] = None
        result.append(row)
    fields = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")
    totals = {field: sum(row[field] for row in result) for field in fields}
    totals["totalTokens"] = sum(totals.values())
    totals["apiCostUsd"] = round(sum(row["apiCostUsd"] or 0 for row in result), 2)
    return {
        "asOf": now(), "totals": totals, "byModel": result,
        "coverage": "Recorded project-scoped T3 usage and deduplicated Claude transcripts; missing usage is not estimated. Account-wide proxy totals are excluded. Cost is an estimate for known-price models at the previously stored rates, not a bill.",
        "unpricedModels": sorted(row["model"] for row in result if row["apiCostUsd"] is None)}


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
        transcripts, project["claudeAgents"] = transcript_metrics(home / ".claude/projects")
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
    usage = usage_metrics(rows, transcripts, prices) if rows is not None and states["claude"]["state"] == "fresh" else old_usage
    return project, usage
