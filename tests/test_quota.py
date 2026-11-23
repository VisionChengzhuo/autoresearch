import contextlib
import io
import json
import unittest

import ar
import ar_quota
import test_ar


class QuotaTest(unittest.TestCase):
    def setUp(self):
        # Reuse the existing subprocess-backed campaign fixture.
        self.loop = test_ar.LoopTest()
        self.addCleanup(self.loop.doCleanups)
        self.loop.setUp()
        self.loop.config["drill"] = True
        ar.save(self.loop.campaign / "config.json", self.loop.config)
        self.transfer = self.loop.base / "transfer"
        self.transfer.mkdir()

    def campaign(self):
        campaign = ar.Campaign(self.loop.campaign, self.loop.now)
        campaign.transfer = self.transfer
        return campaign

    def test_child_retries_and_review_count_by_actual_model_without_queue_minutes(self):
        fixture = self.loop
        fixture.tick()
        parent = fixture.active("alpha")
        child = fixture.jobs / "J-0000-child"
        child.mkdir()
        ar.save(child / "job.json", {"id": child.name, "campaign": "fake",
                                    "parent": parent, "exec": "sol"})
        ar.save(child / "state.json", {"state": "done", "started_at": fixture.now, "attempts": {
            "1": {"exec_model": "gpt-6.2-sol", "review_model": "claude-opus-5-5"},
            "2": {"exec_model": "claude-opus-5-5"}}, "step_timings": [
            {"attempt": 1, "step": "exec", "process_s": 30, "wall_s": 900},
            {"attempt": 1, "step": "accept", "process_s": 20},
            {"attempt": 1, "step": "review", "process_s": 10},
            {"attempt": 2, "step": "exec", "process_s": None, "wall_s": 40},
        ]})
        fixture.config["budget"].update(daily=3, total=15)
        ar.save(fixture.campaign / "config.json", fixture.config)
        fixture.tick()
        self.assertEqual(fixture.state()["status"], "throttled")
        usage = self.campaign().model_usage()
        self.assertEqual(usage["sessions"], 3)
        self.assertEqual(usage["models"]["gpt-6.2-sol"]["seconds"], 30)
        self.assertEqual(usage["teams"]["alpha"], {"sessions": 3, "seconds": 80})
        fixture.tick()
        self.assertEqual(self.campaign().model_usage()["sessions"], 3)
        fixture.tick(86400)
        self.assertEqual(fixture.state()["status"], "running")

    def test_total_limit_opens_one_question_and_keeps_card_running(self):
        fixture = self.loop
        fixture.config["drill"] = False
        fixture.config["budget"]["total"] = 1
        ar.save(fixture.campaign / "config.json", fixture.config)
        fixture.tick()
        parent = fixture.active("alpha")
        ar.save(fixture.jobs / parent / "state.json", {"state": "done", "started_at": fixture.now,
                 "step_timings": [{"attempt": 1, "step": "exec", "process_s": 5}]})
        fixture.tick()
        fixture.tick()
        self.assertEqual(fixture.state()["status"], "throttled")
        self.assertEqual(ar.load(fixture.issue)["state"]["name"], "In Progress")
        self.assertEqual(sum(call[:2] == ["linear-app", "new"] for call in fixture.calls()), 1)

    def test_gpu_lease_reserves_all_slots_and_bills_failed_run_after_outage(self):
        fixture = self.loop
        telemetry = fixture.base / "resource.json"
        ar.save(telemetry, {"time": fixture.now, "alarm": False, "shm_fraction": .05, "memory_fraction": .10})
        fixture.config.update(resources=[{"machine": "h200", "snapshot": str(telemetry)}], resource_rules="rules")
        ar.save(fixture.campaign / "config.json", fixture.config)
        ar.save(fixture.campaign / "slots.json", {
            str(i): {"machine": "h200", "team": "alpha", "index": i} for i in range(3)})
        campaign = self.campaign()
        campaign.state["teams"]["alpha"]["gpu_hours"] = 1 / 12
        run = dict(id="R1", team="alpha", plan="P1", config="test", code="test", seed=1, artifact="shared",
                   eta="ready", gpu_slots=["0", "1"], machine="h200", workdir="/shared",
                   tmux="ar-fake-R1", lease_until=fixture.now + 120, transport="h200")
        campaign.register(run)
        campaign.register(run)
        receipt = ar.load(self.transfer / "leases" / "fake" / "R1.json")
        self.assertEqual(receipt["h200_indices"], [0, 1])
        self.assertEqual(len(campaign.experiments()), 1)
        with self.assertRaisesRegex(ValueError, "quota exhausted"):
            campaign.register(dict(run, id="R2", tmux="ar-fake-R2", gpu_slots=["2"],
                                   lease_until=fixture.now + 61))
        started = fixture.now + 10
        output = self.transfer / "runs" / "fake" / "R1"
        output.mkdir(parents=True)
        ar.save(output / "heartbeat.json", {"alive": True, "time": started, "started_at": started})
        campaign.now += 200
        with contextlib.redirect_stderr(io.StringIO()):
            campaign.collect_runs()
        self.assertEqual(campaign.experiments()["R1"]["status"], "running")
        ar.save(output / "heartbeat.json", {"alive": False, "time": started + 40, "exit_code": 1,
                                            "started_at": started, "finished_at": started + 40})
        campaign.collect_runs()
        campaign.collect_runs()
        result = campaign.experiments()["R1"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["gpu_card_seconds"], 80)
        quota = ar_quota.gpu_quota(campaign.experiments(), campaign.state["teams"],
                                   fixture.now, campaign.now)["alpha"]
        self.assertEqual(quota["used_card_seconds"], 80)
        self.assertEqual(quota["reserved_card_seconds"], 0)

    def test_gpu_status_and_daily_review_use_same_cards_and_split_at_beijing_midnight(self):
        fixture = self.loop
        midnight = ar.timestamp("2026-01-02T00:00:00+08:00")
        review_time = ar.timestamp("2026-01-02T09:00:00+08:00")
        campaign = self.campaign()
        finished = dict(id="R1", team="alpha", plan="P1", seed=1, status="failed",
                        machine="h200", gpu_slots=["L1", "L0"], h200_indices=[0, 1],
                        registered_at=midnight - 100, gpu_started_at=midnight - 60,
                        finished_at=midnight + 60, gpu_card_seconds=240)
        active = dict(id="R2", team="alpha", plan="P1", seed=2, status="running",
                      machine="h200", gpu="L2", registered_at=review_time - 120,
                      lease_until=review_time + 120, heartbeat=str(fixture.base / "pulse.json"),
                      result=str(fixture.base / "result.json"), eta="ready")
        campaign.record_run(dict(finished, status="running", heartbeat=active["heartbeat"],
                                 result=active["result"], eta="ready", lease_until=midnight + 120))
        campaign.record_run(finished)
        campaign.record_run(active)
        ar.save(fixture.base / "pulse.json", {"time": review_time - 30,
                "started_at": review_time - 60, "alive": True})
        campaign.record_run(dict(id="legacy", team="alpha", plan="P1", seed=3,
                                 status="failed", finished_at=midnight - 120, card_hours=0.01))
        campaign.now = review_time - 1
        self.assertEqual(campaign.daily_gpu_report(campaign.experiments()), "")
        campaign.now = review_time
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fixture.now = review_time
            fixture.cli("status", "fake")
        view, _ = json.JSONDecoder().raw_decode(out.getvalue())
        use = view["quota"]["gpu_usage"]
        self.assertEqual((use["card_count"], use["card_seconds"], use["unattributed_card_seconds"]),
                         (3, 306, 36))
        self.assertEqual([(row["card"], row["card_seconds"]) for row in use["cards"]],
                         [("GPU 0", 120), ("GPU 1", 120), ("L2", 30)])
        campaign.apply({"summary": "评审", "next": "继续"})
        campaign.apply({"summary": "下次评审", "next": "继续"})
        campaign.flush_outbox()
        entries = ar.rows(fixture.campaign / "outbox.jsonl")
        daily, = [entry for entry in entries if entry["kind"] == "daily_review"]
        self.assertIn("北京时间 2026-01-01", daily["text"])
        self.assertIn("已识别 2 张卡，合计 0.0433 卡时", daily["text"])
        self.assertIn("h200 / GPU 0，0.0167 卡时", daily["text"])
        self.assertIn("历史记录缺少卡号，0.0100 卡时", daily["text"])
        self.assertNotIn("L2", daily["text"])
        self.assertIn("0.0433 卡时", (fixture.campaign / "reviews/cycle-01.md").read_text())
        self.assertTrue(any("日报" in sent["args"][1]
                            for sent in ar.rows(fixture.base / "aligned.jsonl")))
