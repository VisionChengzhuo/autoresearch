"""Campaign quota accounting from Ambition phase timings and registered GPU leases."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import re

MODEL_PHASES = {"plan": "plan", "exec": "exec", "repair": "exec", "review": "review"}
CHINA = timezone(timedelta(hours=8))
JOB_ID = re.compile(r"J-[A-Za-z0-9][A-Za-z0-9_.-]*")


def day_at(epoch):
    return datetime.fromtimestamp(epoch, CHINA).date().isoformat()


def epoch(value):
    if type(value) in (int, float) and math.isfinite(value):
        return value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return parsed.timestamp()
    raise ValueError("expected timestamp with timezone")


def model_name(step, snapshot, phase, requested):
    attempt = snapshot.get("attempts", {}).get(str(step.get("attempt", snapshot.get("attempt", 1))), {})
    return attempt.get(phase + "_model") or attempt.get(phase) or (
        requested if phase in ("plan", "exec") else "unknown")


def sessions(job, snapshot, now):
    completed = snapshot.get("step_timings") or []
    first_start = snapshot.get("started_at")
    running = snapshot.get("run") or {}
    active = running.get("step") in MODEL_PHASES
    transitions = defaultdict(list)
    for item in snapshot.get("history") or []:
        if item.get("to") in MODEL_PHASES:
            transitions[item["to"]].append(item.get("ts"))
    entries = []
    for step in completed:
        name = step.get("step")
        if name not in MODEL_PHASES:
            continue
        phase = MODEL_PHASES[name]
        starts = transitions[phase]
        started = starts.pop(0) if starts else first_start
        duration = step.get("process_s")
        measured = duration is not None
        if not measured:
            duration = step.get("wall_s", 0)
        entries.append(dict(model=model_name(step, snapshot, phase, job.get("exec")),
                            phase=name, day=day_at(epoch(started) if started else now),
                            seconds=max(0, duration), basis="process" if measured else "step_wall"))
    if active:
        name = running["step"]
        started = epoch(running.get("began") or running["started"])
        entries.append(dict(model=model_name(running, snapshot, MODEL_PHASES[name], job.get("exec")),
                            phase=name, day=day_at(started), seconds=max(0, now - started),
                            basis="running"))
    if not entries and first_start and not completed:
        # Older Ambition states can lack per-step timings. Never include queue time.
        began = epoch(first_start)
        ended = epoch(snapshot.get("updated") or now)
        entries.append(dict(model=job.get("exec") or "unknown", phase="exec", day=day_at(began),
                            seconds=max(0, ended - began), basis="job_wall"))
    return entries


def collect_models(jobs, campaign, previous, now):
    result = dict(previous)
    if not jobs.is_dir():
        return result
    discovered = {}
    for directory in jobs.iterdir():
        if not JOB_ID.fullmatch(directory.name):
            continue
        try:
            job = json.loads((directory / "job.json").read_text())
            if job.get("campaign") != campaign:
                continue
            snapshot = json.loads((directory / "state.json").read_text())
            discovered[directory.name] = (job, snapshot)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    for ident, (job, snapshot) in discovered.items():
        parent = job.get("parent") or job.get("lead_parent")
        entries = sessions(job, snapshot, now)
        result[ident] = {"parent": parent, "entries": entries,
                         "state": snapshot["state"]}
    return result


def summarize_models(observed, reserved, day, terminal):
    by_model = defaultdict(lambda: dict(sessions=0, seconds=0, today_sessions=0, today_seconds=0))
    by_team = defaultdict(lambda: dict(sessions=0, seconds=0))
    pending = today_pending = 0
    for ident in observed.keys() | reserved.keys():
        row = observed.get(ident, {})
        reservation = reserved.get(ident, {})
        team = reservation.get("team")
        parent = row.get("parent")
        seen = {ident}
        while not team and parent and parent not in seen:
            seen.add(parent)
            team = reserved.get(parent, {}).get("team")
            parent = observed.get(parent, {}).get("parent")
        entries = row.get("entries", [])
        if not row and reservation.get("minutes") is not None:
            entries = [dict(model=reservation.get("model"), day=reservation["day"], seconds=0)]
        for entry in entries:
            model, seconds = entry["model"] or "unknown", entry["seconds"]
            counter = by_model[model]
            counter["sessions"] += 1
            counter["seconds"] += seconds
            if entry["day"] == day:
                counter["today_sessions"] += 1
                counter["today_seconds"] += seconds
            by_team[team or "campaign"]["sessions"] += 1
            by_team[team or "campaign"]["seconds"] += seconds
        if not entries and row.get("state") not in terminal:
            pending += 1
            today_pending += reservation.get("day", day) == day
    return dict(models=dict(sorted(by_model.items())), teams=dict(sorted(by_team.items())),
                sessions=sum(v["sessions"] for v in by_model.values()),
                today_sessions=sum(v["today_sessions"] for v in by_model.values()),
                pending=pending, today_pending=today_pending)


def slot_keys(run):
    return run.get("gpu_slots") or ([run["gpu"]] if run.get("gpu") else [])


def utilization(run, pulse, now):
    count = len(slot_keys(run))
    if not count:
        return 0
    now = min(now, epoch(run["lease_until"]))
    start = pulse.get("started_at")
    if start is None:
        return 0
    end = pulse.get("finished_at") or pulse.get("time") or now
    return max(0, min(epoch(end), now) - max(epoch(start), run["registered_at"])) * count


def gpu_usage(runs, pulses, now, since=0, until=None):
    """Report observed use without counting unused lease reservations."""
    until = now if until is None else min(now, until)
    cards = defaultdict(float)
    unattributed = 0
    for ident, run in runs.items():
        slots = slot_keys(run)
        if run["status"] == "running":
            if not slots or ident not in pulses:
                continue
            pulse = pulses[ident]
            try:
                epoch(pulse["time"])
                start = max(epoch(pulse["started_at"]), run["registered_at"])
                seconds = utilization(run, pulse, now)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        else:
            seconds = run.get("gpu_card_seconds", run.get("card_hours", 0) * 3600)
            if not seconds or "finished_at" not in run:
                continue
            start = run.get("gpu_started_at")
            if start is None:
                start = run["finished_at"] - seconds / (len(slots) or 1)
            elif "registered_at" in run:
                start = max(start, epoch(run["registered_at"]))
        if seconds <= 0:
            continue
        duration = seconds / (len(slots) or 1)
        overlap = max(0, min(start + duration, until) - max(start, since))
        if not overlap:
            continue
        if not slots:
            unattributed += overlap
            continue
        machine = run.get("machine") or "unknown"
        indices = run.get("h200_indices")
        identities = [f"GPU {index}" for index in indices] if (
            isinstance(indices, list) and len(indices) == len(slots)
            and all(type(index) is int for index in indices)
        ) else slots
        for card in identities:
            cards[machine, card] += overlap
    total = sum(cards.values()) + unattributed
    return dict(card_count=len(cards), card_seconds=total, card_hours=total / 3600,
                unattributed_card_seconds=unattributed,
                cards=[dict(machine=machine, card=card, card_seconds=seconds, card_hours=seconds / 3600)
                       for (machine, card), seconds in sorted(cards.items())])


def gpu_quota(runs, teams, since, now):
    totals = {name: dict(used_card_seconds=0, reserved_card_seconds=0,
                         limit_card_seconds=team.get("gpu_hours", 0) * 3600)
              for name, team in teams.items()}
    for run in runs.values():
        keys = slot_keys(run)
        if not keys or run["team"] not in totals:
            continue
        team = totals[run["team"]]
        if run["status"] == "running":
            team["reserved_card_seconds"] += max(
                0, epoch(run["lease_until"]) - max(since, run["registered_at"])) * len(keys)
        elif run.get("finished_at", 0) >= since:
            measured = run.get("gpu_card_seconds")
            if measured is None:
                measured = run.get("card_hours", 0) * 3600
            else:
                start = run.get("gpu_started_at")
                if start is not None:
                    measured = max(0, measured - max(0, since - start) * len(keys))
            team["used_card_seconds"] += measured
    return totals
