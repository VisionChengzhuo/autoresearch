#!/usr/bin/env python3
"""Run the two independent acceptance reviews on local verification evidence."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from compare import compare, digest


ROOT = Path(__file__).resolve().parent
SCENARIO = json.loads((ROOT / "scenario.json").read_text())
FABLE = "claude-fable-5-1"
ASTRA = "gpt-6.2-astra"
RELAY_HOST = os.environ.get("AR_RELAY_HOST", "localhost")
RELAY_CMD = os.environ.get("AR_RELAY_CMD", "claude-full")
COMMANDS = {
    FABLE: [
        "ssh", "-T", "-o", "BatchMode=yes", RELAY_HOST,
        f"bash {RELAY_CMD} -p --model claude-fable-5-1 "
        '--effort high --output-format json --tools "" --no-session-persistence',
    ],
    ASTRA: [
        "codex", "exec", "-m", ASTRA, "--sandbox", "read-only",
        "--skip-git-repo-check", "-o", "verdict.json", "-",
    ],
}


def review(model, candidate, provenance):
    # Each reviewer gets a fresh execution of the frozen comparator and no peer result.
    evidence = compare(SCENARIO, candidate)
    if not evidence["pixel_time_pass"] or evidence["candidate_sha256"] == evidence["reference_sha256"]:
        raise ValueError("candidate failed the local comparison or is the original file")
    prompt = (
        "你是纯代码视频重建的独立最终验收者。独立审核下面的本地检查报告和纯文字代码／"
        "复跑证据，要求代码不使用深度学习、源片回放或等价逐帧数据，且无源片、离线复跑"
        "可生成同一候选。必须 150 帧像素和显示时序完全一致。缺证据则 FAIL。"
        "不得读取、上传真人媒体或调用工具。请只输出 JSON，包含 verdict（PASS 或 FAIL）、"
        "candidate_sha256（来自报告）及 reason。不得引用另一验收者。"
        "\n\n本地检查报告\n" + json.dumps(evidence, ensure_ascii=False)
        + "\n\n纯文字审计材料\n" + provenance)
    with tempfile.TemporaryDirectory(prefix="video-reconstruction-review-") as folder:
        process = subprocess.run(COMMANDS[model], input=prompt, text=True, cwd=folder,
                                 capture_output=True, check=True, timeout=1800)
        if model == FABLE:
            response = json.loads(process.stdout)
            if response.get("is_error") or FABLE not in response.get("modelUsage", {}):
                raise ValueError("no valid Fable 5.1 receipt")
            answer = response["result"]
        else:
            answer = (Path(folder) / "verdict.json").read_text()
    result = json.loads(answer.strip().removeprefix("```json").strip().removesuffix("```").strip())
    if result["candidate_sha256"] != evidence["candidate_sha256"]:
        raise ValueError(f"{model} judged a different candidate")
    if result["verdict"] not in {"PASS", "FAIL"} or not result["reason"].strip():
        raise ValueError(f"{model} returned an invalid verdict")
    return result


def verify(candidate, provenance_path, report):
    provenance = provenance_path.read_text()
    if len(provenance) > 100_000 or "\x00" in provenance:
        raise ValueError("audit evidence must be a concise plain-text record, with no media encodings")
    candidate_hash = digest(candidate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {model: pool.submit(review, model, candidate, provenance) for model in COMMANDS}
        reviews = {model: job.result() for model, job in jobs.items()}
    if digest(candidate) != candidate_hash or any(
        item["candidate_sha256"] != candidate_hash for item in reviews.values()
    ):
        raise ValueError("candidate changed during review")
    result = {"candidate": str(candidate), "candidate_sha256": candidate_hash,
              "reference_sha256": SCENARIO["sha256"], "reviews": reviews}
    with tempfile.NamedTemporaryFile("w", dir=report.parent, delete=False) as temporary:
        json.dump(result, temporary, indent=2, ensure_ascii=False)
        temp_path = Path(temporary.name)
    temp_path.replace(report)


def check(candidate, report):
    result = json.loads(report.read_text())
    evidence = compare(SCENARIO, candidate)
    candidate_hash = evidence["candidate_sha256"]
    if (result["candidate_sha256"] != candidate_hash or
            result["reference_sha256"] != evidence["reference_sha256"] or
            candidate_hash == evidence["reference_sha256"] or not evidence["pixel_time_pass"]):
        raise ValueError("dual-review report does not match a locally passing candidate")
    if any(result["reviews"][model]["verdict"] != "PASS" or
           result["reviews"][model]["candidate_sha256"] != candidate_hash for model in COMMANDS):
        raise ValueError("both reviewers must pass the same candidate")


if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    paths = [Path(arg).resolve() for arg in args]
    if mode == "verify":
        verify(*paths)
    elif mode == "check":
        check(*paths)
    else:
        raise ValueError("expected verify or check")
