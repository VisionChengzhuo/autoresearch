"""Exercise the staged Linear adapter's write parsers without network access."""
import runpy
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

if len(sys.argv) != 2:
    raise SystemExit("usage: python3 tests/check_interfaces.py /path/to/staged/linear-app")
adapter = runpy.run_path(sys.argv[1])
namespace = adapter["main"].__globals__
card = {"id": "fake", "identifier": "FA-1", "state": {"name": "In Progress"},
        "description": "", "title": "假课题", "labels": {"nodes": [{"name": "长期"}]}}
with tempfile.TemporaryDirectory() as tmp:
    payload = Path(tmp) / "progress.md"
    payload.write_text("## 现在到哪了\n值为 0.42。\n\n## 下一步\n重复三次。\n")
    with patch.dict(namespace, {"D": tmp}), \
         patch.dict(namespace, {"issue": lambda _: card}), \
         patch("builtins.print"):
        from unittest.mock import Mock
        description = Mock()
        notify = Mock()
        update = Mock(return_value={"issueUpdate": {"success": True}})
        with patch.dict(namespace, {"write_description": description, "comment_once": notify,
                                    "update": update}):
            adapter["main"](["progress", "FA-1", str(payload)])
            assert "值为 0.42。" in description.call_args.args[-1]["progress"]
            assert "重复三次。" in description.call_args.args[-1]["next"]
            adapter["main"](["ask", "FA-1", "三次留出集评测达到阈值。", "--accept"])
            update.assert_not_called()
            record = adapter["read_record"](card)
            assert record["status"] == "open" and record["requested_at"]
            assert "三次留出集评测达到阈值。" in description.call_args.args[-1]["accepted"]
            notify.assert_not_called()
            update.reset_mock()
            for state in ("Done", "Canceled"):
                try:
                    adapter["main"](["state", "FA-1", state])
                except adapter["LinearError"]:
                    pass
                else:
                    raise AssertionError(f"unauthorized state {state} was accepted")
            update.assert_not_called()
print("linear-app progress, persisted acceptance, and required long-card closing evidence verified offline")
