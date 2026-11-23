#!/usr/bin/env python3
"""Persistent AutoResearch campaign loop. See ar.py --help."""
import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ar_quota

TERMINAL = {"done", "failed", "blocked", "reopened", "cancelled"}
CAMPAIGN_CONCURRENCY = 8
TEAM_CONCURRENCY = 2
HOUR = 3600
TIMING = {"review_earliest": 3 * HOUR, "review_interval": 6 * HOUR,
          "review_late": 9 * HOUR, "block_escalation": 2 * HOUR,
          "ruling_fallback": 600, "ruling_late": 1800,
          "progress_min": HOUR, "progress_max": 2 * HOUR}
MODELS = {"team": "opus", "review": "fable", "ruling": "fable", "temporary_ruling": "opus"}
EXEC_MODELS = ("sol", "opus", "fable", "astra")
ACCEPTANCE_STATES = {"In Review", "Human Intervention"}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
CARD = re.compile(r"[A-Z][A-Z0-9]+-[0-9]+")
OUTBOX_LABEL = {"charter": "章程", "review": "大评审决定", "daily_review": "日报", "ruling": "裁决",
                "owner_response": "给你的答复", "alert": "告警", "acceptance": "验收申请", "question": "请决定"}
TRACKED = ("config.json", "charter.md", "state.json", "inbox.jsonl", "outbox.jsonl",
           "events.jsonl", "ledger.jsonl", "ops-ledger.jsonl", "leaderboard.md", "forum.md",
           "survey.md", "slots.json", "teams", "reviews", "questions")


def atomic(path, content):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8",
                                     delete=False) as temp:
        temp.write(content)
        temp.flush()
        os.fsync(temp.fileno())
    try:
        os.replace(temp.name, path)
    finally:
        Path(temp.name).unlink(missing_ok=True)


def save(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def rows(path):
    if path.exists():
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    return []


def append(path, record):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def command(*args, cwd=None):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError(f"{Path(args[0]).name} {args[1]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def timestamp(value):
    if isinstance(value, (float, int)):
        return value
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires a timezone")
    return parsed.timestamp()


def owner_timestamp(value):
    if type(value) not in (str, int, float):
        raise ValueError("invalid owner message timestamp")
    received = timestamp(value)
    if not math.isfinite(received):
        raise ValueError("invalid owner message timestamp")
    return received


def sha(path):
    with Path(path).open("rb") as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


def option(name, default):
    return os.environ.get(name, default)


def ruling_kind(job):
    if "ruling_kind" in job:
        return job["ruling_kind"]
    # Pre-handoff records used Opus exclusively for temporary rulings.
    return "temporary_ruling" if job["model"] == "opus" else "ruling"


def asks_lead(block):
    if "ask_lead" in block:
        return block["ask_lead"]
    return "领队" in str(block["who"]) or str(block["what"]).startswith("请求领队裁决")


def covers_block(job, message):
    for assigned in job["messages"]:
        if assigned.get("team") == message["team"]:
            if "block_time" in assigned:
                if assigned["block_time"] == message["block_time"]:
                    return True
            elif job["time"] >= message["block_time"] and assigned["text"] == message["text"]:
                return True
    return False


def ledger_record(raw):
    run = json.loads(raw.decode("utf-8"))
    if not isinstance(run, dict):
        raise ValueError("expected an object")
    if not isinstance(run.get("id"), str) or not NAME.fullmatch(run["id"]):
        raise ValueError("missing or invalid id")
    # Reject values that cannot be written back as UTF-8 JSON when a run finishes.
    json.dumps(run, ensure_ascii=False, allow_nan=False).encode("utf-8")
    for field in ("team", "plan", "status"):
        if not isinstance(run.get(field), str) or not run[field]:
            raise ValueError(f"missing or invalid {field}")
    if type(run.get("seed")) is not int:
        raise ValueError("missing or invalid seed")
    if run["status"] not in {"running", "succeeded", "failed", "rejected", "infrastructure_interrupted"}:
        raise ValueError("invalid status")
    if run["status"] == "running":
        for field in ("heartbeat", "result"):
            if not isinstance(run.get(field), str) or not run[field]:
                raise ValueError(f"missing or invalid {field}")
        if run.get("log") and not isinstance(run["log"], str):
            raise ValueError("invalid log")
        if "eta" not in run:
            raise ValueError("missing eta")
        run["registered_at"] = timestamp(run["registered_at"])
    if run["status"] == "succeeded":
        if (type(run.get("score")) not in (int, float) or not isinstance(run.get("split"), str)
                or type(run.get("audit_passed")) is not bool):
            raise ValueError("invalid evaluation fields")
        if type(run.get("environment_variant", False)) is not bool:
            raise ValueError("invalid environment_variant")
    if run["status"] == "succeeded" or "finished_at" in run:
        run["finished_at"] = timestamp(run["finished_at"])
    for field in ("card_hours", "gpu_card_seconds"):
        if field in run and type(run[field]) not in (int, float):
            raise ValueError(f"invalid {field}")
    if run.get("gpu_started_at") is not None:
        run["gpu_started_at"] = timestamp(run["gpu_started_at"])
    if (run.get("gpu_slots") and (not isinstance(run["gpu_slots"], list)
                                 or not all(isinstance(slot, str) for slot in run["gpu_slots"]))
            or run.get("gpu") and not isinstance(run["gpu"], str)):
        raise ValueError("invalid GPU slots")
    if ar_quota.slot_keys(run) and run["status"] == "running":
        timestamp(run["lease_until"])
    return run


class Campaign:
    def __init__(self, directory, now):
        self.path = directory
        self.now = now
        self.config = load(directory / "config.json")
        self.jobs = Path(option("AR_AMB_JOBS", str(Path(option("AMB_ROOT", str(Path.home() / "amb"))) / "jobs")))
        self.amb = option("AR_AMB", "amb")
        self.linear = option("AR_LINEAR", "linear-app")
        self.state = self.read_state()
        self.transfer = Path(option("AR_TRANSFER_ROOT", str(Path.home() / "autoresearch/transfer")))

    def read_state(self):
        path = self.path / "state.json"
        for recovering in (False, True):
            if recovering:
                atomic(path, command("git", "show", "HEAD:state.json", cwd=self.path) + "\n")
            try:
                state = load(path)
                if (state["status"] not in {"running", "paused", "stopped", "throttled"}
                        or not all(isinstance(state[key], dict)
                                   for key in ("teams", "jobs", "usage", "alerts"))
                        or not isinstance(state["cycle"], int)):
                    raise ValueError("invalid campaign state")
            except (ValueError, KeyError, TypeError, FileNotFoundError):
                if recovering:
                    raise
                continue
            if recovering:
                self.event("state_recovered")
                print(f"{self.path.name}: recovered state from git", file=sys.stderr)
            return state

    def write(self):
        save(self.path / "state.json", self.state)

    def checkpoint(self):
        self.write()
        paths = [name for name in TRACKED if (self.path / name).exists()]
        command("git", "add", "--", *paths, cwd=self.path)
        if command("git", "diff", "--cached", "--name-only", cwd=self.path):
            command("git", "-c", "user.name=AutoResearch", "-c",
                    "user.email=ar@localhost", "commit", "-qm", "Checkpoint campaign",
                    cwd=self.path)

    def event(self, event, **fields):
        append(self.path / "events.jsonl", {"time": self.now, "event": event, **fields})

    def role_models(self):
        return {**MODELS, **self.state.get("role_models", {})}

    def set_models(self, overrides):
        if not overrides:
            raise ValueError("supply at least one role model")
        previous = self.state.get("role_models", {})
        models = {**previous, **overrides}
        captains = {name: overrides["team"] for name, team in self.state["teams"].items()
                    if "team" in overrides and team.get("active", True)
                    and team.get("captain_model") != overrides["team"]}
        if previous == models and not captains:
            return
        self.state["role_models"] = models
        for name, model in captains.items():
            self.state["teams"][name]["captain_model"] = model
        self.write()
        fields = {"role_models": self.role_models(), "captains_changed": captains}
        append(self.path / "ops-ledger.jsonl",
               {"time": self.now, "event": "role_models_changed", **fields})
        self.event("role_models_changed", **fields)

    def timing(self, key):
        return self.config.get("timing", {}).get(key, TIMING[key])

    def notify(self, kind, text, *, comment=False):
        append(self.path / "outbox.jsonl", {"time": self.now, "kind": kind, "text": text,
                                           "comment": comment})

    def alert(self, key, message):
        if key not in self.state["alerts"]:
            self.state["alerts"][key] = message
            self.event("alert", key=key, message=message)
            self.notify("alert", message)
            print(f"{self.path.name}: {message}", file=sys.stderr)

    def clear(self, key):
        self.state["alerts"].pop(key, None)

    def flush_outbox(self):
        """章程、评审决定、裁决、告警在生效的同时发给用户（经 amb align：飞书 + 课题卡评论），不等确认。"""
        entries = rows(self.path / "outbox.jsonl")
        sent = self.state.get("outbox_sent", 0)
        folder = self.path / "outbox"
        for index in range(sent, len(entries)):
            entry = entries[index]
            folder.mkdir(exist_ok=True)
            body = folder / f"{index:06d}.md"
            atomic(body, entry["text"].rstrip() + "\n")
            title = f"自动研究 {self.path.name} · {OUTBOX_LABEL.get(entry['kind'], entry['kind'])}"
            try:
                card_arg = ["--card", self.card] if entry.get("comment", entry["kind"] == "charter") else []
                command(self.amb, "align", str(body), "--title", title, "--source",
                        f"ar-{self.path.name}", "--key", str(index), *card_arg)
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                if "outbox" not in self.state["alerts"]:
                    self.state["alerts"]["outbox"] = str(error)
                    self.event("alert", key="outbox", message=str(error))
                return
            self.state["outbox_sent"] = index + 1
            self.event("notification_queued", index=index, kind=entry["kind"])
        self.clear("outbox")

    @property
    def card(self):
        return self.config["card"]

    @property
    def day(self):
        return ar_quota.day_at(self.now)

    def owner_message(self, ident):
        record = Path(self.config.get("owner_log", str(Path.home() / ".local/state/ambition/linear-owner-messages.jsonl")))
        if not record.is_absolute() or any(path.is_symlink() or path.stat().st_uid != 0
                                           or os.access(path, os.W_OK)
                                           for path in (record, *record.parents)) or not record.is_file():
            raise ValueError("owner log must be controlled by the inbound message service")
        matches = [row for row in rows(record) if isinstance(row, dict) and row.get("id") == ident]
        if len(matches) == 1:
            row = matches[0]
            text = row.get("text")
            received = owner_timestamp(row["received_at"])
            if isinstance(text, str) and text.strip() and received <= self.now:
                if "created_at" in row and owner_timestamp(row["created_at"]) > received:
                    raise ValueError("invalid owner message creation time")
                return row
        raise ValueError("owner message not found")

    def stop_instruction(self, text):
        # Match the entire message so that context, qualifiers and other targets cannot grant authority.
        name = re.escape(self.path.name)
        card = re.escape(self.card)
        verb = r"(?:请[ \t]*)?(?:停止|终止|停掉)"
        english = r"(?i:(?:please[ \t]+)?stop)"
        if CARD.fullmatch(self.path.name):
            target = card
        else:
            target = rf"(?:{name}|{card})"
        return re.fullmatch(
            rf"(?:{verb}[ \t]+{target}|{verb}(?:[ \t]+)?课题[ \t]+{name}"
            rf"|{verb}[ \t]+{name}[ \t]*课题|{english}[ \t]+{target}"
            rf"|{english}[ \t]+(?i:campaign)[ \t]+{name})[。.!！]?",
            text,
        ) is not None

    def acceptance_approval(self, row, record):
        requested = record.get("requested_at")
        if not isinstance(requested, str) or row["text"].strip() != f"同意验收 {self.card} {requested}":
            return False
        created = row.get("created_at")
        if not isinstance(created, str) or not isinstance(row["received_at"], str):
            return False
        return timestamp(requested) < timestamp(created) <= timestamp(row["received_at"]) <= self.now

    def acceptance_on_card(self, issue, record):
        requested = record.get("requested_at")
        if not isinstance(requested, str):
            return False
        marker = re.escape(f"## 验收申请\n申请时间：{requested}")
        return re.search(rf"^{marker}$", issue.get("description") or "", re.MULTILINE) is not None

    def open_acceptance(self, issue, record):
        return (issue["state"]["name"] in ACCEPTANCE_STATES
                and record.get("status") in {"open", "closing"}
                and self.acceptance_on_card(issue, record))

    def stop(self, ident):
        row = self.owner_message(ident)
        text = row["text"].strip()
        if not self.stop_instruction(text):
            if not text.startswith(f"同意验收 {self.card} "):
                raise ValueError(f"owner message must explicitly say 停止课题 {self.path.name} or 停止 {self.card}, "
                                 "or approve this card's current acceptance request")
            issue = self.card_snapshot()
            record = self.acceptance_record(issue)
            if not self.open_acceptance(issue, record) or not self.acceptance_approval(row, record):
                raise ValueError("owner message does not approve this card's current acceptance request")
        self.state.update(status="stopped", stopped_by=ident, stop_text=row["text"],
                          stop_time=row["received_at"], progress_dirty=True, progress_urgent=True)
        path = self.path / "charter.md"
        marker = f"## 停止记录 {ident}"
        if marker not in path.read_text():
            with path.open("a", encoding="utf-8") as stream:
                stream.write(f"\n{marker}\n\n{row['text']}\n")
        self.event("owner_stop", message_id=ident)

    def linear_read(self, query, **variables):
        return json.loads(command(self.linear, "gql", query, json.dumps(variables)))["issue"]

    def card_snapshot(self):
        return self.linear_read(
            "query($id:String!){issue(id:$id){id description state{id name} labels{nodes{name}}}}",
            id=self.card)

    def last_transition(self, issue):
        cursor = None
        while True:
            page = self.linear_read(
                "query($id:String!,$c:String){issue(id:$id){history(first:50,after:$c)"
                "{nodes{actor{id} botActor{id} toStateId} pageInfo{hasNextPage endCursor}}}}",
                id=issue["id"], c=cursor)["history"]
            for row in page["nodes"]:
                if row.get("toStateId"):
                    if row["toStateId"] != issue["state"]["id"]:
                        raise ValueError("Linear state changed during audit")
                    return row
            if not page["pageInfo"]["hasNextPage"]:
                raise ValueError("no audit trail for current Linear state")
            cursor = page["pageInfo"]["endCursor"]

    def acceptance_record(self, issue):
        directory = Path(option("AR_LINEAR_RECORDS", str(Path.home() / ".config/linear/acceptance")))
        key = hashlib.sha256(issue["id"].encode()).hexdigest()
        path = directory / (key + ".json")
        return load(path) if path.exists() else {}

    def check_card(self):
        issue = self.card_snapshot()
        status = issue["state"]["name"]
        if "长期" not in {label["name"] for label in issue["labels"]["nodes"]}:
            raise ValueError("campaign card is missing the 长期 label")
        description = issue.get("description") or ""
        record = self.acceptance_record(issue)
        if self.open_acceptance(issue, record):
            self.state["acceptance"] = {"time": timestamp(record["requested_at"])}
            return True
        if status == "In Progress":
            if self.state.pop("acceptance", None):
                self.event("acceptance_resolved")
            self.clear("card_state")
            return True
        latest = self.last_transition(issue)
        owner = self.config.get("linear_owner_id")
        if not owner:
            raise ValueError("linear_owner_id required before repairing card state")
        manual = not latest.get("botActor") and (latest.get("actor") or {}).get("id") == owner
        authorized = False
        if (not manual and status == "Done" and record.get("status") in {"closing", "closed"}
                and record.get("owner_msg") and self.acceptance_on_card(issue, record)):
            try:
                message = self.owner_message(record["owner_msg"])
                approved = self.acceptance_approval(message, record)
                quoted = "\n".join("> " + line if line else ">" for line in message["text"].split("\n"))
                authorized = approved and f"（{record['owner_msg']}）" in description and quoted in description
            except (ValueError, OSError, KeyError, TypeError) as error:
                raise ValueError("cannot verify recorded owner closure") from error
        if manual or authorized:
            if status in {"Done", "Canceled"} and self.state["status"] != "stopped":
                self.state["status"] = "stopped"
                self.event("owner_linear_stop", status=status)
            return False
        self.alert("card_state", f"{self.card} unexpected state {status}; restoring In Progress")
        command(self.linear, "state", self.card, "In Progress")
        command(self.linear, "comment", self.card, f"巡检发现课题卡处于 {status}，已恢复为 In Progress。")
        self.state.pop("acceptance", None)
        return True

    def reconcile_jobs(self, allow_dispatch):
        events = self.path / "amb_events.jsonl"
        if events.exists():
            with events.open("rb") as stream:
                stream.seek(self.state.get("amb_events_offset", 0))
                while raw := stream.readline():
                    if not raw.endswith(b"\n"):
                        break
                    event = json.loads(raw)
                    jid = event["id"]
                    if jid in self.state["usage"]:
                        if event.get("state") == "alert":
                            self.alert(f"amb:{event['event_id']}", f"{jid}：{event['summary']}")
                        else:
                            self.event("amb_terminal_event", job=jid, status=event["state"])
                    self.state["amb_events_offset"] = stream.tell()
            self.write()
        for jid, job in list(self.state["jobs"].items()):
            status_file = self.jobs / jid / "state.json"
            if not status_file.exists():
                if (self.jobs / "_draft" / jid / "job.json").exists():
                    if allow_dispatch and self.can_submit(job) and self.has_dispatch_slot(job):
                        try:
                            self.try_submit(jid, job)
                        except (OSError, ValueError, subprocess.TimeoutExpired):
                            if job["role"] != "ruling":
                                raise
                            # try_submit has raised an alert. Let scheduling check the fallback deadline.
                elif self.now - job["time"] > 600:
                    self.alert(f"missing:{jid}", f"amb job {jid} has no draft or state")
                continue
            snapshot = load(status_file)
            if snapshot["state"] not in TERMINAL:
                continue
            result = self.jobs / jid / "result.md"
            self.event("job_finished", job=jid, status=snapshot["state"],
                       result=str(result) if result.exists() else None,
                       open_items=snapshot.get("open_items"))
            actual = self.state.get("model_usage", {}).get(jid, {}).get("entries", [])
            self.state["usage"][jid]["minutes"] = round(sum(item["seconds"] for item in actual) / 60, 2)
            if job["role"] in {"review", "ruling"} and snapshot["state"] == "done":
                decision = Path(job["output"])
                if decision.exists():
                    try:
                        self.apply(load(decision), expected=job)
                    except (ValueError, KeyError, TypeError) as error:
                        self.alert(f"decision:{jid}", str(error))
            del self.state["jobs"][jid]
            self.write()

    def experiments(self, *, quarantine=False):
        ledger = self.path / "ledger.jsonl"
        runs, rejected = {}, {}
        if ledger.exists():
            with ledger.open("rb") as stream:
                for line, raw in enumerate(stream, 1):
                    if not raw.strip():
                        continue
                    try:
                        run = ledger_record(raw)
                    except (ValueError, KeyError, TypeError, OverflowError, RecursionError) as error:
                        if quarantine:
                            digest = hashlib.sha256(raw).hexdigest()
                            rejected[line, digest] = dict(time=self.now, line=line, sha256=digest,
                                                          error=str(error),
                                                          raw=raw.decode("utf-8", "backslashreplace"))
                        continue
                    runs[run["id"]] = run
        if quarantine:
            self.quarantine_ledger(rejected)
        return runs

    def quarantine_ledger(self, rejected):
        path = self.path / "ledger.quarantine.jsonl"
        key = "ledger_invalid"
        if rejected:
            previous = rows(path)
            seen = {(row["line"], row["sha256"]) for row in previous}
            added = [row for location, row in rejected.items() if location not in seen]
            if added:
                atomic(path, "".join(json.dumps(row, ensure_ascii=True) + "\n"
                                     for row in [*previous, *added]))
                for row in added:
                    print(f"{self.path.name}: ledger.jsonl line {row['line']} quarantined: {row['error']}",
                          file=sys.stderr)
            message = f"账本告警：已跳过 {len(rejected)} 条格式错误的记录，详见 ledger.quarantine.jsonl。"
            if self.state["alerts"].get(key) != message:
                self.state["alerts"][key] = message
                self.state["progress_urgent"] = True
        elif key in self.state["alerts"]:
            self.clear(key)
            self.state["progress_urgent"] = True

    def record_run(self, run):
        append(self.path / "ledger.jsonl", run)
        self.state["progress_dirty"] = True

    def collect_runs(self, runs=None):
        if runs is None:
            runs = self.experiments()
        transfer_waiting = False
        for run in runs.values():
            if run["status"] != "running":
                continue
            heartbeat = Path(run["heartbeat"])
            try:
                pulse = load(heartbeat) if heartbeat.exists() else {}
            except (OSError, ValueError):
                pulse = {}
            expired = self.now - timestamp(pulse.get("time", run["registered_at"])) > 180
            result = Path(run["result"])
            transported = run.get("transport") == "h200"
            if transported and pulse.get("alive") is not False:
                try:
                    sync = load(self.transfer / "status.json")
                    transfer_fresh = sync["ok"] and self.now - sync["time"] <= 120
                except (OSError, ValueError, KeyError):
                    transfer_fresh = False
                if not transfer_fresh:
                    transfer_waiting = True
                    self.alert("transfer", "H200 transfer unavailable; awaiting experiment records")
                    continue
                if expired and sync["time"] < timestamp(pulse.get("time", run["registered_at"])) + 180:
                    continue
                if expired and ar_quota.slot_keys(run) and sync["time"] < timestamp(run["lease_until"]):
                    continue
            if pulse.get("alive") is True and not expired:
                continue
            if not result.exists() and not expired and pulse.get("alive") is not False:
                continue
            update = dict(run, finished_at=timestamp(pulse.get("finished_at") or pulse.get("time") or self.now))
            if ar_quota.slot_keys(run) and (transported or "started_at" in pulse):
                update.update(gpu_card_seconds=ar_quota.utilization(run, pulse, self.now),
                              gpu_started_at=pulse.get("started_at"))
            if (pulse.get("infrastructure_interrupted") is True
                    or (transported and expired and pulse.get("alive") is not False)):
                update["status"] = "infrastructure_interrupted"
            elif (result.exists() and pulse.get("resource_killed") is not True
                  and (not transported or pulse.get("exit_code") == 0)):
                try:
                    pins = self.config["evaluation"]
                    if set(pins) != {"script", "scenarios"} or any(
                        sha(entry["path"]) != entry["sha256"] for entry in pins.values()
                    ):
                        raise ValueError("evaluation hash mismatch")
                    output = load(result)
                    score = output["score"]
                    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                        raise ValueError("invalid evaluation score")
                    card_hours = (update["gpu_card_seconds"] / HOUR if "gpu_card_seconds" in update
                                  else output.get("card_hours", 0))
                    if not isinstance(card_hours, (int, float)) or not math.isfinite(card_hours) or card_hours < 0:
                        raise ValueError("invalid GPU usage")
                    update.update(status="succeeded", score=score, card_hours=card_hours,
                                  split=output.get("split", "development"),
                                  audit_passed=output.get("audit_passed") is True)
                except (KeyError, OSError, ValueError) as error:
                    update.update(status="rejected", reason=str(error))
                    self.alert(f"run:{run['id']}", f"{run['id']}: {error}")
            else:
                update.update(status="failed", reason=pulse.get("reason", "experiment heartbeat expired"))
                log = Path(run["log"]) if run.get("log") else None
                if not transported and log and log.is_file():
                    with log.open("rb") as stream:
                        stream.seek(max(0, log.stat().st_size - 4096))
                        update["log_tail"] = stream.read(4096).decode("utf-8", errors="replace")
            self.record_run(update)
            runs[run["id"]] = update
            self.event("experiment_finished", id=run["id"], status=update["status"])
        if not transfer_waiting:
            self.clear("transfer")

    def leaderboard(self, runs):
        groups = {}
        for run in runs.values():
            if run["status"] != "succeeded":
                continue
            group = (run["team"], run["plan"], run["split"], run.get("environment_variant", False))
            scores = groups.setdefault(group, {})
            scores.setdefault(str(run["seed"]), run)

        lines = ["# 排行榜"]
        survey = self.path / "survey.md"
        if survey.exists():
            baseline = re.search(r"^## 排行榜第 0 行[ \t]*$\n(.*?)(?=^#{1,2} |\Z)",
                                 survey.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
            if baseline and baseline[1].strip():
                lines += ["", "## 第 0 行：冻结基准", "", baseline[1].strip()]

        ranked = []
        candidates = []
        environment = []
        for group, seeds in groups.items():
            score = sum(run["score"] for run in seeds.values()) / len(seeds)
            entry = (score, group[:3], len(seeds))
            if group[3]:
                environment.append(entry)
            elif len(seeds) >= 3 and all(run["audit_passed"] for run in seeds.values()):
                ranked.append(entry)
            else:
                candidates.append(entry)

        direction = self.config.get("maximize", True)
        for heading, entries in (
            ("终极指标（留出集）", [row for row in ranked if row[1][2] == "holdout"]),
            ("代理指标（开发集及其他筛选集）", [row for row in ranked if row[1][2] != "holdout"]),
            ("候选", candidates),
            ("环境变体", environment),
        ):
            lines += ["", f"## {heading}", "", "| 团队 | 计划 | 数据集 | 均值 | 种子数 |",
                      "|---|---|---|---:|---:|"]
            for score, group, count in sorted(entries, reverse=direction):
                lines.append(f"| {' | '.join(group)} | {score:.6g} | {count} |")
        path = self.path / "leaderboard.md"
        report = "\n".join(lines) + "\n"
        if not path.exists() or path.read_text() != report:
            atomic(path, report)
        eligible = [entry[0] for entry in ranked if entry[1][2] == "development"]
        if eligible:
            best = (max if direction else min)(eligible)
            old = self.state.get("best")
            if old is None or (best > old if direction else best < old):
                self.state.update(best=best, early_review=True, progress_dirty=True)

    def resources(self):
        if not self.config.get("resources"):
            return False
        alarm = False
        for resource in self.config["resources"]:
            machine = resource["machine"]
            key = f"resource:{machine}"
            try:
                snapshot = load(Path(resource["snapshot"]))
                if snapshot.get("machine", machine) != machine:
                    raise ValueError("resource snapshot belongs to a different machine")
                # keeper samples every 2 minutes and the transfer adds up to a minute; 180 s flapped on every other tick.
                if self.now - timestamp(snapshot["time"]) > resource.get("stale_seconds", 420):
                    raise ValueError("stale resource snapshot")
                if snapshot["alarm"] or max(snapshot["shm_fraction"], snapshot["memory_fraction"]) >= .85:
                    raise ValueError("resource alarm or shared memory / RAM at 85%")
            except (OSError, ValueError, KeyError) as error:
                self.alert(key, f"{machine}: {error}")
                alarm = True
            else:
                self.clear(key)
        return alarm

    def gpu_usage(self, runs, since=0, until=None):
        pulses = {}
        for ident, run in runs.items():
            if run["status"] == "running" and ar_quota.slot_keys(run):
                try:
                    pulse = load(Path(run["heartbeat"]))
                except (OSError, ValueError):
                    continue
                if isinstance(pulse, dict) and "time" in pulse:
                    pulses[ident] = pulse
        return ar_quota.gpu_usage(runs, pulses, self.now, since, until)

    def daily_gpu_report(self, runs):
        local = datetime.fromtimestamp(self.now, ar_quota.CHINA)
        if local.hour < 9 or any(
            entry["kind"] == "daily_review" and ar_quota.day_at(entry["time"]) == self.day
            for entry in rows(self.path / "outbox.jsonl")
        ):
            return ""
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = midnight - timedelta(days=1)
        usage = self.gpu_usage(runs, since=yesterday.timestamp(), until=midnight.timestamp())
        lines = [f"GPU 用量（北京时间 {yesterday.date()}）",
                 f"已识别 {usage['card_count']} 张卡，合计 {usage['card_hours']:.4f} 卡时。"]
        lines.extend(f"- {card['machine']} / {card['card']}，{card['card_hours']:.4f} 卡时"
                     for card in usage["cards"])
        if usage["unattributed_card_seconds"]:
            lines.append(f"- 历史记录缺少卡号，{usage['unattributed_card_seconds'] / HOUR:.4f} 卡时")
        return "\n".join(lines)

    def model_usage(self):
        return ar_quota.summarize_models(self.state.get("model_usage", {}), self.state["usage"],
                                         self.day, TERMINAL)

    def refresh_model_usage(self):
        self.state["model_usage"] = ar_quota.collect_models(
            self.jobs, self.path.name, self.state.get("model_usage", {}), self.now)

    def budget(self, alarm):
        usage = self.model_usage()
        limits = self.config["budget"]
        today = usage["today_sessions"] + usage["today_pending"]
        total = usage["sessions"] + usage["pending"]
        daily_limit, total_limit = limits["daily"], limits["total"]
        if today >= daily_limit * .8:
            self.alert("daily_budget", f"daily sessions {today}/{daily_limit}")
        else:
            self.clear("daily_budget")
        if total >= total_limit:
            self.alert("total_budget", f"total sessions {total}/{total_limit}; owner decision required")
        else:
            self.clear("total_budget")
        fable = [entry for name, entry in usage["models"].items() if "fable" in name.lower()]
        minutes = sum(entry["seconds"] for entry in usage["models"].values())
        if (self.state["status"] in {"running", "throttled"} and not self.config.get("drill")
                and (sum(entry["today_sessions"] for entry in fable) > 12
                     or (minutes and sum(entry["seconds"] for entry in fable) > minutes * .2))):
            self.alert("fable_share", "Fable usage above target; review at the next major review")
        else:
            self.clear("fable_share")
        if self.state["status"] in {"running", "throttled"}:
            target = "throttled" if alarm or today >= daily_limit or total >= total_limit else "running"
            if target != self.state["status"]:
                self.state.update(status=target, progress_dirty=True, progress_urgent=True)
                self.event("budget_status", status=target)

    def ask_budget(self):
        if (self.state["status"] != "throttled" or "total_budget" not in self.state["alerts"]
                or self.config.get("drill") or (self.path / "STOP").exists()):
            return
        limit = self.config["budget"]["total"]
        if self.model_usage()["sessions"] < limit:
            return
        self.question(dict(id=f"total-model-budget-{limit}", question=f"课题模型总额度 {limit} 次已用完，是否追加？",
                           options="追加会话额度，或继续限速。", recommendation="先核对用量，再决定是否追加。",
                           waiting="常规研究派工与新实验。已启动的实验继续收尾。"))

    def dispatch_hold(self):
        if (self.path / "STOP").exists():
            return "STOP"
        status = self.state["status"]
        if status in {"paused", "stopped"}:
            return status
        return None

    def can_submit(self, job):
        status = self.state["status"]
        if self.dispatch_hold():
            return False
        if job["role"] == "review" and status == "throttled":
            return False
        if job["role"] == "team":
            if status == "throttled" and job["mode"] != "consolidate":
                return False
            if self.state.get("acceptance") and job["mode"] == "explore":
                return False
        return True

    @property
    def campaign_limit(self):
        return self.config["budget"].get("concurrency", CAMPAIGN_CONCURRENCY)

    def active_amb_jobs(self):
        """Count submitted nonterminal jobs, including queue reservations and worker jobs."""
        jobs, active = {}, {}
        for path in self.jobs.glob("*/job.json"):
            jid = path.parent.name
            if jid.startswith("_"):
                continue
            try:
                job = load(path)
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            if not isinstance(job, dict) or job.get("campaign") != self.path.name:
                continue
            jobs[jid] = job
            try:
                status = load(path.with_name("state.json"))["state"]
            except FileNotFoundError:
                # amb moves the job out of _draft before it writes the initial state.
                status = "queued"
            if status not in TERMINAL:
                active[jid] = None
        usage = self.state["usage"]
        for ident in active:
            seen = set()
            jid = ident
            while jid and jid not in seen:
                seen.add(jid)
                job = jobs.get(jid, {})
                team = usage.get(jid, {}).get("team") or job.get("team")
                if isinstance(team, str) and team in self.state["teams"]:
                    active[ident] = team
                    break
                jid = job.get("parent") or job.get("lead_parent")
            if active[ident] is None:
                # Older captain briefs used titles such as "percept P-001 …".
                title = str(jobs[ident].get("title") or "").split(maxsplit=2)
                if (len(title) >= 2 and title[0] in self.state["teams"]
                        and re.fullmatch(r"P-[0-9]+", title[1])):
                    active[ident] = title[0]
        return active

    def has_dispatch_slot(self, job):
        if job["role"] == "ruling":
            return True
        active = self.active_amb_jobs()
        team = job["team"]
        return len(active) < self.campaign_limit and (
            team is None or sum(owner == team for owner in active.values()) < TEAM_CONCURRENCY)

    def dispatch(self, role, model, team=None, messages=None, mode=None, *, ruling_kind="ruling"):
        job = {"role": role, "model": model, "team": team, "time": self.now,
               "day": self.day, "mode": mode, "cycle": self.state["cycle"],
               "messages": messages or []}
        if role == "ruling":
            job["ruling_kind"] = ruling_kind
        if not self.can_submit(job) or not self.has_dispatch_slot(job):
            return
        # Current amb releases use the test flag to suppress automatic card creation.
        # A drill job must have neither its own card nor an attached card.
        card_args = [] if self.config.get("drill") else ["--linear", self.card]
        overrides = self.config.get("dispatch_overrides", {}).get(model, {})
        execution_args = [arg for key in ("host", "model", "effort") if key in overrides
                          for arg in ("--" + key, overrides[key])]
        jid = command(self.amb, "new", "--kind", "doc", "--tier", "L1", "--exec", model,
                      "--campaign", self.path.name, *card_args, *execution_args)
        if not NAME.fullmatch(jid):
            raise ValueError("unexpected amb job ID")
        if role in {"review", "ruling"}:
            job["output"] = str(self.path / "reviews" / f"{jid}.json")
        self.state["jobs"][jid] = job
        self.state["usage"][jid] = dict(job, minutes=None)
        self.checkpoint()
        self.try_submit(jid, job)

    def try_submit(self, jid, job):
        try:
            self.submit(jid, job)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            self.alert(f"dispatch:{jid}", str(error))
            raise
        else:
            self.clear(f"dispatch:{jid}")

    def submit(self, jid, job):
        draft = self.jobs / "_draft" / jid
        role, team = job["role"], job["team"]
        title = f"推进{team}方向" if team else ("处理课题阻塞" if role == "ruling" else "评审研究课题")
        rules = self.config.get("resource_rules", "")
        brief = [f"# {title}", f"课题目录 {self.path}。阅读章程、状态、账本、最近评审和本队记忆。",
                 "你的会话结束不代表课题结束。目标尚未达成时，交付前在团队记忆里留下至少三条待试事项。"]
        if team:
            brief.append(f"本队 {team}，工作模式 {job['mode']}。仅 explore 可探索。"
                         "consolidate 只消化结果和更新记忆，verify 做复现、消融、讲解。"
                         "无 GPU 配额或有资源告警时只做 CPU 工作。训练只通过统一启动器，先登记到账本。")
            if self.state["teams"][team].get("blocking"):
                brief.append("查看本队登记的阻塞，继续待试清单中不受该阻塞影响的事项。"
                             "受阻事项按当前批示执行，等待所需条件或领队裁决。")
            brief.append("每队同时在跑的 amb 单不超过 2 张（含队长自己）。派队员前先用 amb status 数"
                         "本队已提交未结束的单，排队也占名额。队员草稿 job.json 须填与本单相同的 team 字段，"
                         "并保留同一 campaign。")
        if not self.config.get("drill"):
            if team:
                brief.append("按 ~/autoresearch/SKILL.md B4 和 templates/brief_lead.md 执行。"
                             "每个新方向（含已有团队转向）先有 survey.md 标尺和批示，再派外部复现首单。"
                             "北大校园 Go2 导航以项目 ops/ACCEPTANCE.md 的冻结整程成绩决定迭代续派。")
                worker_template = Path(__file__).resolve().parent / "templates" / "brief_worker.md"
                reproduction_section = worker_template.read_text().split(
                    "## 新方向首环：外部复现与接口差异", 1
                )[1].split("\n## ", 1)[0]
                brief.append("## 新方向首环：外部复现与接口差异" + reproduction_section.rstrip())
            else:
                brief.append("按 ~/autoresearch/SKILL.md B6 和 templates/brief_commander.md 执行。")
                if role == "review":
                    brief.append("先读 ~/autoresearch/lessons.md 和已有的同领域 knowledge 文件。"
                                 "按 templates/review.md 分开核对终极指标、代理指标及实验类型。"
                                 "北大校园 Go2 导航按项目 ops/ACCEPTANCE.md 判断是否续派；"
                                 "同口径整程持平或下降时停扩训并交人工复核。")
        if job.get("output"):
            if role == "ruling" and ruling_kind(job) == "temporary_ruling":
                brief.append("本单负责临时裁决，结论供领队在下次大评审复核。")
            brief.append(f"把结构化决定写入 {job['output']}。"
                         '小裁决用 {"rulings":{"队名":"批示"},"owner_response":"给用户的答复"}，'
                         '如果收到 ar say 消息，owner_response 必须逐条回应。大评审用 '
                         '{"summary":"当前进度与目标差距","next":"下一步","teams":{"队名":{"decision":"maintain"}}}。')
        if job["messages"]:
            brief.append(json.dumps(job["messages"], ensure_ascii=False))
        if self.config.get("drill"):
            brief.append("这是假课题空跑。工作范围仅本课题目录。"
                         "禁止操作 Linear、真实课题、训练机、GPU、凭据和下派 amb 单。"
                         "队长只整理本队假实验结果和待试清单，实验由演练驱动器启动。"
                         "大评审和裁决必须写上面指定的 JSON 文件，再交付。")
        if rules:
            brief.append(rules)
        brief = "\n\n".join(brief) + "\n"
        data = load(draft / "job.json")
        data.update(title=title, user_words=title, checklist=["交付与章程和本轮任务一致"],
                    deadline=datetime.fromtimestamp(self.now + 2 * HOUR, timezone.utc).isoformat())
        if team:
            data["team"] = team
        if self.config.get("drill"):
            data["test"] = True
            data.setdefault("limits", {}).update(step_minutes=6, max_attempts=1)
        save(draft / "job.json", data)
        atomic(draft / "brief.md", brief)
        if not self.can_submit(job) or not self.has_dispatch_slot(job):
            return
        command(self.amb, "submit", jid)
        self.event("job_dispatched", job=jid, role=role, model=job["model"], team=team)

    def schedule(self, runs):
        # A blocking ruling is still eligible when the regular review budget is exhausted.
        if not self.can_submit({"role": "ruling"}):
            return
        models = self.role_models()
        teams = {name: team for name, team in self.state["teams"].items() if team.get("active", True)}
        last = self.state["last_review"]
        elapsed = self.now - last
        counts = {name: sum(run["team"] == name and run["status"] == "succeeded"
                            and run["finished_at"] >= last for run in runs.values())
                  for name in teams}
        if elapsed >= self.timing("review_late"):
            self.alert("review_late", "no completed major review before the alert deadline")
        jobs = self.state["jobs"]
        owner_messages = [row for row in rows(self.path / "inbox.jsonl")
                          if row["seq"] > self.state.get("inbox_ack", 0)]
        block_messages = []
        for name, team in teams.items():
            block = team.get("blocking")
            if block:
                block_time = timestamp(block["time"])
                ruling_at = team.get("ruling_at")
                if asks_lead(block):
                    # Legacy help records first became eligible when the new rule saw them.
                    due_at = block_time if "ask_lead" in block else block.get("ruling_queued_at", self.now)
                else:
                    due_at = max(block_time, min(timestamp(block["recheck_at"]),
                                                 block_time + self.timing("block_escalation")))
                if due_at <= self.now and (ruling_at is None or timestamp(ruling_at) < block_time):
                    if "ruling_queued_at" not in block:
                        block["ruling_queued_at"] = self.now
                        self.event("block_ruling_queued", team=name, block_time=block_time)
                    block_messages.append({"team": name, "time": due_at, "text": block["what"],
                                           "block_time": block_time})
            if team.get("backlog", 3) < 3:
                self.state["early_review"] = True
        if owner_messages or block_messages:
            age = self.now - min(timestamp(row["time"]) for row in owner_messages + block_messages)
            active_rulings = {jid: job for jid, job in jobs.items() if job["role"] == "ruling"}
            launched = {jid for jid, job in active_rulings.items()
                        if ruling_kind(job) == "ruling" and self.running(jid)}
            if age >= self.timing("ruling_late"):
                self.alert("ruling_late", "blocking decision still pending after 30 minutes")
            primary, temporary = [], []
            batches = [([message], {jid: job for jid, job in active_rulings.items()
                                    if covers_block(job, message)}) for message in block_messages]
            if owner_messages:
                # Keep owner messages together because inbox_ack is a cumulative sequence number.
                batches.append((owner_messages, {jid: job for jid, job in active_rulings.items()
                                                 if any("seq" in row for row in job["messages"])}))
            for batch, assigned in batches:
                if (self.now - timestamp(batch[0]["time"]) >= self.timing("ruling_fallback")
                        and not launched.intersection(assigned)
                        and not any(ruling_kind(job) == "temporary_ruling" for job in assigned.values())):
                    temporary.extend(batch)
                elif not assigned:
                    primary.extend(batch)
            if temporary:
                self.dispatch("ruling", models["temporary_ruling"], messages=temporary,
                              ruling_kind="temporary_ruling")
            if primary:
                self.dispatch("ruling", models["ruling"], messages=primary)
        else:
            self.clear("ruling_late")
        active = self.active_amb_jobs()
        limit = self.campaign_limit
        due = elapsed >= self.timing("review_interval") or (elapsed >= self.timing("review_earliest") and
                                     (self.state.get("early_review") or all(n >= 3 for n in counts.values())))
        if due and len(active) < limit and not any(job["role"] == "review" for job in jobs.values()):
            self.dispatch("review", models["review"])
            active = self.active_amb_jobs()
        busy = {run["team"] for run in runs.values() if run["status"] == "running"}
        busy.update(job["team"] for job in jobs.values())
        team_counts = Counter(active.values())
        for name, team in teams.items():
            if len(active) >= limit:
                break
            block = team.get("blocking")
            if (name in busy or team_counts[name] >= TEAM_CONCURRENCY
                    or (block and not asks_lead(block) and "ruling_queued_at" not in block
                        and timestamp(block["recheck_at"]) > self.now)):
                continue
            self.budget(self.state["resource_alarm"])
            today = [job for job in self.state["usage"].values() if job["day"] == self.day]
            mode = "explore"
            if self.state["status"] == "throttled":
                mode = "consolidate"
                if any(job["team"] == name and job["mode"] == mode for job in today):
                    continue
            elif self.state.get("acceptance"):
                mode = "verify"
                usage = self.model_usage()
                if usage["today_sessions"] + usage["today_pending"] >= int(self.config["budget"]["daily"] * .3):
                    continue
            self.dispatch("team", team.get("captain_model", models["team"]), team=name, mode=mode)
            active = self.active_amb_jobs()

    def running(self, jid):
        path = self.jobs / jid / "state.json"
        if not path.exists():
            return False
        return load(path)["state"] not in TERMINAL | {"queued", "submitted", "new"}

    def apply(self, decision, expected=None):
        if expected and expected["role"] == "ruling":
            rulings = decision["rulings"]
            if not isinstance(rulings, dict) or not all(
                name in self.state["teams"] and isinstance(body, str) and body.strip()
                for name, body in rulings.items()
            ):
                raise ValueError("invalid team ruling")
            owner_messages = [row["seq"] for row in expected["messages"] if "seq" in row]
            if owner_messages and not str(decision.get("owner_response", "")).strip():
                raise ValueError("owner_response required for owner messages")
            missing = {row["team"] for row in expected["messages"] if "team" in row} - set(rulings)
            if missing:
                raise ValueError("ruling must cover every assigned blocked team")
            if owner_messages and max(owner_messages) <= self.state.get("inbox_ack", 0) and not missing:
                return
            kind = ruling_kind(expected)
            header = f"{expected['model'].capitalize()} 临时裁决\n\n" if kind == "temporary_ruling" else ""
            assigned_blocks = {row["team"]: row["block_time"] for row in expected["messages"]
                               if "block_time" in row}
            for name, body in rulings.items():
                team = self.state["teams"][name]
                block = team.get("blocking")
                block_time = timestamp(block["time"]) if block else None
                if name in assigned_blocks and assigned_blocks[name] != block_time:
                    continue
                atomic(self.path / "teams" / name / "directive.md", header + body + "\n")
                self.notify("ruling", f"{name}：{header}{body}", comment=True)
                if block_time is None or block_time <= expected["time"]:
                    team["ruling_at"] = self.now
            if owner_messages:
                atomic(self.path / "reviews" / "owner-response.md", decision["owner_response"] + "\n")
                self.state["inbox_ack"] = max(self.state.get("inbox_ack", 0), *owner_messages)
                self.notify("owner_response", decision["owner_response"], comment=True)
            self.event("ruling_applied", model=expected["model"], ruling_kind=kind)
            return
        current = self.state["cycle"]
        if (expected and expected["cycle"] != current) or decision.get("cycle", current) != current:
            raise ValueError("review belongs to an older cycle")
        if any(not isinstance(decision.get(key), str) or not decision[key].strip()
               for key in ("summary", "next")):
            raise ValueError("review needs summary and next")
        experiments = self.experiments()
        runs = list(experiments.values())
        teams = json.loads(json.dumps(self.state["teams"]))
        review_changes = []
        for name, change in decision.get("teams", {}).items():
            action = change["decision"]
            if action == "replace":
                if name in teams or not NAME.fullmatch(name) or not change.get("family"):
                    raise ValueError("replacement requires a new team and method family")
                teams[name] = {"family": change["family"], "joined": current + 1,
                               "gpu_hours": change.get("gpu_hours", 0)}
            else:
                team = teams[name]
                completed = [run for run in runs if run["team"] == name and run["status"] == "succeeded"]
                recent = [run for run in completed if run["finished_at"] >= self.state["last_review"]]
                if action != "maintain" and len(recent) < 3:
                    raise ValueError(f"{name}: three fresh results required for a change")
                if action in {"retire", "merge"}:
                    if current - team["joined"] + 1 < 2 or len(completed) < 6:
                        raise ValueError(f"{name}: retirement requires two cycles and six completed experiments")
                    if action == "merge" and (change.get("into") == name or
                                            not teams.get(change.get("into"), {}).get("active", True)
                                            or change.get("into") not in teams):
                        raise ValueError("merge requires another active team")
                    team["active"] = False
                elif action == "boost":
                    verified = {}
                    for run in recent:
                        if run["audit_passed"] and not run.get("environment_variant"):
                            verified.setdefault(run["plan"], set()).add(run["seed"])
                    if not any(len(seeds) >= 3 for seeds in verified.values()):
                        raise ValueError("boost requires three audited seeds of the same plan")
                    team["gpu_hours"] = change["gpu_hours"]
                elif action == "warn":
                    team["warning"] = change["reason"]
                elif action != "maintain":
                    raise ValueError(f"unknown decision {action}")
            if "captain_model" in change:
                if change["captain_model"] not in EXEC_MODELS:
                    raise ValueError(f"{name}: invalid captain model")
                teams[name]["captain_model"] = change["captain_model"]
            if action in {"replace", "retire", "merge", "boost"}:
                if not str(change.get("reason", "")).strip():
                    raise ValueError("review changes require a reason")
                review_changes.append(f"{name}：{change['reason']}")
        if len({team["family"] for team in teams.values() if team.get("active", True)}) < 3:
            raise ValueError("keep at least three active method families")
        acceptance = decision.get("acceptance")
        if acceptance:
            if not str(acceptance.get("criteria", "")).strip() or not acceptance.get("artifacts"):
                raise ValueError("acceptance requires criteria and artifacts")
            seeds = {run["seed"] for run in runs if run["status"] == "succeeded"
                     and run["plan"] == acceptance["plan"] and run["team"] == acceptance["team"]
                     and run["split"] == "holdout" and run["audit_passed"]
                     and not run.get("environment_variant")}
            if len(seeds) < 3:
                raise ValueError("acceptance requires three audited holdout seeds of the same plan")
            if validator := self.config.get("acceptance_validator"):
                command(validator, "check", acceptance["candidate"], acceptance["dual_review"])
            if not self.state.get("acceptance"):
                self.state["acceptance_request"] = acceptance
        for name in teams:
            directory = self.path / "teams" / name
            if not directory.exists():
                (directory / "plans").mkdir(parents=True)
                atomic(directory / "memory.md", f"# {name}\n")
        daily_gpu = self.daily_gpu_report(experiments)
        atomic(self.path / "reviews" / f"cycle-{current:02d}.md",
               f"# 第 {current} 次评审\n\n{decision['summary']}\n\n{decision['next']}\n\n"
               + "\n".join(review_changes) + "\n" + (f"\n{daily_gpu}\n" if daily_gpu else ""))
        self.state.update(teams=teams, cycle=current + 1, last_review=self.now,
                          early_review=False, review_summary=decision["summary"],
                          review_next=decision["next"], progress_dirty=True,
                          progress_urgent=True)
        self.clear("review_late")
        self.event("review_applied", cycle=current)
        review_body = [decision["summary"], decision["next"], *review_changes]
        if daily_gpu:
            review_body.append(daily_gpu)
        self.notify("daily_review" if daily_gpu else "review", "\n\n".join(review_body),
                    comment=bool(review_changes))
        self.write()

    def question(self, request):
        key = request["id"]
        if not NAME.fullmatch(key) or not self.config.get("project"):
            raise ValueError("question requires a valid id and a campaign project")
        fields = ("question", "options", "recommendation", "waiting")
        if any(not isinstance(request.get(field), str) or not request[field].strip()
               for field in fields):
            raise ValueError("question requires question, options, recommendation and waiting")
        title = f"[AR {self.path.name}/{key}] {request['question']}"
        body = ("问题：" + request["question"] + "\n\n选项：" + request["options"]
                + "\n\n建议：" + request["recommendation"] + "\n\n受影响的实验：" + request["waiting"])
        directory = self.path / "questions"
        directory.mkdir(exist_ok=True)
        path = directory / (key + ".json")
        record = load(path) if path.exists() else dict(title=title, body=body)
        if record["title"] != title or record["body"] != body:
            raise ValueError("question id already has a different request")
        if record.get("queued"):
            return
        if not record.get("card"):
            # Look up a successfully created issue before retrying a timed-out create.
            query = ("query($title:String!){issues(first:2,filter:{title:{eq:$title}})"
                     "{nodes{identifier state{name}}}}")
            cards = json.loads(command(self.linear, "gql", query,
                                       json.dumps({"title": title})))["issues"]["nodes"]
            if len(cards) > 1:
                raise ValueError("multiple Linear cards match the question id")
            if cards:
                record["card"] = cards[0]["identifier"]
            else:
                result = command(self.linear, "new", title, "--goal", body,
                                 "--project", self.config["project"])
                record["card"] = result.split()[0]
            if not CARD.fullmatch(record["card"]) or record["card"] == self.card:
                raise ValueError("invalid question card")
            save(path, record)
        issue = self.linear_read("query($id:String!){issue(id:$id){state{name}}}", id=record["card"])
        if issue["state"]["name"] not in ACCEPTANCE_STATES:
            command(self.linear, "ask", record["card"], body)
        self.notify("question", f"{record['card']}\n\n{body}")
        record["queued"] = True
        save(path, record)
        self.event("question_requested", card=record["card"], question=key)

    def progress(self, runs):
        if self.state.get("acceptance_request") and self.can_submit({"role": "review"}):
            request = self.state["acceptance_request"]
            if not self.state.get("acceptance"):
                body = request["criteria"] + "\n\n" + "\n".join(request["artifacts"])
                command(self.linear, "ask", self.card, body, "--accept")
                self.state["acceptance"] = {"time": self.now}
            self.state.pop("acceptance_request")
            self.event("acceptance_requested")
            self.notify("acceptance", request["criteria"])
            self.write()
        if self.state.get("review_comment"):
            command(self.linear, "comment", self.card, self.state["review_comment"])
            self.state.pop("review_comment")
            self.write()
        age = self.now - self.state.get("progress_at", 0)
        if not self.state.get("progress_urgent") and age < self.timing("progress_max"):
            if not self.state.get("progress_dirty") or age < self.timing("progress_min"):
                return
        summary = self.state.get("review_summary", "研究循环已登记。")
        if warning := self.state["alerts"].get("ledger_invalid"):
            summary += f"\n\n- {warning}"
        next_steps = [self.state.get("review_next", "各队按章程推进。")]
        if self.state["status"] == "stopped" and self.state.get("stopped_by"):
            next_steps = [f"已按用户消息 {self.state['stopped_by']} 停止。\n\n{self.state['stop_text']}"]
        else:
            next_steps.append(f"课题状态为 {self.state['status']}。")
            for name, team in self.state["teams"].items():
                if not team.get("active", True):
                    continue
                active = [f"{run['plan']}（预计 {run['eta']}）" for run in runs.values()
                          if run["team"] == name and run["status"] == "running"]
                if team.get("blocking"):
                    active.append(team["blocking"]["what"])
                next_steps.append(f"- {name}：{'；'.join(active) or '推进本队待试事项'}")
        path = self.path / ".progress.md"
        atomic(path, "## 现在到哪了\n" + summary + "\n\n## 下一步\n" + "\n".join(next_steps) + "\n")
        command(self.linear, "progress", self.card, str(path))
        self.state.update(progress_dirty=False, progress_urgent=False, progress_at=self.now)

    def slots(self, runs):
        path = self.path / "slots.json"
        slots = load(path)
        for gpu, slot in slots.items():
            for record in runs.values():
                if gpu in ar_quota.slot_keys(record) and record["status"] == "running":
                    slot.update(run=record["id"], expires_at=record["lease_until"])
            run = runs.get(slot.get("run"))
            if run and run["status"] != "running":
                slot.pop("run")
                slot.pop("expires_at")
        save(path, slots)
        return slots

    def publish_lease(self, run):
        if "h200_indices" not in run:
            return
        directory = self.transfer / "leases" / self.path.name
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        save(directory / f"{run['id']}.json",
             {key: run[key] for key in ("id", "machine", "workdir", "tmux", "lease_until", "h200_indices")})

    def register(self, run):
        if "h200_indices" in run:
            raise ValueError("H200 indices come from the allocated slots")
        ident = run["id"]
        runs = self.experiments()
        if not NAME.fullmatch(ident):
            raise ValueError("invalid experiment ID")
        if ident in runs:
            existing = runs[ident]
            if "lease_until" in run:
                run = dict(run, lease_until=datetime.fromtimestamp(
                    timestamp(run["lease_until"]), timezone.utc).isoformat())
            if (existing["status"] == "running" and "h200_indices" in existing
                    and all(existing.get(key) == value for key, value in run.items())):
                self.publish_lease(existing)
                return
            raise ValueError("experiment ID must be unique")
        team = self.state["teams"][run["team"]]
        purpose = run.get("mode", "explore")
        if purpose not in {"explore", "verify"}:
            raise ValueError("experiment mode must be explore or verify")
        if not team.get("active", True) or not self.can_submit({"role": "team", "mode": purpose}):
            raise ValueError("campaign or team is not accepting new experiments")
        alarm = self.resources()
        self.refresh_model_usage()
        self.budget(alarm)
        if alarm or self.state["status"] != "running":
            raise ValueError("new experiments are paused by resources or budget")
        if self.state.get("acceptance") and purpose == "verify":
            today = [row for row in runs.values() if row.get("mode") == "verify"
                     and self.now - row["registered_at"] < 86400]
            if len(today) >= int(self.config["budget"]["daily"] * .3):
                raise ValueError("verification limit reached")
        if run.get("transport") == "h200":
            directory = self.transfer / "runs" / self.path.name / ident
            if (directory / "heartbeat.json").exists():
                raise ValueError("H200 experiment ID already has a remote heartbeat")
            run = dict(run, heartbeat=str(directory / "heartbeat.json"),
                       result=str(directory / "result.json"), log=str(directory / "run.log"))
        for key in ("plan", "code", "config", "seed", "heartbeat", "result", "artifact", "eta"):
            if key not in run or run[key] == "":
                raise ValueError(f"experiment requires {key}")
        if not NAME.fullmatch(run["plan"]) or type(run["seed"]) is not int:
            raise ValueError("invalid experiment plan or seed")
        plan_attempts = [r for r in runs.values() if r["team"] == run["team"]
                         and r["plan"] == run["plan"] and r["seed"] == run["seed"]
                         and r["status"] not in {"running", "infrastructure_interrupted"}]
        if len(plan_attempts) >= 3 and all(r["status"] == "failed" for r in plan_attempts[-3:]):
            raise ValueError("experiment failed three consecutive attempts")
        keys = ar_quota.slot_keys(run)
        if keys:
            if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
                raise ValueError("GPU slots must be distinct slot IDs")
            for key in ("machine", "tmux", "workdir", "lease_until"):
                if not run.get(key):
                    raise ValueError(f"GPU experiment requires {key}")
            if not self.config.get("resources") or not self.config.get("resource_rules"):
                raise ValueError("GPU experiments require resource monitoring and KML rules")
            slots = self.slots(runs)
            expiry = timestamp(run["lease_until"])
            if not math.isfinite(expiry) or expiry <= self.now:
                raise ValueError("GPU lease must end in the future")
            if any(slots[key].get("run") or slots[key]["team"] != run["team"]
                   or slots[key]["machine"] != run["machine"] for key in keys):
                raise ValueError("GPU slot unavailable")
            quota = ar_quota.gpu_quota(runs, self.state["teams"], self.state["last_review"], self.now)[run["team"]]
            requested = (expiry - self.now) * len(keys)
            if quota["used_card_seconds"] + quota["reserved_card_seconds"] + requested > quota["limit_card_seconds"]:
                raise ValueError("team GPU quota exhausted for this review cycle")
            if run.get("transport") == "h200":
                indices = [slots[key]["index"] for key in keys]
                if (any(type(index) is not int or not 0 <= index <= 7 for index in indices)
                        or len(set(indices)) != len(indices)):
                    raise ValueError("H200 slots require distinct indices from 0 to 7")
                if run["tmux"] != f"ar-{self.path.name}-{ident}":
                    raise ValueError("H200 tmux name must match the campaign and run ID")
                run["h200_indices"] = sorted(indices)
                run["lease_until"] = datetime.fromtimestamp(expiry, timezone.utc).isoformat()
            for key in keys:
                slots[key].update(run=ident, expires_at=run["lease_until"])
        plan = self.path / "teams" / run["team"] / "plans" / (run["plan"] + ".md")
        if plan.is_file():
            objects = re.search(r"^-[ \t]+改动对象[：:]([^\n]*)", plan.read_text(encoding="utf-8"), re.MULTILINE)
            if objects and "环境" in objects[1].strip().split("；", 1)[0]:
                run = dict(run, environment_variant=True)
        self.record_run(dict(run, registered_at=self.now, status="running"))
        if keys:
            save(self.path / "slots.json", slots)
        self.publish_lease(run)
        self.event("experiment_registered", id=ident, team=run["team"])

    def tick(self):
        self.flush_outbox()
        runs = self.experiments(quarantine=True)
        self.collect_runs(runs)
        self.leaderboard(runs)
        self.slots(runs)
        alarm = self.resources()
        self.state["resource_alarm"] = alarm
        self.refresh_model_usage()
        self.budget(alarm)
        card_ok = False
        card_error = None
        try:
            card_ok = self.check_card()
            self.clear("linear_unavailable")
        except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
            self.alert("linear_unavailable", str(error))
            card_error = error
        self.reconcile_jobs(allow_dispatch=card_ok)
        if card_ok:
            try:
                self.ask_budget()
                self.clear("budget_question")
            except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
                self.alert("budget_question", str(error))
            try:
                self.progress(runs)
            except (ValueError, OSError, subprocess.TimeoutExpired) as error:
                self.alert("linear_progress", str(error))
            else:
                self.clear("linear_progress")
            self.schedule(runs)
        self.flush_outbox()
        if card_error and self.can_submit({"role": "ruling"}):
            raise card_error

    def checked_tick(self):
        heartbeat = self.state.get("heartbeat")
        failures = self.state.get("tick_failures", 0)
        failure_since = self.state.get("tick_failure_since", self.now)
        try:
            self.tick()
            active = len(self.active_amb_jobs())
            self.state.update(heartbeat=self.now, tick_failures=0)
            self.state.pop("tick_failure_since", None)
            self.state.pop("last_tick_error", None)
            self.checkpoint()
        except Exception as error:
            detail = f"{type(error).__name__}: {' '.join(str(error).splitlines())}"[:500]
            self.state.update(heartbeat=heartbeat, tick_failures=failures + 1,
                              tick_failure_since=failure_since, last_tick_error=detail)
            self.write()
            print(f"{self.path.name}: tick failed ({failures + 1}): {detail}", file=sys.stderr)
            return 1
        when = datetime.fromtimestamp(self.now, timezone.utc).isoformat(timespec="seconds")
        hold = self.dispatch_hold()
        dispatch = f"; dispatch held: {hold}" if hold else ""
        print(f"{when} {self.path.name}: tick ok; active amb jobs {active}/{self.campaign_limit}{dispatch}",
              flush=True)
        return 0


def initialize(path, config, charter):
    if not CARD.fullmatch(config["card"]):
        raise ValueError("config.card must be a Linear issue identifier")
    for key, value in config.get("timing", {}).items():
        if key not in TIMING or type(value) is not int or value < 1:
            raise ValueError(f"invalid timing: {key}")
    if any(team.get("card") for team in config["teams"].values()):
        raise ValueError("teams do not have Linear cards")
    teams = config["teams"]
    if len({team["family"] for team in teams.values()}) < 3 or not all(NAME.fullmatch(name) for name in teams):
        raise ValueError("init requires three distinct method families and valid team names")
    limits = config["budget"]
    if any(type(value) is not int or value < 1 for value in
           (limits["daily"], limits["total"], limits.get("concurrency", CAMPAIGN_CONCURRENCY))):
        raise ValueError("budget limits must be positive integers")
    if config.get("resource_rules_file"):
        config["resource_rules"] = Path(config["resource_rules_file"]).read_text(encoding="utf-8")
    charter_text = charter.read_text(encoding="utf-8")
    evaluation_section = re.search(
        r"^##[ \t]+4[.、][ \t]*评测口径[ \t]*$\n(.*?)(?=^#{1,2}[ \t]|\Z)",
        charter_text, re.MULTILINE | re.DOTALL,
    )
    benchmark_field = re.compile(r"^[ \t]*(?:-[ \t]+)?外部可比基准[：:]([^\n]*)$", re.MULTILINE)
    fields = benchmark_field.findall(evaluation_section[1]) if evaluation_section else []
    if len(fields) != 1 or not fields[0].strip() or fields[0].strip().startswith("<"):
        raise ValueError("章程第 4 节须填写外部可比基准；没有公开 benchmark 时写明理由并开提问卡")
    if config.get("resource_rules"):
        charter_text += "\n\n" + config["resource_rules"]
    path.mkdir(parents=True)
    command("git", "init", "-q", cwd=path)
    save(path / "config.json", config)
    atomic(path / "charter.md", charter_text)
    save(path / "state.json", {"status": "paused", "started": False, "heartbeat": None,
                                "teams": {name: dict(team, joined=1) for name, team in teams.items()},
                                "cycle": 1, "last_review": 0, "jobs": {}, "usage": {}, "alerts": {}})
    for name in ("reviews", "teams"):
        (path / name).mkdir()
    for name in teams:
        directory = path / "teams" / name
        (directory / "plans").mkdir(parents=True)
        atomic(directory / "memory.md", f"# {name}\n")
    for name in ("inbox.jsonl", "outbox.jsonl", "ledger.jsonl", "events.jsonl", "forum.md", "survey.md", "leaderboard.md"):
        (path / name).touch()
    append(path / "outbox.jsonl", {"time": time.time(), "kind": "charter", "text": charter_text})
    save(path / "slots.json", config.get("slots", {}))


def main(argv=None, now=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(option(
        "AR_ROOT", str(Path.home() / "autoresearch/campaigns"))))
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("init", "start", "tick", "status", "say", "pause", "resume", "stop",
                   "apply", "register", "block", "question", "set-models"):
        child = sub.add_parser(action)
        child.add_argument("name", nargs="?" if action == "tick" else None)
        if action == "init":
            child.add_argument("--config", type=Path, required=True)
            child.add_argument("--charter", type=Path, required=True)
            child.add_argument("--card", help="override the existing campaign card in config")
            child.add_argument("--project", help="record the Linear project in config")
        if action == "say":
            child.add_argument("text")
        if action == "stop":
            child.add_argument("--owner-msg", required=True)
        if action in {"apply", "register", "block", "question"}:
            child.add_argument("file", type=Path)
        if action == "set-models":
            for role in MODELS:
                child.add_argument("--" + role.replace("_", "-"), choices=EXEC_MODELS)
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    if args.name and not NAME.fullmatch(args.name):
        parser.error("invalid campaign name")
    paths = [root / args.name] if args.name else sorted(
        path for path in root.glob("*") if (path / "config.json").exists())
    result = 0
    for path in paths:
        try:
            if args.action == "init":
                config = load(args.config)
                for key in ("card", "project"):
                    supplied = getattr(args, key)
                    if supplied:
                        config[key] = supplied
                initialize(path, config, args.charter)
            with (path / ".lock").open("a") as lock:
                flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if args.action == "tick" else 0)
                try:
                    fcntl.flock(lock, flags)
                except BlockingIOError:
                    continue
                campaign = Campaign(path, now if now is not None else time.time())
                if args.action == "tick":
                    result |= campaign.checked_tick()
                    continue
                if args.action == "stop":
                    campaign.stop(args.owner_msg)
                    campaign.checkpoint()
                    continue
                try:
                    execute(campaign, args)
                finally:
                    if args.action not in {"status", "set-models"}:
                        campaign.checkpoint()
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
            print(f"{path.name}: {error}", file=sys.stderr)
            result = 1
    return result


def execute(campaign, args):
    state = campaign.state
    action = args.action
    if action == "status":
        view = dict(state)
        view["dispatch_hold"] = campaign.dispatch_hold()
        view["role_models"] = campaign.role_models()
        if state["started"]:
            view["review_window"] = {"earliest": state["last_review"] + campaign.timing("review_earliest"),
                                     "latest": state["last_review"] + campaign.timing("review_interval")}
        view["budget"] = dict(campaign.config["budget"], concurrency=campaign.campaign_limit)
        active = campaign.active_amb_jobs()
        view["amb_concurrency"] = {"active": len(active), "by_team": dict(Counter(
            team for team in active.values() if team is not None)),
            "unassigned": sum(team is None for team in active.values())}
        runs = campaign.experiments()
        view["quota"] = {"model": campaign.model_usage(),
                         "gpu": ar_quota.gpu_quota(runs, state["teams"], state["last_review"], campaign.now),
                         "gpu_usage": campaign.gpu_usage(runs)}
        print(json.dumps(view, ensure_ascii=False, indent=2))
        print((campaign.path / "leaderboard.md").read_text(), end="")
    elif action in {"start", "resume", "pause"}:
        if state["status"] == "stopped" and action == "pause":
            raise ValueError("campaign already stopped")
        state.update(status="paused" if action == "pause" else "running",
                     progress_dirty=True, progress_urgent=True)
        if action in {"start", "resume"} and not state["started"]:
            state.update(started=True, last_review=campaign.now)
        campaign.event(action)
    elif action == "say":
        path = campaign.path / "inbox.jsonl"
        if not args.text.strip():
            raise ValueError("empty owner instruction")
        append(path, {"seq": len(rows(path)) + 1, "time": campaign.now, "text": args.text})
    elif action == "apply":
        campaign.apply(load(args.file))
    elif action == "set-models":
        campaign.set_models({role: getattr(args, role) for role in MODELS if getattr(args, role)})
    elif action == "register":
        campaign.register(load(args.file))
    elif action == "question":
        campaign.question(load(args.file))
        campaign.flush_outbox()
    elif action == "block":
        change = load(args.file)
        team = state["teams"][change["team"]]
        if change.get("clear") is True:
            team.pop("blocking", None)
        else:
            if not all(str(change.get(key, "")).strip() for key in ("who", "what", "recheck_at")):
                raise ValueError("blocking requires who, what and recheck_at")
            timestamp(change["recheck_at"])
            if "ask_lead" in change and type(change["ask_lead"]) is not bool:
                raise ValueError("ask_lead must be a boolean")
            team["blocking"] = dict(change, time=campaign.now)
            team["blocking"].pop("ruling_queued_at", None)
        team.pop("ruling_at", None)
        state.update(progress_dirty=True, progress_urgent=True)
        campaign.event("team_block", team=change["team"], blocked="blocking" in team)


if __name__ == "__main__":
    sys.exit(main())
