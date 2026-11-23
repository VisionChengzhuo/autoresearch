#!/bin/sh
# Independent of ar.py. Queue alerts on the same Feishu route as amb.
set -eu
exec python3 - "${1:-${AR_ROOT:-$HOME/autoresearch/campaigns}}" "${AR_WATCHDOG_NOW:-}" <<'PY'
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

now = float(sys.argv[2]) if sys.argv[2] else time.time()
failure_limit = int(os.environ.get("AR_WATCHDOG_FAILURE_LIMIT", "3"))
if failure_limit < 1:
    sys.exit("AR_WATCHDOG_FAILURE_LIMIT must be positive")
failed = False
amb = os.environ.get("AR_AMB", str(pathlib.Path.home() / "bin" / "amb"))

def notify(campaign, key, message):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as body:
        body.write(message + "\n")
        body.flush()
        return subprocess.run(
            [amb, "align", body.name, "--title", f"自动研究 {campaign} · 心跳告警",
             "--source", f"ar-watchdog-{campaign}", "--key", str(key)],
            capture_output=True, timeout=25, check=True)

for path in sorted(pathlib.Path(sys.argv[1]).glob("*/state.json")):
    try:
        state = json.loads(path.read_text())
        if state["status"] not in {"running", "throttled"} or (path.parent / "STOP").exists():
            continue
        pulse = state.get("heartbeat") or state["last_review"]
        elapsed = now - pulse
        failures = state.get("tick_failures", 0)
        reasons = []
        if failures >= failure_limit:
            reasons.append(f"{failures} consecutive AutoResearch tick failures")
        if elapsed > 600:
            reasons.append(f"AutoResearch heartbeat stale ({int(elapsed)} seconds)")
        if not reasons:
            continue
        key = f"failures-{state.get('tick_failure_since', pulse)}" if failures >= failure_limit else pulse
        message = f"{path.parent.name}: " + "; ".join(reasons)
    except (OSError, ValueError, TypeError, KeyError) as error:
        key = "unreadable"
        message = f"{path.parent.name}: unreadable campaign heartbeat: {error}"
    print(message, file=sys.stderr)
    try:
        notify(path.parent.name, key, message)
    except (OSError, subprocess.SubprocessError):
        print(f"{path.parent.name}: could not queue Feishu heartbeat alert", file=sys.stderr)
    failed = True
sys.exit(int(failed))
PY
