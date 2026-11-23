import contextlib
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ar


REQUESTED = "2026-01-02T01:00:00+00:00"
CREATED = "2026-01-02T01:01:00+00:00"
RECEIVED = "2026-01-02T01:02:00+00:00"
NOW = ar.timestamp("2026-01-02T01:03:00+00:00")
APPROVAL = f"同意验收 FA-10 {REQUESTED}"


class StopAuthorizationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaigns"
        self.path = self.root / "fake-nav"
        self.path.mkdir(parents=True)
        self.log = self.base / "fake-inbound.jsonl"
        self.config = {"card": "FA-10", "linear_owner_id": "fake-owner", "owner_log": str(self.log)}
        ar.save(self.path / "config.json", self.config)
        ar.save(self.path / "state.json", {
            "status": "running", "teams": {}, "jobs": {}, "usage": {}, "alerts": {}, "cycle": 1,
        })
        (self.path / "charter.md").write_text("# 假课题\n", encoding="utf-8")
        for name in ("events.jsonl", "inbox.jsonl"):
            (self.path / name).touch()
        self.records = self.base / "acceptance"
        self.records.mkdir()
        self.record_path = self.records / (hashlib.sha256(b"fake-linear-uuid").hexdigest() + ".json")
        self.issue = {
            "id": "fake-linear-uuid",
            "state": {"id": "fake-review", "name": "Human Intervention"},
            "labels": {"nodes": [{"name": "长期"}]},
            "description": f"## 验收申请\n申请时间：{REQUESTED}\n\n",
        }
        ar.save(self.record_path, {"status": "open", "requested_at": REQUESTED})
        self.enterContext(patch.dict(os.environ, {"AR_LINEAR_RECORDS": str(self.records)}))
        self.checkpoint = self.enterContext(patch.object(
            ar.Campaign, "checkpoint", autospec=True, side_effect=ar.Campaign.write))
        self.snapshot = self.enterContext(patch.object(ar.Campaign, "card_snapshot", return_value=self.issue))
        self.command = self.enterContext(patch.object(ar, "command", return_value=""))

    def message(self, text, **overrides):
        self.log.write_text("", encoding="utf-8")
        row = {"id": "fake-1", "text": text, "received_at": RECEIVED, **overrides}
        ar.append(self.log, row)
        return row

    @contextlib.contextmanager
    def protected_fixture(self, *, writable=None, non_root=None):
        # Substitute permission metadata only for the temporary log and its path chain.
        protected = {self.log, *self.log.parents}
        real_stat = Path.stat
        real_access = os.access

        def stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path not in protected:
                return result
            fields = list(result)
            fields[4] = 1 if path == non_root else 0
            return os.stat_result(fields)

        def access(path, mode, *args, **kwargs):
            if path in protected and mode == os.W_OK:
                return path == writable
            return real_access(path, mode, *args, **kwargs)

        with patch.object(Path, "stat", stat), patch.object(os, "access", access):
            yield

    def cli(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = ar.main(["--root", str(self.root), *args], now=NOW)
        return code, err.getvalue()

    def assert_denied(self, substring="owner", ident="fake-1"):
        tracked = ["state.json", "charter.md", "events.jsonl", "inbox.jsonl"]
        before = {name: (self.path / name).read_bytes() for name in tracked}
        result, error = self.cli("stop", "fake-nav", "--owner-msg", ident)
        self.assertEqual(result, 1, error)
        self.assertIn(substring, error)
        self.assertEqual(before, {name: (self.path / name).read_bytes() for name in tracked})
        self.checkpoint.assert_not_called()

    def assert_stopped(self, row):
        code, err = self.cli("stop", "fake-nav", "--owner-msg", row["id"])
        self.assertEqual(code, 0, err)
        state = ar.load(self.path / "state.json")
        self.assertEqual((state["status"], state["stopped_by"], state["stop_text"], state["stop_time"]),
                         ("stopped", row["id"], row["text"], row["received_at"]))
        self.assertIn(row["text"], (self.path / "charter.md").read_text(encoding="utf-8"))
        self.assertEqual(ar.rows(self.path / "events.jsonl")[-1]["event"], "owner_stop")

    def test_explicit_stop_uses_full_original_and_campaign_name(self):
        row = self.message("  请停止课题 fake-nav。  ")
        with self.protected_fixture():
            self.assert_stopped(row)
        self.snapshot.assert_not_called()
        self.command.assert_not_called()

    def test_explicit_stop_can_identify_the_campaign_by_card(self):
        row = self.message("停止 FA-10", received_at=NOW - 1)
        with self.protected_fixture():
            self.assert_stopped(row)
        self.snapshot.assert_not_called()

    def test_unrelated_ambiguous_negative_pause_and_wrong_target_are_denied(self):
        for text in (
            "暂停 fake-nav", "不要停止课题 fake-nav", "停止课题 fake-nav？",
            "考虑停止课题 fake-nav", "先停止课题 fake-nav，等一下继续",
            "谢谢", "OK", "停止", "停止本课题", "停止 fake-nav-2", "停止 FA-100",
            "停止 FA-11", "停止课题 other-nav", "停止 fake-nav 和 other-nav",
            "转发：停止课题 fake-nav", "停止课题 fake-nav。\n不要执行",
            "停止 fake-nav 的子实验", "同意验收", f"同意验收 FA-11 {REQUESTED}",
            f"不同意验收 FA-10 {REQUESTED}",
        ):
            with self.subTest(text=text), self.protected_fixture():
                self.message(text)
                self.assert_denied()
        self.snapshot.assert_not_called()

    def test_writable_untrusted_symlinked_and_relative_logs_are_denied(self):
        self.message("停止 FA-10")
        for conditions in ({"writable": self.log}, {"writable": self.base}, {"non_root": self.base}):
            with self.subTest(conditions=conditions), self.protected_fixture(**conditions):
                self.assert_denied("owner log")
        real_log = self.log.with_suffix(".original")
        self.log.rename(real_log)
        self.log.symlink_to(real_log)
        with self.protected_fixture():
            self.assert_denied("owner log")
        ar.save(self.path / "config.json", {**self.config, "owner_log": self.log.name})
        self.assert_denied("owner log")

    def test_unknown_duplicate_malformed_and_future_messages_are_denied(self):
        for changes in (
            {"id": "other-id"}, {"text": " "}, {"text": 42}, {"received_at": NOW + 1},
            {"received_at": True}, {"received_at": "2026-01-02T01:02:00"},
            {"created_at": "2026-01-02T01:02:30+00:00"},
        ):
            with self.subTest(changes=changes), self.protected_fixture():
                self.message(**{"text": "停止 FA-10", **changes})
                self.assert_denied("requires a timezone" if changes.get("received_at") == "2026-01-02T01:02:00"
                                   else "owner")
        with self.protected_fixture():
            row = self.message("停止 FA-10")
            ar.append(self.log, row)
            self.assert_denied()
            self.log.write_text('{"malformed"\n', encoding="utf-8")
            self.assert_denied("Expecting")

    def test_campaign_inbox_does_not_authorize_stop(self):
        self.message("消息记录里的日常聊天")
        ar.append(self.path / "inbox.jsonl", {"id": "fake-1", "text": "停止 FA-10"})
        with self.protected_fixture():
            self.assert_denied()

    def test_current_card_acceptance_is_allowed(self):
        row = self.message(APPROVAL, created_at=CREATED)
        with self.protected_fixture():
            self.assert_stopped(row)
        self.snapshot.assert_called_once()
        self.command.assert_not_called()

    def test_acceptance_requires_exact_request_and_creation_after_request(self):
        for text, created in (
            (APPROVAL, REQUESTED), (APPROVAL, "2026-01-02T00:59:59+00:00"),
            (APPROVAL + "？", CREATED), (APPROVAL + "，但是还要继续", CREATED),
            (APPROVAL.replace("01:00:00", "00:50:00"), CREATED),
        ):
            with self.subTest(text=text, created=created), self.protected_fixture():
                self.message(text, created_at=created)
                self.assert_denied()
        with self.protected_fixture():
            self.message(APPROVAL)
            self.assert_denied()

    def test_acceptance_requires_a_pending_request_on_this_card(self):
        self.message(APPROVAL, created_at=CREATED)
        with self.protected_fixture():
            description = self.issue.pop("description")
            self.assert_denied()
            self.issue["description"] = description
            self.issue["state"]["name"] = "Done"
            self.assert_denied()
            self.issue["state"]["name"] = "In Review"
            ar.save(self.record_path, {"status": "withdrawn", "requested_at": REQUESTED})
            self.assert_denied()
            self.record_path.unlink()
            self.assert_denied()

    def check_automated_closure(self, text, *, manual=False):
        row = self.message(text, created_at=CREATED)
        ar.save(self.record_path, {"status": "closed", "requested_at": REQUESTED, "owner_msg": row["id"]})
        self.issue["state"]["name"] = "Done"
        self.issue["description"] += f"## 你的验收答复\n（fake-1）\n> {text}"
        campaign = ar.Campaign(self.path, NOW)
        with self.protected_fixture(), patch.object(campaign, "last_transition", return_value={
            "actor": {"id": "fake-owner" if manual else "fake-agent"},
        }):
            return campaign.check_card(), campaign.state["status"]

    def test_automated_closure_accepts_verified_acceptance(self):
        self.assertEqual(self.check_automated_closure(APPROVAL), (False, "stopped"))
        self.command.assert_not_called()

    def test_automated_closure_rejects_wrong_card_even_if_quoted(self):
        self.assertEqual(self.check_automated_closure(APPROVAL.replace("FA-10", "FA-11")),
                         (True, "running"))
        self.assertEqual(self.command.call_args_list[0].args[1:], ("state", "FA-10", "In Progress"))

    def test_automated_closure_rejects_an_obsolete_request(self):
        self.issue["description"] = self.issue["description"].replace("01:00:00", "01:02:30")
        self.assertEqual(self.check_automated_closure(APPROVAL), (True, "running"))

    def test_manual_owner_closure_retains_priority(self):
        self.assertEqual(self.check_automated_closure("历史记录里的无关消息", manual=True),
                         (False, "stopped"))
        self.command.assert_not_called()
