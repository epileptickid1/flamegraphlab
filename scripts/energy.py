#!/usr/bin/env python3
"""Integrate normalized power samples over benchmark intervals.

Input format is documented in ENERGY.md. Uses only the Python standard library.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, NoReturn


def fail(message: str) -> NoReturn:
    raise ValueError(message)


def finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        fail(f"{label} must be a finite number")
    return number


def read_json(path: str) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8-sig") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read JSON {path!r}: {exc}")


def parse_series(obj: Any) -> tuple[str, str, str, list[tuple[int, float]]]:
    if not isinstance(obj, dict):
        fail("series JSON must be an object")
    if obj.get("schema_version") != 1:
        fail("series schema_version must be 1")
    scope = obj.get("scope")
    if scope not in ("system", "cpu_package"):
        fail("series scope must be 'system' or 'cpu_package'")
    source, sensor = obj.get("source"), obj.get("sensor_name")
    if not isinstance(source, str) or not source.strip():
        fail("series source must be a non-empty string")
    if not isinstance(sensor, str) or not sensor.strip():
        fail("series sensor_name must be a non-empty string")
    rows = obj.get("samples")
    if not isinstance(rows, list) or len(rows) < 2:
        fail("series samples must contain at least two rows")
    samples: list[tuple[int, float]] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            fail(f"sample {i} must be an object")
        ts = row.get("timestamp_unix_us")
        if isinstance(ts, bool) or not isinstance(ts, int):
            fail(f"sample {i} timestamp_unix_us must be an integer")
        power = finite_number(row.get("power_w"), f"sample {i} power_w")
        if power < 0:
            fail(f"sample {i} power_w must be non-negative")
        if samples and ts <= samples[-1][0]:
            fail("sample timestamps must be strictly increasing")
        samples.append((ts, power))
    return scope, source, sensor, samples


def require_int(obj: Any, key: str, label: str) -> int:
    value = obj.get(key) if isinstance(obj, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"{label}.{key} must be an integer")
    return value


def interp(samples: list[tuple[int, float]], t: int, hint: int) -> float:
    t0, p0 = samples[hint]
    t1, p1 = samples[hint + 1]
    return p0 + (t - t0) * (p1 - p0) / (t1 - t0)


def integrate(samples: list[tuple[int, float]], start: int, end: int,
              max_gap_us: int, label: str) -> float:
    if start >= end:
        fail(f"{label} start must be before end")
    if samples[0][0] > start or samples[-1][0] < end:
        fail(f"sensor series does not cover the full {label} interval")
    # Validate raw neighboring samples, not only the clipped integration
    # segments: a very short requested interval can still sit inside a large
    # telemetry gap and must not look adequately sampled after interpolation.
    for (t0, _), (t1, _) in zip(samples, samples[1:]):
        if t0 < end and t1 > start and t1 - t0 > max_gap_us:
            fail(f"sensor gap of {(t1 - t0) / 1e6:.3f}s exceeds --max-gap-s in {label}")
    i = 0
    while i + 1 < len(samples) and samples[i + 1][0] <= start:
        i += 1
    points = [(start, interp(samples, start, i))]
    while i + 1 < len(samples) and samples[i + 1][0] < end:
        i += 1
        points.append(samples[i])
    if points[-1][0] != end:
        points.append((end, interp(samples, end, i)))
    area_w_us = 0.0
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        gap = t1 - t0
        if gap <= 0:
            fail(f"invalid sample interval in {label}")
        if gap > max_gap_us:
            fail(f"sensor gap of {gap / 1e6:.3f}s exceeds --max-gap-s in {label}")
        area_w_us += (p0 + p1) * 0.5 * gap
    return area_w_us / 1_000_000.0


def analyze(run_path: str, series_path: str, baseline_start: int,
            baseline_end: int, max_gap_s: float) -> dict[str, Any]:
    run = read_json(run_path)
    run_start = require_int(run, "start_unix_us", "run")
    run_end = require_int(run, "end_unix_us", "run")
    if run_start >= run_end:
        fail("run start_unix_us must be before end_unix_us")
    if baseline_end > run_start:
        fail("baseline window must end at or before the benchmark start")
    max_gap_s = finite_number(max_gap_s, "--max-gap-s")
    max_gap_us = round(max_gap_s * 1_000_000)
    if max_gap_us <= 0:
        fail("--max-gap-s must be positive")
    series_obj = read_json(series_path)
    scope, source, sensor, samples = parse_series(series_obj)
    run_j = integrate(samples, run_start, run_end, max_gap_us, "run")
    base_j = integrate(samples, baseline_start, baseline_end, max_gap_us, "baseline")
    run_s = (run_end - run_start) / 1_000_000.0
    baseline_s = (baseline_end - baseline_start) / 1_000_000.0
    base_w = base_j / baseline_s
    workload_keys = ("n", "repeats", "batches", "mode", "threads", "seed")
    workload = {key: run[key] for key in workload_keys if key in run}
    return {
        "schema_version": 1,
        "measurement_status": "measured_sensor_series_estimate",
        "run_file": str(Path(run_path)),
        "sensor_file": str(Path(series_path)),
        "source": source,
        "sensor_name": sensor,
        "scope": scope,
        "workload": workload,
        "run_start_unix_us": run_start,
        "run_end_unix_us": run_end,
        "duration_s": run_s,
        "baseline_start_unix_us": baseline_start,
        "baseline_end_unix_us": baseline_end,
        "baseline_mean_power_w": base_w,
        "sensor_energy_j": run_j,
        "baseline_energy_j": base_j,
        "incremental_energy_j_estimate": run_j - base_w * run_s,
        "method": "trapezoidal_sensor_power_integration_minus_baseline_mean_power_times_run_duration",
        "attribution": "incremental estimate for selected sensor scope; not exact per-process energy",
        "max_gap_s": max_gap_s,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="benchmark JSON containing start_unix_us/end_unix_us")
    parser.add_argument("--series", required=True, help="normalized power-series JSON")
    parser.add_argument("--baseline-start-unix-us", required=True, type=int)
    parser.add_argument("--baseline-end-unix-us", required=True, type=int)
    parser.add_argument("--max-gap-s", type=float, default=1.0,
                        help="reject intervals with larger gaps (default: 1.0s)")
    parser.add_argument("--output", help="write result JSON to this file; default is stdout")
    args = parser.parse_args()
    try:
        result = analyze(args.run, args.series, args.baseline_start_unix_us,
                         args.baseline_end_unix_us, args.max_gap_s)
        rendered = json.dumps(result, indent=2, allow_nan=False) + "\n"
        if args.output:
            Path(args.output).write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 0
    except (ValueError, OSError) as exc:
        print(f"energy.py: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
