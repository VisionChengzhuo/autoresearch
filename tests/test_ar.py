import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ar

TESTS = Path(__file__).resolve().parent
START = ar.timestamp("2026-01-01T00:00:00+00:00")


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / "campaigns"
        self.campaign = self.root / "fake"
        self.now = START
        self.jobs = self.base / "jobs"
        self.issue = self.base / "issue.json"
        self.cards("In Progress")
        for tool in ("amb", "linear-app"):
            (self.base / tool).symlink_to(TESTS / "fake_cli.py")
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"AR_TEST_DIR": str(self.base), "AR_AMB": str(self.base / "amb"),
                               "AR_AMB_JOBS": str(self.jobs), "AR_LINEAR": str(self.base / "linear-app"),
                               "AR_LINEAR_RECORDS": str(self.base / "records")}).start()
        self.eval = self.base / "evaluation"
        self.eval.write_text("frozen")
        self.config = {
            "card": "FA-1", "linear_owner_id": "owner", "project": "Fake project",
            "teams": {name: {"family": name} for name in ("alpha", "beta", "gamma")},
            "budget": {"daily": 20, "total": 100, "concurrency": 4},
            "evaluation": {key: {"path": str(self.eval), "sha256": ar.sha(self.eval)}
                           for key in ("script", "scenarios")},
        }
        ar.save(self.base / "config.json", self.config)
        (self.base / "charter.md").write_text(
            "# 假课题\n\n完成标准为 0.99。\n\n## 4. 评测口径\n- 外部可比基准：假方法在假公开任务得 0.42。\n"
        )
        self.cli("init", "fake", "--config", str(self.base / "config.json"),
                 "--charter", str(self.base / "charter.md"))
        self.cli("start", "fake")

    def test_init_rejects_missing_or_unfilled_external_benchmark(self):
        charter = self.base / "invalid.md"
        for contents in (
            "# 课题\n- 外部可比基准：填错章节",
            "## 4. 评测口径\n- 外部可比基准：<模板占位符>",
            "## 4. 评测口径\n- 外部可比基准：\n- 种子：1",
        ):
            with self.subTest(contents=contents):
                charter.write_text(contents)
                error = self.cli("init", "invalid", "--config", str(self.base / "config.json"),
                                 "--charter", str(charter), code=1)
                self.assertIn("外部可比基准", error)
                self.assertFalse((self.root / "invalid").exists())

    def cards(self, status, actor="agent", description=""):
        ar.save(self.issue, {"id": "fake-card", "state": {"id": status, "name": status},
                            "labels": {"nodes": [{"name": "长期"}]}, "description": description,
                            "transition": {"actor": {"id": actor}, "toStateId": status}})

    def cli(self, *args, code=0):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            actual = ar.main(["--root", str(self.root), *args], now=self.now)
        self.assertEqual(actual, code, err.getvalue())
        return err.getvalue()

    def state(self):
        return ar.load(self.campaign / "state.json")

    def tick(self, delta=60):
        self.now += delta
        return self.cli("tick", "fake")

    def active(self, team):
        return next(ident for ident, job in self.state()["jobs"].items() if job["team"] == team)

    def finish(self, ident, status="done"):
        ar.save(self.jobs / ident / "state.json", {"state": status, "updated": self.now,
                                                  "review": {"verdict": "PASS"}, "open_items": []})

    def run_record(self, ident="R1", team="alpha", **extras):
        run = {"id": ident, "team": team, "plan": "P1", "code": "fake-rev", "config": "fake-config",
               "seed": 1, "artifact": "/fake/artifact", "eta": "10:30", **extras,
               "heartbeat": str(self.base / f"{ident}.status.json"),
               "result": str(self.base / f"{ident}.result.json")}
        ar.save(self.base / "run.json", run)
        self.cli("register", "fake", str(self.base / "run.json"))
        return run

    def calls(self):
        return ar.rows(self.base / "calls.jsonl")

    def worker(self, ident, *, status="exec", campaign="fake", **metadata):
        directory = self.jobs / ident
        directory.mkdir(parents=True)
        ar.save(directory / "job.json", {"campaign": campaign, **metadata})
        ar.save(directory / "state.json", {"state": status})

    def test_campaign_limit_counts_workers_and_owner_ruling(self):
        self.config["budget"].pop("concurrency")
        ar.save(self.campaign / "config.json", self.config)
        for index in range(8):
            self.worker(f"J-0000-{100 + index}", status="queued" if index == 0 else "exec")
        self.worker("J-0000-200", campaign="other")
        self.worker("J-0000-201", status="done")
        self.tick(6 * ar.HOUR)
        self.assertFalse(self.state()["jobs"])
        self.cli("say", "fake", "请先裁决。")
        self.tick()
        ruling, = self.state()["jobs"]
        self.assertEqual(self.state()["jobs"][ruling]["role"], "ruling")
        campaign = ar.Campaign(self.campaign, self.now)
        self.assertEqual(len(campaign.active_amb_jobs()), 9)
        self.finish("J-0000-100")
        self.tick()
        self.assertEqual(list(self.state()["jobs"]), [ruling])
        self.finish("J-0000-101")
        self.tick()
        self.assertCountEqual([job["role"] for job in self.state()["jobs"].values()], ["ruling", "review"])

    def test_team_limit_uses_job_metadata_and_parent_chain(self):
        self.config["budget"]["concurrency"] = 8
        ar.save(self.campaign / "config.json", self.config)
        self.worker("J-0000-100", team="alpha")
        self.worker("J-0000-101", lead_parent="J-0000-102")
        self.worker("J-0000-102", status="done", team="alpha")
        self.worker("J-0000-103", title="alpha P-001 legacy worker")
        self.tick()
        self.assertEqual({job["team"] for job in self.state()["jobs"].values()}, {"beta", "gamma"})
        self.finish("J-0000-101")
        self.tick()
        self.assertFalse(any(job["team"] == "alpha" for job in self.state()["jobs"].values()))
        self.finish("J-0000-103")
        self.tick()
        captain = self.jobs / self.active("alpha")
        self.assertEqual(ar.load(captain / "job.json")["team"], "alpha")
        self.assertIn("派队员前先用 amb status", (captain / "brief.md").read_text())

    def test_pending_captain_cannot_retry_at_campaign_capacity(self):
        self.config["budget"]["concurrency"] = 1
        ar.save(self.campaign / "config.json", self.config)
        with patch.object(ar.Campaign, "submit", side_effect=ValueError("amb submit unavailable")):
            self.now += 60
            self.cli("tick", "fake", code=1)
        captain, = self.state()["jobs"]
        self.worker("J-0000-100")
        self.tick()
        self.assertTrue((self.jobs / "_draft" / captain / "job.json").exists())
        self.finish("J-0000-100")
        self.tick()
        self.assertTrue((self.jobs / captain / "state.json").exists())

    def test_malformed_ledger_rows_are_isolated_and_scheduling_continues(self):
        run = self.run_record()
        ar.save(Path(run["heartbeat"]), {"time": self.now, "alive": True})
        ledger = self.campaign / "ledger.jsonl"
        with ledger.open("ab") as stream:
            stream.write(b'{"progress":"missing id"}\n[]\n{broken\n{"id":"R1"}\n')
        before = ledger.read_bytes()
        self.assertIn("ledger.jsonl line 2 quarantined", self.tick())
        self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual(len(ar.rows(self.campaign / "ledger.quarantine.jsonl")), 4)
        message = self.state()["alerts"]["ledger_invalid"]
        self.assertIn("4 条", message)
        self.assertIn(message, (self.base / "progress.md").read_text().split("## 下一步")[0])
        self.assertEqual({job["team"] for job in self.state()["jobs"].values()}, {"beta", "gamma"})
        self.assertNotIn("quarantined", self.tick())
        self.assertEqual(len(ar.rows(self.campaign / "ledger.quarantine.jsonl")), 4)

    def test_watchdog_alerts_on_schedule_failures_with_recent_heartbeat(self):
        self.tick()
        pulse = self.state()["heartbeat"]
        ar.append(self.campaign / "ledger.jsonl", {"progress": "no id"})
        watchdog = [str(TESTS.parent / "watchdog.sh"), str(self.root)]
        with patch.object(ar.Campaign, "schedule", side_effect=RuntimeError("injected scheduler error")):
            for count in (1, 2, 3):
                self.now += 60
                errors = self.cli("tick", "fake", code=1)
                if count == 1:
                    self.assertIn("quarantined", errors)
                state = self.state()
                self.assertEqual((state["heartbeat"], state["tick_failures"]), (pulse, count))
                self.assertIn("injected scheduler error", state["last_tick_error"])
                check = subprocess.run(watchdog, env=dict(os.environ, AR_WATCHDOG_NOW=str(self.now)),
                                       capture_output=True, text=True)
                self.assertEqual(check.returncode, int(count == 3), check.stderr)
        self.assertIn("3 consecutive AutoResearch tick failures", check.stderr)
        self.assertNotIn("stale", check.stderr)
        self.assertIn("3 consecutive", ar.rows(self.base / "aligned.jsonl")[-1]["text"])
        self.tick()
        self.assertEqual(self.state()["heartbeat"], self.now)
        self.assertEqual(self.state()["tick_failures"], 0)
        self.assertNotIn("last_tick_error", self.state())

    def test_leaderboard_separates_environment_variants(self):
        plan = self.campaign / "teams" / "alpha" / "plans" / "P1.md"
        plan.write_text("- 改动对象：算法 / 环境\n")
        for team, split, score in (("alpha", "holdout", .99), ("beta", "holdout", .55),
                                   ("gamma", "development", .73)):
            for seed in range(3):
                run = self.run_record(f"{team}-{seed}", team=team, seed=seed)
                ar.save(Path(run["result"]), {"score": score, "split": split, "audit_passed": True})
        self.tick()
        report = (self.campaign / "leaderboard.md").read_text()
        ultimate, remaining = report.split("## 代理指标", 1)
        proxy, variant = remaining.split("## 环境变体", 1)
        self.assertIn("0.55", ultimate)
        self.assertIn("0.73", proxy)
        self.assertIn("0.99", variant)
        self.assertNotIn("0.99", ultimate + proxy)

    def test_next_round_after_pass_and_no_close(self):
        self.tick()
        first = self.active("alpha")
        self.finish(first)
        self.tick()
        self.assertNotEqual(first, self.active("alpha"))
        self.assertEqual(self.state()["status"], "running")
        self.assertTrue(all(row[:2] != ["linear-app", "state"] for row in self.calls()))

    def test_child_cards_done_and_review_pass_leave_campaign_running(self):
        self.tick()
        card = ar.load(self.issue)
        card["children"] = {"nodes": [{"state": {"name": "Done"}} for _ in range(3)]}
        ar.save(self.issue, card)
        for jid in self.state()["jobs"]:
            self.finish(jid)
        self.tick()
        self.assertEqual(self.state()["status"], "running")
        self.assertEqual(ar.load(self.issue)["state"]["name"], "In Progress")
        self.assertFalse(any(call[:2] == ["linear-app", "state"] for call in self.calls()))

    def test_recovery_drill(self):
        self.tick()
        lost_lead = self.active("alpha")
        with subprocess.Popen(["sleep", "120"]) as lead_session:
            lead_session.kill()
            lead_session.wait()
            self.finish(lost_lead, "failed")
        killed = self.run_record(team="beta")
        scored = self.run_record("R2", team="gamma")
        self.finish(self.active("beta"))
        self.finish(self.active("gamma"))
        with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"]) as worker:
            worker.kill()
            worker.wait()
        ar.save(Path(killed["heartbeat"]), {"alive": False, "time": self.now})
        ar.save(Path(scored["result"]), {"score": .42})
        self.cards("Done")
        watchdog = subprocess.run([str(TESTS.parent / "watchdog.sh"), str(self.root)],
                                  env=dict(os.environ, AR_WATCHDOG_NOW=str(self.now + 900)),
                                  capture_output=True, text=True)
        self.assertEqual(watchdog.returncode, 1)
        self.assertIn("heartbeat stale", watchdog.stderr)
        errors = self.tick(900)
        self.assertNotIn("restoring In Progress", errors)
        self.assertEqual(ar.load(self.issue)["state"]["name"], "Done")
        self.assertEqual(self.state()["linear_hold"], "Done")
        self.assertFalse(any(call[:2] in (["linear-app", "state"], ["linear-app", "comment"])
                             for call in self.calls()))
        self.cards("In Progress")
        self.tick()
        ledger = ar.rows(self.campaign / "ledger.jsonl")
        self.assertEqual([row["status"] for row in ledger],
                         ["running", "running", "failed", "succeeded"])
        self.assertIn("0.42", (self.campaign / "leaderboard.md").read_text())
        jobs = self.state()["jobs"]
        self.assertEqual({job["team"] for job in jobs.values()}, {"alpha", "beta", "gamma"})
        self.assertNotIn(lost_lead, jobs)
        (self.campaign / "state.json").write_text("{broken")
        self.assertIn("recovered state", self.tick())
        self.assertEqual(jobs, self.state()["jobs"])
        self.assertEqual(ledger, ar.rows(self.campaign / "ledger.jsonl"))
        self.assertEqual(sum(row[:2] == ["amb", "submit"] for row in self.calls()), 6)

    def test_stop_and_emergency_file(self):
        error = self.cli("stop", "fake", "--owner-msg", "unknown", code=1)
        self.assertIn("owner", error)
        with self.assertRaises(SystemExit) as error, contextlib.redirect_stderr(io.StringIO()):
            self.cli("stop", "fake")
        self.assertEqual(error.exception.code, 2)
        (self.campaign / "STOP").touch()
        self.tick()
        self.assertFalse(self.state()["jobs"])
        with patch.object(ar.Campaign, "owner_message", return_value={
            "text": "停止课题 fake。", "received_at": self.now - 1,
        }):
            self.cli("stop", "fake", "--owner-msg", "M-1")
        self.tick()
        self.assertEqual(self.state()["status"], "stopped")
        self.assertIn("停止课题 fake。", (self.campaign / "charter.md").read_text())
        self.assertIn("停止课题 fake。", (self.base / "progress.md").read_text())

    def test_daily_budget(self):
        self.config["budget"]["daily"] = 1
        ar.save(self.campaign / "config.json", self.config)
        self.tick()
        self.assertEqual(self.state()["status"], "throttled")
        (self.campaign / "STOP").touch()
        self.tick(86400)
        self.assertEqual(self.state()["status"], "running")

    def test_pins_and_release(self):
        self.tick()
        run = self.run_record()
        self.finish(self.active("alpha"))
        self.tick()
        self.assertFalse(any(job["team"] == "alpha" for job in self.state()["jobs"].values()))
        ar.save(Path(run["result"]), {"score": .99})
        self.eval.write_text("tampered")
        self.assertIn("evaluation hash mismatch", self.tick())
        self.assertEqual(ar.rows(self.campaign / "ledger.jsonl")[-1]["status"], "rejected")
        self.assertTrue(self.active("alpha"))

    def test_resource_guard_signal_collected_without_touching_keeper(self):
        self.tick()
        run = self.run_record()
        snapshot = self.base / "resource.json"
        self.config["resources"] = [{"machine": "fake-gpu", "snapshot": str(snapshot)}]
        ar.save(self.campaign / "config.json", self.config)
        with subprocess.Popen(["sleep", "120"]) as worker, subprocess.Popen(["sleep", "120"]) as keeper:
            try:
                shm = .95
                # Fake ar-guard producer. Only its own experiment receives the signal.
                if shm >= .95:
                    worker.kill()
                    worker.wait()
                ar.save(snapshot, {"time": self.now, "alarm": True, "shm_fraction": shm,
                                   "memory_fraction": .2})
                ar.save(Path(run["heartbeat"]), {"alive": False, "resource_killed": True,
                                                 "time": self.now, "reason": "shm reached 95%"})
                self.tick()
                self.assertEqual(self.state()["status"], "throttled")
                self.assertIsNone(keeper.poll())
            finally:
                keeper.terminate()
                keeper.wait()
                if worker.poll() is None:
                    worker.terminate()
        self.assertEqual(ar.rows(self.campaign / "ledger.jsonl")[-1]["status"], "failed")
        ar.save(snapshot, {"time": self.now, "alarm": False, "shm_fraction": .1, "memory_fraction": .2})
        self.tick()
        self.assertEqual(self.state()["status"], "running")

    def test_card_manual_owner_and_open_acceptance(self):
        desc = "## 验收申请\n申请时间：2026-01-01T00:00:00+00:00\n\n证据。"
        self.cards("Human Intervention", description=desc)
        records = self.base / "records"
        records.mkdir()
        key = hashlib.sha256(b"fake-card").hexdigest()
        ar.save(records / (key + ".json"), {"status": "open", "requested_at": START_ISO})
        self.tick()
        self.assertEqual(ar.load(self.issue)["state"]["name"], "Human Intervention")
        self.assertTrue(self.state()["acceptance"])
        self.cards("Done", actor="owner", description=desc)
        self.tick()
        self.assertEqual(self.state()["status"], "stopped")
        self.assertEqual(ar.load(self.issue)["state"]["name"], "Done")
        self.assertFalse(any(row[:2] == ["linear-app", "state"] for row in self.calls()))

    def test_reviews_and_blocking_ruling(self):
        self.tick()
        self.cli("say", "fake", "先看方向 B。")
        self.tick()
        ident = next(i for i, job in self.state()["jobs"].items() if job["role"] == "ruling")
        output = self.state()["jobs"][ident]["output"]
        ar.save(Path(output), {"rulings": {"beta": "先评估 B。"}, "owner_response": "已接收。"})
        self.finish(ident)
        self.tick()
        self.assertEqual(self.state()["inbox_ack"], 1)
        self.tick(6 * ar.HOUR)
        ident = next(i for i, job in self.state()["jobs"].items() if job["role"] == "review")
        ar.save(Path(self.state()["jobs"][ident]["output"]),
                {"summary": "假课题距阈值 0.57。", "next": "继续三队实验。"})
        self.finish(ident)
        self.tick()
        self.assertEqual(self.state()["cycle"], 2)
        self.assertIn("0.57", (self.base / "progress.md").read_text())

    def test_role_handoff_keeps_running_legacy_ruling_and_routes_new_jobs(self):
        self.cli("say", "fake", "先用旧裁决收尾。")
        self.tick()
        ruling_id, = [jid for jid, job in self.state()["jobs"].items() if job["role"] == "ruling"]
        state = self.state()
        legacy = state["jobs"][ruling_id]
        legacy.pop("ruling_kind")
        self.assertEqual(legacy["model"], "fable")
        ar.save(self.campaign / "state.json", state)
        options = ("--review", "astra", "--ruling", "astra", "--team", "sol",
                   "--temporary-ruling", "sol")
        calls = self.calls()
        self.cli("set-models", "fake", *options)
        self.cli("set-models", "fake", *options)
        changed = self.state()
        self.assertEqual(changed["jobs"], state["jobs"])
        self.assertEqual(self.calls(), calls)
        self.assertEqual(changed["role_models"], dict(review="astra", ruling="astra",
                                                     team="sol", temporary_ruling="sol"))
        self.assertEqual({team["captain_model"] for team in changed["teams"].values()}, {"sol"})
        ledger = ar.rows(self.campaign / "ops-ledger.jsonl")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0], ar.rows(self.campaign / "events.jsonl")[-1])
        self.tick(delta=ar.TIMING["ruling_fallback"])
        self.assertEqual(self.state()["jobs"], changed["jobs"])
        ar.save(Path(legacy["output"]), {"rulings": {"alpha": "旧裁决照常应用。"},
                                       "owner_response": "已收到。"})
        self.finish(ruling_id)
        for jid, job in self.state()["jobs"].items():
            if job["role"] == "team":
                self.finish(jid)
        self.tick()
        self.assertEqual(self.state()["inbox_ack"], 1)
        self.assertEqual((self.campaign / "teams/alpha/directive.md").read_text(), "旧裁决照常应用。\n")
        self.assertEqual({job["model"] for job in self.state()["jobs"].values()}, {"sol"})

        self.cli("say", "fake", "请新领队裁决。")
        self.tick()
        primary, = [(jid, job) for jid, job in self.state()["jobs"].items() if job["role"] == "ruling"]
        self.assertEqual((primary[1]["model"], primary[1]["ruling_kind"]), ("astra", "ruling"))
        self.finish(primary[0], "queued")
        self.tick(delta=ar.TIMING["ruling_fallback"])
        fallback, = [(jid, job) for jid, job in self.state()["jobs"].items()
                     if job.get("ruling_kind") == "temporary_ruling"]
        self.assertEqual(fallback[1]["model"], "sol")
        self.tick()
        self.assertEqual(sum(job.get("ruling_kind") == "temporary_ruling"
                             for job in self.state()["jobs"].values()), 1)
        ar.save(Path(fallback[1]["output"]), {"rulings": {"alpha": "临时方案。"},
                                             "owner_response": "先执行临时方案。"})
        self.finish(fallback[0])
        self.finish(primary[0], "failed")
        self.tick(delta=ar.TIMING["review_interval"])
        self.assertEqual((self.campaign / "teams/alpha/directive.md").read_text(),
                         "Sol 临时裁决\n\n临时方案。\n")
        review, = [job for job in self.state()["jobs"].values() if job["role"] == "review"]
        self.assertEqual(review["model"], "astra")
        ar.save(Path(review["output"]), {"summary": "进展。", "next": "继续。",
                                        "teams": {"alpha": {"decision": "maintain", "captain_model": "astra"}}})
        self.finish(Path(review["output"]).stem)
        self.tick()
        self.assertEqual(self.state()["teams"]["alpha"]["captain_model"], "astra")

    def test_charter_and_decisions_reach_owner_without_waiting(self):
        """与人类对齐：章程、裁决、大评审决定生效同时经 amb align 发给用户（设计 reply 第 7 节）。"""
        (self.base / "align-fail").write_text("1")
        self.tick()
        self.assertIn("outbox", self.state()["alerts"])
        self.assertFalse((self.base / "aligned.jsonl").exists())
        (self.base / "align-fail").unlink()
        self.tick()
        sent = ar.rows(self.base / "aligned.jsonl")
        self.assertIn("完成标准为 0.99", sent[0]["text"])
        self.assertIn("章程", sent[0]["args"][1])
        self.assertIn("FA-1", sent[0]["args"])
        self.assertNotIn("outbox", self.state()["alerts"])
        self.cli("say", "fake", "先看方向 B。")
        self.tick()
        ident = next(i for i, job in self.state()["jobs"].items() if job["role"] == "ruling")
        ar.save(Path(self.state()["jobs"][ident]["output"]),
                {"rulings": {"beta": "先评估 B。"}, "owner_response": "已接收方向 B。"})
        self.finish(ident)
        self.tick()
        texts = [row["text"] for row in ar.rows(self.base / "aligned.jsonl")]
        self.assertTrue(any("先评估 B" in text for text in texts))
        self.assertTrue(any("已接收方向 B" in text for text in texts))
        self.assertEqual(len(texts), len(set(texts)))
        self.assertEqual(self.state()["outbox_sent"], len(ar.rows(self.campaign / "outbox.jsonl")))

    def test_question_uses_campaign_card_and_fake_campaign_jobs_have_no_card(self):
        self.config["drill"] = True
        ar.save(self.campaign / "config.json", self.config)
        self.tick()
        jobs = list(self.state()["jobs"])
        for jid in jobs:
            spec = ar.load(self.jobs / jid / "job.json")
            self.assertTrue(spec["test"])
            self.assertNotIn("--linear", spec["args"])
            self.assertNotIn("linear", spec)
            self.assertIn("--campaign", spec["args"])
            self.finish(jid)
        ar.append(self.campaign / "amb_events.jsonl", {"id": jobs[0], "state": "done"})
        self.tick()
        self.assertIn(jobs[0], [event.get("job") for event in ar.rows(self.campaign / "events.jsonl")
                                if event["event"] == "amb_terminal_event"])
        request = self.base / "question.json.input"
        ar.save(request, {"id": "choose", "question": "假实验选 A 吗？", "options": "A / B",
                          "recommendation": "A", "waiting": "假实验 P1"})
        self.cli("question", "fake", str(request))
        self.cli("question", "fake", str(request))
        self.assertFalse(any(call[:2] == ["linear-app", "new"] for call in self.calls()))
        self.assertEqual([call[2] for call in self.calls() if call[:2] == ["linear-app", "ask"]], ["FA-1"])
        self.assertTrue(any("FA-1" in sent["text"] for sent in ar.rows(self.base / "aligned.jsonl")))
        self.assertFalse(any(call[:2] == ["linear-app", "state"] for call in self.calls()))

    def test_compressed_timing_two_reviews_pause_resume_and_fresh_progress(self):
        self.config["timing"] = {"review_earliest": 119, "review_interval": 120, "review_late": 360,
                                 "progress_min": 10, "progress_max": 60, "block_escalation": 90}
        ar.save(self.campaign / "config.json", self.config)
        self.tick(10)
        self.cli("pause", "fake")
        for jid in self.state()["jobs"]:
            self.finish(jid)
        self.tick(60)
        self.assertEqual(self.state()["status"], "paused")
        self.assertFalse(self.state()["jobs"])
        first_write = self.state()["progress_at"]
        self.tick(60)
        self.assertGreater(self.state()["progress_at"], first_write)
        self.cli("resume", "fake")
        for index in (1, 2):
            self.tick(120)
            jid = next(jid for jid, job in self.state()["jobs"].items() if job["role"] == "review")
            ar.save(Path(self.state()["jobs"][jid]["output"]),
                    {"summary": f"假课题评审 {index}", "next": "继续假实验。"})
            self.finish(jid)
            self.tick(1)
        self.assertEqual(self.state()["cycle"], 3)
        reviews = [sent for sent in ar.rows(self.base / "aligned.jsonl") if "大评审决定" in sent["args"][1]]
        self.assertEqual(len(reviews), 2)
        self.assertTrue(all("--card" not in sent["args"] for sent in reviews))

    def test_external_block_escalates_once_at_configured_age(self):
        self.config["timing"] = {"block_escalation": 90}
        ar.save(self.campaign / "config.json", self.config)
        request = self.base / "block.json"
        ar.save(request, {"team": "alpha", "who": "fake reviewer", "what": "等待假输入",
                          "recheck_at": self.now + 600})
        self.cli("block", "fake", str(request))
        self.tick(89)
        self.assertFalse(any(job["role"] == "ruling" for job in self.state()["jobs"].values()))
        self.tick(1)
        self.tick(1)
        rulings = [job for job in self.state()["jobs"].values() if job["role"] == "ruling"]
        self.assertEqual(len(rulings), 1)
        self.assertEqual(rulings[0]["model"], "fable")
        self.assertEqual(rulings[0]["messages"][0]["text"], "等待假输入")

    def block(self, team, what, *, who="外部负责人", recheck=7200, **fields):
        request = self.base / "block.json"
        ar.save(request, dict(team=team, who=who, what=what, recheck_at=self.now + recheck, **fields))
        self.cli("block", "fake", str(request))

    def rulings(self):
        return {jid: job for jid, job in self.state()["jobs"].items() if job["role"] == "ruling"}

    def test_help_and_legacy_requests_dispatch_over_both_caps(self):
        self.config["budget"]["concurrency"] = 8
        ar.save(self.campaign / "config.json", self.config)
        for index in range(8):
            self.worker(f"J-0000-{100 + index}", team="alpha" if index < 2 else None)
        self.block("alpha", "请批准方案", ask_lead=True)
        self.block("beta", "需要定方向", who="Astra 领队")
        self.block("gamma", "请求领队裁决：方向 C")
        self.tick()
        ruling, = self.rulings().values()
        self.assertEqual(ruling["ruling_kind"], "ruling")
        self.assertEqual([row["team"] for row in ruling["messages"]], ["alpha", "beta", "gamma"])
        self.assertEqual(len(ar.Campaign(self.campaign, self.now).active_amb_jobs()), 9)
        events = ar.rows(self.campaign / "events.jsonl")
        self.assertEqual([row["team"] for row in events if row["event"] == "block_ruling_queued"],
                         ["alpha", "beta", "gamma"])

    def test_help_continues_captain_and_falls_back_after_ten_minutes(self):
        self.block("alpha", "请改派", ask_lead=True)
        self.tick()
        primary, = self.rulings()
        self.finish(primary, "queued")
        captain = self.active("alpha")
        self.assertIn("继续待试清单", (self.jobs / captain / "brief.md").read_text())
        self.tick(ar.TIMING["ruling_fallback"] - 61)
        self.assertEqual(list(self.rulings()), [primary])
        self.tick(1)
        self.assertEqual({job["ruling_kind"] for job in self.rulings().values()},
                         {"ruling", "temporary_ruling"})
        self.tick(ar.TIMING["ruling_late"] - ar.TIMING["ruling_fallback"])
        self.assertEqual(len(self.rulings()), 2)
        self.assertIn("ruling_late", self.state()["alerts"])

    def test_external_recheck_only_escalates_latest_registration(self):
        self.block("alpha", "请求领队裁决字样不覆盖显式分类", who="领队", recheck=120,
                   ask_lead=False)
        self.block("beta", "等待授权", ask_lead=False, recheck=120)
        self.tick()
        self.assertFalse(self.rulings())
        self.assertNotIn("alpha", {job["team"] for job in self.state()["jobs"].values()})
        self.block("beta", "授权仍在核实", ask_lead=False, recheck=120)
        self.tick()
        first, = self.rulings().values()
        self.assertEqual([row["team"] for row in first["messages"]], ["alpha"])
        self.finish(next(iter(self.rulings())), "exec")
        self.tick()
        self.assertEqual([job["messages"][0]["team"] for job in self.rulings().values()],
                         ["alpha", "beta"])

    def test_old_ruling_and_late_writeback_do_not_mask_new_block(self):
        state = self.state()
        state["teams"]["alpha"]["ruling_at"] = self.now - 1
        state["teams"]["alpha"]["blocking"] = dict(
            time=self.now, who="Astra 领队", what="旧格式的新阻塞", recheck_at=self.now + 7200)
        ar.save(self.campaign / "state.json", state)
        self.tick()
        first, = self.rulings()
        ar.save(Path(self.rulings()[first]["output"]), {"rulings": {"alpha": "第一份批示"}})
        self.finish(first)
        self.tick()
        self.assertFalse(self.rulings())
        self.block("alpha", "再次请求改派", ask_lead=True)
        self.tick()
        second, = self.rulings()
        self.block("alpha", "改用新的请求", ask_lead=True)
        ar.save(Path(self.rulings()[second]["output"]), {"rulings": {"alpha": "过期批示"}})
        self.finish(second)
        self.tick()
        latest, = self.rulings().values()
        self.assertEqual(latest["messages"][0]["text"], "改用新的请求")
        self.assertEqual((self.campaign / "teams/alpha/directive.md").read_text(), "第一份批示\n")

    def test_ruling_submit_retry_failure_allows_temporary_dispatch(self):
        self.block("alpha", "请改派", ask_lead=True)
        submit = ar.Campaign.submit

        def submit_except_primary(campaign, jid, job):
            if job.get("ruling_kind") == "ruling":
                raise ValueError("primary submission unavailable")
            return submit(campaign, jid, job)

        with patch.object(ar.Campaign, "submit", submit_except_primary):
            self.now += 60
            self.cli("tick", "fake", code=1)
            self.tick(ar.TIMING["ruling_fallback"] - 60)
        temporary, = [jid for jid, job in self.rulings().items()
                      if job["ruling_kind"] == "temporary_ruling"]
        self.assertTrue((self.jobs / temporary / "state.json").exists())

    def test_acceptance_uses_audited_holdout(self):
        self.tick()
        for seed in range(3):
            run = self.run_record(f"R{seed}", seed=seed)
            ar.save(Path(run["result"]), {"score": .99, "split": "holdout", "audit_passed": True})
            self.tick()
        request = {"criteria": "完成标准 0.99，三个留出集种子均达到。", "artifacts": ["https://example.invalid/fake"],
                   "plan": "P1", "team": "alpha"}
        review = self.base / "review.json"
        ar.save(review, {"summary": "达到目标。", "next": "复现。", "acceptance": request})
        self.cli("apply", "fake", str(review))
        self.tick()
        self.tick()
        self.assertEqual(self.state()["status"], "running")
        self.assertEqual(ar.load(self.issue)["state"]["name"], "In Progress")
        self.assertTrue(self.state()["acceptance"])
        self.assertEqual(sum(row[:2] == ["linear-app", "ask"] for row in self.calls()), 1)


    def test_dispatch_hold_visible_in_status_and_tick_without_changing_campaign_state(self):
        def status():
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.cli("status", "fake")
            return json.JSONDecoder().raw_decode(stdout.getvalue())[0]

        stop = self.campaign / "STOP"
        stop.touch()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.tick()
        self.assertIn("tick ok; active amb jobs 0/4; dispatch held: STOP", stdout.getvalue())
        self.assertEqual(self.state()["heartbeat"], self.now)
        self.assertEqual(status()["status"], "running")
        self.assertEqual(status()["dispatch_hold"], "STOP")
        self.assertFalse(self.state()["jobs"])

        self.cli("pause", "fake")
        self.assertEqual(status()["dispatch_hold"], "STOP")
        stop.unlink()
        self.assertEqual(status()["dispatch_hold"], "paused")
        self.cli("resume", "fake")
        self.assertIsNone(status()["dispatch_hold"])
        self.tick()
        self.assertEqual(len(self.state()["jobs"]), 3)

    def test_throttled_campaign_dispatches_blocking_ruling(self):
        self.tick()
        self.config["budget"]["daily"] = 1
        ar.save(self.campaign / "config.json", self.config)
        for jid in self.state()["jobs"]:
            ar.save(self.jobs / jid / "state.json", {"state": "done", "started_at": self.now,
                    "step_timings": [{"attempt": 1, "step": "exec", "process_s": 5}]})
        self.cli("pause", "fake")
        self.tick(6 * ar.HOUR)
        self.cli("say", "fake", "请给 beta 裁决。")
        self.tick()
        self.assertFalse(self.state()["jobs"])
        self.cli("resume", "fake")
        stop = self.campaign / "STOP"
        stop.touch()
        self.tick()
        self.assertEqual(self.state()["status"], "throttled")
        self.assertFalse(self.state()["jobs"])
        stop.unlink()
        self.tick()
        self.assertEqual(self.state()["status"], "throttled")
        jobs = self.state()["jobs"]
        ruling_id, = [jid for jid, job in jobs.items() if job["role"] == "ruling"]
        ruling = jobs[ruling_id]
        self.assertEqual(ruling["model"], "fable")
        self.assertEqual(ruling["messages"][0]["text"], "请给 beta 裁决。")
        self.assertTrue(all(j["role"] == "ruling" or
                            (j["role"] == "team" and j["mode"] == "consolidate")
                            for j in jobs.values()))
        ar.save(Path(ruling["output"]), {"rulings": {"beta": "继续复算。"}, "owner_response": "已处理。"})
        self.finish(ruling_id)
        self.tick()
        self.assertEqual(self.state()["inbox_ack"], 1)
        self.assertIn("继续复算。", (self.campaign / "teams/beta/directive.md").read_text())


START_ISO = "2026-01-01T00:00:00+00:00"

if __name__ == "__main__":
    unittest.main()
