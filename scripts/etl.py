#!/usr/bin/env python3
"""Build per-process ETW CPU flamegraphs from xperf's symbolized dumper output."""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
PROFILE_SPEC = importlib.util.spec_from_file_location("local_profile", SCRIPT_DIR / "profile.py")
if PROFILE_SPEC is None or PROFILE_SPEC.loader is None:
    raise RuntimeError("Cannot import the local flamegraph renderer")
PROFILE = importlib.util.module_from_spec(PROFILE_SPEC)
PROFILE_SPEC.loader.exec_module(PROFILE)

PROCESS_RE = re.compile(r"benchmark\.exe\s*\(\s*(\d+)\s*\)", re.IGNORECASE)
WEIGHT_RE = re.compile(r",\s*(\d+)\s*,\s*([^,\s]+)\s*$")
TRACE_START_RE = re.compile(r"Trace Start:\s*(\d+)")
LOSS_RE = re.compile(r"Events Lost:\s*(\d+)\s*,\s*Buffers lost:\s*(\d+)")


def int_field(value: str, label: str) -> int:
    try:
        return int(value.strip(), 0) if value.strip().lower().startswith("0x") else int(value.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid {label}: {value!r}") from exc


def trace_metadata(dump: Path):
    trace_start_filetime = None
    lost_events = lost_buffers = None
    header = ""
    with dump.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for _ in range(300):
            line = stream.readline()
            if not line:
                break
            if "Trace Start:" in line:
                header = line.strip()
                match = TRACE_START_RE.search(line)
                if match:
                    trace_start_filetime = int(match.group(1))
                loss = LOSS_RE.search(line)
                if loss:
                    lost_events, lost_buffers = map(int, loss.groups())
                break
    if trace_start_filetime is None:
        raise ValueError(f"No Trace Start FILETIME found in {dump}")
    # xperf dumper event timestamps are elapsed microseconds from the trace start.
    trace_start_unix_us = (trace_start_filetime - 116444736000000000) // 10
    return trace_start_filetime, trace_start_unix_us, lost_events, lost_buffers, header


def find_benchmark_process(dump: Path, mode: str, child: dict):
    candidates = []
    pids = {}
    ends = {}
    expected = (
        f"--n {child.get('n')} --repeats {child.get('repeats')} "
        f"--batches {child.get('batches')} --mode {child.get('mode')}"
    )
    with dump.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            stripped = line.lstrip()
            if stripped.startswith("P-Start,") and "benchmark.exe" in line.lower():
                match = PROCESS_RE.search(line)
                if not match:
                    continue
                pid = int(match.group(1))
                expected_image = f"build\\{mode.lower()}\\benchmark.exe"
                lower = line.lower().replace("/", "\\")
                if expected_image in lower and expected.lower() in lower:
                    candidates.append((pid, int_field(line.split(",", 3)[1], "process start timestamp"), line.strip()))
            elif stripped.startswith("P-End,") and "benchmark.exe" in line.lower():
                match = PROCESS_RE.search(line)
                if match:
                    pids[int(match.group(1))] = int_field(line.split(",", 2)[1], "process end timestamp")
    if len(candidates) != 1:
        raise ValueError(f"Expected one {mode} benchmark P-Start record matching child.json; found {len(candidates)}")
    pid, pstart_ts, command = candidates[0]
    return pid, pstart_ts, pids.get(pid), command


def parse_profile_event(line: str):
    # The first six comma-delimited fields are fixed; function names later may contain commas.
    fields = line.split(",", 6)
    if len(fields) > 1 and fields[1].strip() == "TimeStamp":
        return None
    if len(fields) < 6:
        raise ValueError("Malformed SampledProfile row")
    timestamp = int_field(fields[1], "profile timestamp")
    process = fields[2]
    process_match = PROCESS_RE.search(process)
    if not process_match:
        return None
    pid = int(process_match.group(1))
    tid = int_field(fields[3], "profile thread id")
    # Most sampled rows are Unbatched with count=1. Parse the final count/type pair,
    # even when a symbol's C++ template name itself contains commas.
    weight_match = WEIGHT_RE.search(line.rstrip())
    weight = int(weight_match.group(1)) if weight_match else 1
    sample_type = weight_match.group(2) if weight_match else "unreported"
    if weight < 1:
        weight = 1
    return {"timestamp": timestamp, "pid": pid, "tid": tid, "weight": weight, "sample_type": sample_type}


def parse_stack_row(line: str):
    fields = line.split(",", 5)
    if len(fields) < 6:
        return None
    try:
        return int_field(fields[1], "stack timestamp"), int_field(fields[2], "stack thread id"), int_field(fields[3], "stack frame number"), fields[5].strip()
    except ValueError:
        return None


def find_decode_warnings(path: Path):
    if not path.exists():
        return None
    warning_count = invalid_count = 0
    with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            if "Warning:" in line:
                warning_count += 1
                if "Invalid Event" in line:
                    invalid_count += 1
    return {"path": str(path), "warning_lines": warning_count, "invalid_event_warnings": invalid_count}


def weighted_add(root, frames, weight):
    node = root
    node["count"] += weight
    for frame in frames:
        node = node["children"].setdefault(frame, {"count": 0, "children": {}})
        node["count"] += weight


def analyze_mode(trace_dir: Path, mode: str, result_root: Path, graphs_dir: Path):
    dump = trace_dir / f"{mode}-dump.txt"
    child_path = trace_dir / f"{mode}-child.json"
    warnings_path = trace_dir / f"{mode}-decode-warnings.txt"
    child = json.loads(child_path.read_text(encoding="utf-8-sig"))
    if child.get("verified") is not True or child.get("mode") != "sequential":
        raise ValueError(f"{child_path} does not describe a verified sequential benchmark")
    start_us, end_us = child.get("start_unix_us"), child.get("end_unix_us")
    if not isinstance(start_us, int) or not isinstance(end_us, int) or end_us <= start_us:
        raise ValueError(f"Invalid measured workload interval in {child_path}")

    trace_filetime, trace_unix_us, lost_events, lost_buffers, header = trace_metadata(dump)
    pid, process_start_ts, process_end_ts, command = find_benchmark_process(dump, mode, child)
    if process_end_ts is not None and process_start_ts > process_end_ts:
        raise ValueError(f"Process timestamps reversed for PID {pid}")

    counts = Counter()
    folded = Counter()
    inclusive = Counter()
    self_counts = Counter()
    pending = None
    target_pid = pid
    profile_total_weight = 0

    def finish(event):
        nonlocal profile_total_weight
        if event is None:
            return
        absolute_us = trace_unix_us + event["timestamp"]
        if not start_us <= absolute_us <= end_us:
            counts["target_profile_outside_workload"] += 1
            return
        counts["target_profile_in_workload"] += 1
        weight = event["weight"]
        profile_total_weight += weight
        frames_numbered = sorted(event["frames"], key=lambda pair: pair[0])
        frames_leaf_first = [frame for _, frame in frames_numbered if frame]
        if not frames_leaf_first:
            counts["samples_missing_stack"] += 1
            return
        if len(frames_numbered) != len(frames_leaf_first):
            counts["samples_missing_frame_symbols"] += 1
        # A complete process stack should reach the benchmark image. Keep observed
        # frames even when it does not, and report the likely truncation separately.
        if not any(frame.lower().startswith("benchmark.exe!") for frame in frames_leaf_first):
            counts["samples_without_benchmark_image_frame"] += 1
        frame_numbers = [number for number, frame in frames_numbered if frame]
        if frame_numbers and frame_numbers != list(range(1, max(frame_numbers) + 1)):
            counts["samples_with_frame_number_gaps"] += 1
        if any(re.search(r"!0x[0-9a-f]+$", frame, re.IGNORECASE) for frame in frames_leaf_first):
            counts["samples_with_unresolved_symbols"] += 1
        # ETW No. 1 is the sampled leaf; folded stacks conventionally go root to leaf.
        stack = [PROFILE.safe_frame(frame) for frame in reversed(frames_leaf_first)]
        folded[";".join(stack)] += weight
        for name in set(stack):
            inclusive[name] += weight
        self_counts[stack[-1]] += weight
        counts["samples_with_stack"] += 1
        counts["folded_sample_weight"] += weight

    with dump.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            stripped = line.lstrip()
            if stripped.startswith("SampledProfile,"):
                finish(pending)
                pending = None
                parsed = parse_profile_event(line)
                if parsed is not None and parsed["pid"] == target_pid:
                    counts["target_profile_events"] += 1
                    pending = {**parsed, "frames": []}
            elif stripped.startswith("Stack,"):
                if pending is not None:
                    row = parse_stack_row(line)
                    if row is not None and row[0] == pending["timestamp"] and row[1] == pending["tid"]:
                        pending["frames"].append((row[2], row[3]))
                    else:
                        finish(pending)
                        pending = None
            elif pending is not None and stripped:
                finish(pending)
                pending = None
    finish(pending)
    if counts["target_profile_events"] == 0:
        raise ValueError(f"No SampledProfile events found for PID {pid} in {dump}")
    if counts["target_profile_in_workload"] == 0:
        raise ValueError(f"No target CPU samples aligned with {child_path}'s workload interval")
    if counts["samples_with_stack"] == 0:
        raise ValueError(f"All in-window ETW profile events were missing stack rows for PID {pid}")

    result_root.mkdir(parents=True, exist_ok=True)
    graphs_dir.mkdir(parents=True, exist_ok=True)
    folded_path = result_root / f"{mode}.folded"
    top_path = result_root / f"{mode}.top.txt"
    summary_path = result_root / f"{mode}.summary.json"
    svg_path = graphs_dir / f"{mode}.svg"
    folded_path.write_text("".join(f"{stack} {weight}\n" for stack, weight in sorted(folded.items())), encoding="utf-8")
    root = {"count": 0, "children": {}}
    for stack, weight in folded.items():
        weighted_add(root, stack.split(";"), weight)
    PROFILE.render_svg(root, svg_path)
    svg = svg_path.read_text(encoding="utf-8")
    svg = svg.replace(
        f"Flamegraph | {root['count']} workload-window samples | search highlights matching frames",
        f"ETW CPU flamegraph | {root['count']} weighted workload-window samples | search highlights matching frames",
    )
    svg_path.write_text(svg, encoding="utf-8")

    top_lines = [
        f"ETW CPU stack summary ({mode})",
        "Counts are weighted by the xperf SampledProfile Count field; folded paths preserve observed Stack No. ordering.",
        f"Benchmark PID: {pid}",
        f"Measured workload window (UTC Unix microseconds): {start_us} .. {end_us}",
        "",
        "inclusive  self  percent  function",
    ]
    total = counts["folded_sample_weight"]
    for name, value in sorted(inclusive.items(), key=lambda item: (-item[1], item[0]))[:40]:
        pct = 100.0 * value / total if total else 0.0
        top_lines.append(f"{value:9d} {self_counts[name]:5d} {pct:7.2f}%  {name}")
    top_path.write_text("\n".join(top_lines) + "\n", encoding="utf-8")

    warning_info = find_decode_warnings(warnings_path)
    summary = {
        "source": {"dump": str(dump), "child_json": str(child_path), "decode_warnings": warning_info},
        "mode": mode,
        "pid": pid,
        "process_command_line": command,
        "process_start_timestamp_us_since_trace_start": process_start_ts,
        "process_end_timestamp_us_since_trace_start": process_end_ts,
        "trace": {
            "trace_start_filetime_100ns": trace_filetime,
            "trace_start_unix_us": trace_unix_us,
            "profile_timestamp_unit": "microseconds elapsed since trace start (aligned against child UTC timestamps)",
            "events_lost": lost_events,
            "buffers_lost": lost_buffers,
            "header": header,
        },
        "workload_window": {"start_unix_us": start_us, "end_unix_us": end_us, "duration_us": end_us-start_us},
        "alignment": {
            "process_start_to_workload_start_us": start_us - (trace_unix_us + process_start_ts),
            "workload_end_to_process_end_us": (trace_unix_us + process_end_ts - end_us) if process_end_ts is not None else None,
        },
        "counts": dict(counts),
        "notes": [
            "Only SampledProfile events with the benchmark process PID were considered.",
            "Only samples whose trace-start-relative timestamp maps into child.json's measured workload window were folded.",
            "Stack frames are ordered using ETW Stack No. values and emitted root-to-leaf; no frames were inferred.",
            "Stacks without a benchmark image frame are retained; these can be runtime helper threads or partial stacks, not necessarily truncation.",
        ],
        "outputs": {"folded": str(folded_path), "top": str(top_path), "svg": str(svg_path)},
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"{mode}: PID {pid}, {counts['target_profile_in_workload']} workload-window ETW profile events, "
          f"{counts['samples_with_stack']} stacks, {counts['folded_sample_weight']} folded sample weight")
    return summary_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", type=Path, default=ROOT / "results" / "wpr-20261117-114145",
                        help="directory containing MODE-dump.txt and MODE-child.json")
    parser.add_argument("--results-dir", type=Path, default=ROOT / "results" / "profiles")
    parser.add_argument("--graphs-dir", type=Path, default=ROOT / "graphs")
    args = parser.parse_args()
    for mode in ("Od", "O2"):
        analyze_mode(args.trace_dir, mode, args.results_dir, args.graphs_dir)


if __name__ == "__main__":
    main()
