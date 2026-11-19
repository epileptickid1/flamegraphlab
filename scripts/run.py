"""Repeat native Windows experiments; all saved performance runs are unprofiled."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time
import winreg

ROOT = Path(__file__).resolve().parents[1]

def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def machine():
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
        cpu = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    return {"os": platform.platform(), "cpu": cpu, "logical_processors": os.cpu_count(),
            "python": platform.python_version(), "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

def command(config, args):
    return [str(ROOT / "build" / config / "benchmark.exe"), "--n", str(args.n),
            "--repeats", str(args.repeats), "--batches", str(args.batches),
            "--mode", args.mode, "--threads", str(args.threads)]

def execute(config, args, index, warmup):
    t0 = time.perf_counter()
    completed = subprocess.run(command(config, args), capture_output=True, text=True, check=True, timeout=600)
    wall = time.perf_counter() - t0
    result = json.loads(completed.stdout)
    if result["verified"] is not True:
        raise RuntimeError("benchmark did not verify its output")
    result.update(config=config, run=index, warmup=warmup, process_wall_s=wall)
    print(f"{config} {'warmup' if warmup else 'run '+str(index)}: "
          f"workload={result['workload_s']:.6f}s, generation={result['generation_s']:.6f}s, "
          f"search={result['search_s']:.6f}s", flush=True)
    return result

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n", type=int, default=10_000_000)
    p.add_argument("--repeats", type=int, default=100)
    p.add_argument("--batches", type=int, default=40)
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--mode", choices=["sequential", "parallel"], default="sequential")
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--output", type=Path, default=ROOT / "results" / "measurements.json")
    args = p.parse_args()
    if min(args.n, args.repeats, args.batches, args.runs, args.threads) <= 0:
        p.error("counts must be positive")
    records = []
    result = {"schema": 1, "machine": machine(), "method": "unprofiled; alternating Od/O2 order; one warmup each",
              "parameters": {k: getattr(args,k) for k in ("n","repeats","batches","runs","mode","threads")},
              "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in (ROOT/"src"/"benchmark.cpp", ROOT/"src"/"kernels.cpp", ROOT/"src"/"kernels.h")},
              "records": records}
    for config in ("Od","O2"):
        records.append(execute(config, args, 0, True))
        write_json(args.output, result)
    for index in range(1, args.runs + 1):
        order = ("Od","O2") if index % 2 else ("O2","Od")
        for config in order:
            records.append(execute(config, args, index, False))
            write_json(args.output, result)
    fingerprints = {(r["minimum"], r["checksum"], r["input_sum"], r["n"],r["repeats"],r["batches"]) for r in records}
    if len(fingerprints) != 1:
        raise RuntimeError("input/result mismatch between runs")
    metrics = ("workload_s", "generation_s", "search_s", "process_wall_s", "user_s", "kernel_s",
               "process_cycles", "page_faults", "peak_working_set_bytes", "private_bytes")
    summary = {}
    for config in ("Od","O2"):
        selected = [r for r in records if r["config"] == config and not r["warmup"]]
        summary[config] = {
            key: {"median": statistics.median([r[key] for r in selected]),
                  "min": min(r[key] for r in selected), "max": max(r[key] for r in selected)}
            for key in metrics}
    summary["workload_speedup"] = summary["Od"]["workload_s"]["median"] / summary["O2"]["workload_s"]["median"]
    summary["all_results_match"] = True
    result["summary"] = summary
    write_json(args.output, result)
    print(f"Speedup (median workload): {summary['workload_speedup']:.3f}x\nSaved: {args.output}", flush=True)

if __name__ == "__main__":
    main()
