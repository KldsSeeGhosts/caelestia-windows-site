"""Explicit-file, privacy-checked hourly Pages publisher. Config stays outside Git."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
# Explicit sibling import also works with Python -I (no ambient cwd imports).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import metrics

PUBLIC_FILES = ("index.html", "status-data.js", ".nojekyll", "og.png",
                "demo-full.mp4", "demo-full-poster.jpg")
PRIVATE = re.compile(r"(?i)\b[A-Z]:[\\/]|AppData|tail74ed91|\.ts\.net|linear\.app|"
                     r"\bErmin\b|gh[pousr]_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9]{8,}|"
                     r"password|authorization|bearer|@[A-Za-z0-9.-]+\.(?:com|net|org|io|dev)\b")


def public_payload(project, usage):
    # Deliberately omit old milestone narratives: they are not refreshed evidence.
    value = {"project": project, "usage": usage, "updatedAt": project.get("asOf"),
             "publisherRefreshedAt": metrics.now(),
             "statusCoverage": "Public repository activity and recorded local project metrics. Not installed-version, live desktop or hardware acceptance."}
    payload = "window.__STATUS = " + json.dumps(value, ensure_ascii=True) + ";\n"
    if PRIVATE.search(payload):
        raise ValueError("Refusing personal data in public metrics")
    return payload


@contextmanager
def exclusive_run(runtime):
    # OS-held byte lock survives neither a crash nor process exit. Never steal
    # a PID-based lock or allow the hourly task to race a manual publish.
    import msvcrt
    with (runtime / ".publisher.lock").open("a+b") as lock:
        if lock.seek(0, os.SEEK_END) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def publish(config, dry_run=False):
    runtime = Path(config["runtime"])
    project, usage = metrics.collect(config)
    payload = public_payload(project, usage)
    page = (Path(__file__).resolve().parents[1] / "source/showcase.html").read_text(encoding="utf-8")
    if "const MEDIA = null;" not in page:
        raise ValueError("Missing reviewed media marker")
    # The template is the only source of index.html. A page edited directly on
    # a publishing branch is overwritten by the next run, so required
    # behaviour is asserted here.
    for marker in ('data-latest href="${REPO}/releases"', "if (RELEASED) latestDownload();",
                   "Microsoft Visual C++ x64 Redistributable"):
        if marker not in page:
            raise ValueError(f"Template lost required download behaviour: {marker}")
    page = page.replace("const MEDIA = null;", 'const MEDIA = {"demo-loop": false, "demo-full": true};')
    page = page.replace('content="og.png"', f'content="{config["canonicalUrl"]}og.png"')
    if PRIVATE.search(page):
        raise ValueError("Refusing personal data in public page")
    metrics.atomic_json(runtime / "project.json", project)
    metrics.atomic_json(runtime / "usage.json", usage)
    (runtime / "status-data.js").write_text(payload, encoding="utf-8")
    (runtime / "index.html").write_text(page, encoding="utf-8")
    (runtime / ".nojekyll").write_text("", encoding="utf-8")
    for target in config["targets"]:
        root = Path(target["path"])
        origin = metrics.command(["git", "-C", str(root), "remote", "get-url", "origin"]).strip()
        if origin != target["url"]:
            raise ValueError("Publishing destination origin mismatch")
        branch = metrics.command(["git", "-C", str(root), "branch", "--show-current"]).strip()
        if branch != target["branch"]:
            raise ValueError("Publishing destination branch mismatch")
        metrics.command(["git", "-C", str(root), "pull", "--ff-only"])
        if dry_run:
            continue
        for name in PUBLIC_FILES:
            shutil.copyfile(runtime / name, root / name)
        metrics.command(["git", "-C", str(root), "add", "--", *PUBLIC_FILES])
        changed = metrics.command(["git", "-C", str(root), "diff", "--cached", "--name-only"]).strip()
        if not changed:
            print(f"No public changes: {target['branch']}")
            continue
        if set(changed.splitlines()) - set(PUBLIC_FILES):
            raise ValueError("Unreviewed staged files; refusing publication")
        metrics.command(["git", "-C", str(root), "commit", "-m",
                         f"Refresh migrated project metrics ({project.get('asOf')})"])
        metrics.command(["git", "-C", str(root), "push", "origin",
                         f"HEAD:{target['branch']}"])
        print(f"Published {target['branch']}: {project.get('asOf')}")
    states = {name: row["state"] for name, row in project["sources"].items()}
    print(json.dumps({"sources": states, "metricsAsOf": project["asOf"],
                      "publisherRefreshedAt": metrics.now(), "dryRun": dry_run}))
    return 0 if all(state == "fresh" for state in states.values()) else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        config = metrics.read(args.config)
        with exclusive_run(Path(config["runtime"])):
            result = publish(config, args.dry_run)
        raise SystemExit(result)
    except subprocess.CalledProcessError as error:
        metrics.report_failure("publisher", error)
        print(f"Publisher command failed, exit={error.returncode}; earlier public destinations may already have succeeded.",
              file=sys.stderr)
        raise SystemExit(1)
