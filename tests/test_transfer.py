import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ar import load, save


SCRIPT = Path(__file__).resolve().parents[1] / "ar-transfer"
spec = importlib.util.spec_from_loader("transport", importlib.machinery.SourceFileLoader("transport", str(SCRIPT)))
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


class TransferTest(unittest.TestCase):
    def test_retry_preserves_old_files_and_publishes_terminal_heartbeat_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source" / "runs"
            remote = source / "campaign" / "test"
            remote.mkdir(parents=True)
            local = base / "local"
            status = base / "keeper.json"
            pod = {"host": "h200", "pod": "pod", "alerts": [], "daemon": {"state": "running", "alarm": None},
                   "gpus": [None] * 8, "shm_percent": 4, "ram_percent": 10, "shm_used_bytes": 5}
            save(status, {"state": "checked", "checked_at": 100, "alerts": [],
                          "records": {"record": {"pods": {"launcher": pod, "worker": pod}}}})
            save(remote / "heartbeat.json", {"alive": True})
            (remote / "run.log").write_text("started\n")
            (remote / "process.json").write_text("must not be transferred")
            (remote / "link").symlink_to(remote / "process.json")
            sync_files = transport.sync_files

            def local_sync(src, dest, filters=()):
                sync_files(str(src).removeprefix("test:"), str(dest).removeprefix("test:"), filters)

            with patch.object(transport, "KEEPER", str(status)), patch.object(transport, "sync_files", local_sync):
                transport.sync("test", str(source), local)
                published = local / "runs" / "campaign" / "test"
                original = load(local / "status.json")
                save(remote / "heartbeat.json", {"alive": False})
                save(remote / "result.json", {"score": 0.42})
                (remote / "run.log").write_text("started\nfinished\n")
                with patch.object(transport, "sync_files", side_effect=subprocess.CalledProcessError(255, "rsync")):
                    with self.assertRaises(subprocess.CalledProcessError):
                        transport.sync("test", str(source), local)
                self.assertEqual(load(local / "status.json"), original)
                self.assertTrue(load(published / "heartbeat.json")["alive"])
                publish = transport.publish

                def check_order(src, dest):
                    if dest.name == "heartbeat.json":
                        self.assertEqual(load(published / "result.json")["score"], .42)
                        self.assertTrue((published / "run.log").read_text().endswith("finished\n"))
                    publish(src, dest)

                with patch.object(transport, "publish", check_order):
                    transport.sync("test", str(source), local)
                self.assertFalse(load(published / "heartbeat.json")["alive"])
                self.assertFalse((published / "process.json").exists())
                self.assertFalse((published / "link").exists())
                self.assertFalse(load(local / "resources" / "launcher.json")["alarm"])
