#!/usr/bin/env python3
"""Compare the decoded video samples and exact presentation times."""
import hashlib
import json
from fractions import Fraction
from pathlib import Path
import subprocess
import sys


def ffprobe(path):
    raw = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_frames", "-show_streams",
        "-show_entries", "stream=codec_type,width,height,pix_fmt,time_base,duration_ts:"
        "frame=best_effort_timestamp",
        "-of", "json", str(path),
    ])
    data = json.loads(raw)
    if len(data["streams"]) != 1 or data["streams"][0]["codec_type"] != "video":
        raise ValueError("expected one video stream")
    stream = data["streams"][0]
    time_base = Fraction(stream["time_base"])
    pts = [int(frame["best_effort_timestamp"]) * time_base for frame in data["frames"]]
    return stream, pts, int(stream["duration_ts"]) * time_base


def frame_hashes(path, pixel_format):
    raw = subprocess.check_output([
        "ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-map", "0:v:0",
        "-pix_fmt", pixel_format, "-c:v", "rawvideo", "-threads:v", "1",
        "-fps_mode", "passthrough", "-f", "framehash", "-hash", "sha256", "-",
    ], text=True)
    return [line.rsplit(",", 1)[-1].strip()
            for line in raw.splitlines() if line and not line.startswith("#")]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def compare(scenario, candidate):
    source = Path(scenario["reference"])
    source_sha = digest(source)
    if source_sha != scenario["sha256"]:
        raise ValueError("reference hash mismatch")
    reference, reference_pts, reference_duration = ffprobe(source)
    output, output_pts, output_duration = ffprobe(candidate)
    fields = ("width", "height", "pix_fmt")
    same_format = all(reference[field] == output[field] for field in fields)
    exact_frames = 0
    if same_format:
        reference_hashes = frame_hashes(source, reference["pix_fmt"])
        output_hashes = frame_hashes(candidate, reference["pix_fmt"])
        if len(reference_hashes) != len(reference_pts) or len(output_hashes) != len(output_pts):
            raise ValueError("decoded frame count does not match probed frame count")
        exact_frames = sum(a == b for a, b in zip(reference_hashes, output_hashes))
    exact_timing = reference_pts == output_pts and reference_duration == output_duration
    return dict(reference_sha256=source_sha, candidate_sha256=digest(candidate),
                frames=len(reference_pts), exact_frames=exact_frames,
                exact_format=same_format, exact_timing=exact_timing,
                score=exact_frames / len(reference_pts),
                pixel_time_pass=same_format and exact_timing and exact_frames == len(reference_pts))


if __name__ == "__main__":
    scenario = json.loads(Path(sys.argv[1]).read_text())
    result = compare(scenario, Path(sys.argv[2]))
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["pixel_time_pass"] else 1)
