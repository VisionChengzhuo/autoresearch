#!/usr/bin/env python3
"""Process-boundary test doubles. Writes only inside AR_TEST_DIR."""
import json
import os
import sys
from pathlib import Path

base = Path(os.environ["AR_TEST_DIR"])
tool = Path(sys.argv[0]).name
cmd, *args = sys.argv[1:]
with (base / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps([tool, cmd, *args]) + "\n")
if tool == "amb":
    jobs = base / "jobs"
    if cmd == "new":
        ident = f"J-0000-{len(list(jobs.glob('J-*'))) + len(list((jobs / '_draft').glob('J-*'))) + 1}"
        draft = jobs / "_draft" / ident
        draft.mkdir(parents=True)
        job = {"id": ident, "args": args}
        for flag in ("campaign", "exec"):
            if "--" + flag in args:
                job[flag] = args[args.index("--" + flag) + 1]
        (draft / "job.json").write_text(json.dumps(job))
        print(ident)
    elif cmd == "submit":
        ident = args[0]
        draft = jobs / "_draft" / ident
        draft.rename(jobs / ident)
        (jobs / ident / "state.json").write_text('{"state":"running"}')
        print(ident)
    elif cmd == "align":
        if (base / "align-fail").exists():
            raise SystemExit("align unavailable")
        with (base / "aligned.jsonl").open("a") as stream:
            stream.write(json.dumps({"text": Path(args[0]).read_text(), "args": args[1:]}) + "\n")
        print("queued")
    else:
        raise SystemExit("unsupported fake amb command")
else:
    file = base / "issue.json"
    issue = json.loads(file.read_text())
    if cmd == "gql":
        if "issues(" in args[0]:
            question = base / "question.json"
            print(json.dumps({"issues": {"nodes": [json.loads(question.read_text())] if question.exists() else []}}))
            raise SystemExit(0)
        if json.loads(args[1]).get("id") == "FA-99":
            issue = json.loads((base / "question.json").read_text())
        if "history(" in args[0]:
            issue = {"history": {"nodes": [issue["transition"]],
                                 "pageInfo": {"hasNextPage": False}}}
        print(json.dumps({"issue": issue}))
    elif cmd == "state":
        assert args == ["FA-1", "In Progress"], args
        issue["state"] = {"id": "In Progress", "name": "In Progress"}
        file.write_text(json.dumps(issue))
    elif cmd == "progress":
        assert args[0] == "FA-1"
        payload = Path(args[1]).read_text()
        assert payload.startswith("## 现在到哪了\n") and "\n## 下一步\n" in payload
        (base / "progress.md").write_text(payload)
    elif cmd == "new":
        assert args[0].startswith("[AR fake/") and args[-2:] == ["--project", "Fake project"]
        (base / "question.json").write_text(json.dumps({"identifier": "FA-99", "state": {"name": "In Progress"}}))
        print("FA-99 https://example.invalid/fake")
    elif cmd == "ask" and args[0] == "FA-99":
        (base / "question.json").write_text(json.dumps({"identifier": "FA-99", "state": {"name": "Human Intervention"}}))
    elif cmd == "ask":
        assert args[0] == "FA-1"
        if args[-1] != "--accept":
            raise SystemExit(0)
        value = "2026-01-01T00:00:00+00:00"
        issue["description"] = f"## 验收申请\n申请时间：{value}\n\n{args[1]}"
        file.write_text(json.dumps(issue))
        import hashlib
        records = base / "records"
        records.mkdir(exist_ok=True)
        key = hashlib.sha256(issue["id"].encode()).hexdigest()
        (records / (key + ".json")).write_text(json.dumps({"status": "open", "requested_at": value}))
    elif cmd == "comment":
        assert args[0] == "FA-1"
    else:
        raise SystemExit("unsupported fake linear-app command")
