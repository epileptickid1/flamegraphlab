"""Convert the HWiNFO CSV and analyze every recorded run in an energy session."""
import argparse
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import statistics

import energy

POWER_HEADERS = (
    "CPU Package Power [W]",
    "Повна споживана потужність ЦП [W]",
)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def read_sensor_series(path, utc_offset):
    offset = datetime.strptime(utc_offset, "%z").tzinfo
    # Some unrelated sensor labels in HWiNFO CSVs contain legacy-encoded bytes.
    with path.open(encoding="utf-8-sig", errors="surrogateescape", newline="") as stream:
        reader = csv.reader(stream)
        header = [field.strip() for field in next(reader, [])]
        if header[:2] != ["Date", "Time"]:
            raise ValueError("Expected HWiNFO Date and Time columns")
        indices = [index for index, title in enumerate(header) if title in POWER_HEADERS]
        if len(indices) != 1:
            raise ValueError("Expected exactly one CPU Package Power [W] column")
        power_column = indices[0]
        samples = []
        for row in reader:
            if not row or not any(row[:2]):
                continue
            if row[:2] == ["Date", "Time"]:
                if [field.strip() for field in row] != header:
                    raise ValueError(f"CSV column layout changed at row {reader.line_num}")
                continue
            try:
                date, time = row[:2]
                stamp = datetime.strptime(f"{date} {time}", "%d.%m.%Y %H:%M:%S.%f")
                timestamp = (stamp.replace(tzinfo=offset) - EPOCH) // timedelta(microseconds=1)
                power = float(row[power_column].replace(",", "."))
            except (ValueError, IndexError) as exc:
                raise ValueError(f"Could not read timestamp and power at CSV row {reader.line_num}") from exc
            samples.append({"timestamp_unix_us": timestamp, "power_w": power})
    series = {
        "schema_version": 1,
        "scope": "cpu_package",
        "source": "HWiNFO64",
        "sensor_name": header[power_column],
        "csv_file": path.name,
        "utc_offset": utc_offset,
        "samples": samples,
    }
    energy.parse_series(series)
    return series


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def analyze_session(directory, utc_offset, max_gap_s):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8-sig"))
    utc_offset = utc_offset or manifest.get("timestamp_utc_offset")
    if not utc_offset:
        raise ValueError("Provide --utc-offset for sessions without a saved offset, e.g. --utc-offset +02:00")
    sensor_path = directory / "hwinfo-sensors.json"
    series = read_sensor_series(directory / manifest["sensor_log"], utc_offset)
    write_json(sensor_path, series)
    results = []
    for record in manifest["records"]:
        run_path = directory / record["child_file"]
        run = json.loads(run_path.read_text(encoding="utf-8-sig"))
        if run.get("verified") is not True or any(
            run.get(key) != record.get(key) for key in ("start_unix_us", "end_unix_us")
        ):
            raise ValueError(f"Benchmark output and manifest disagree: {run_path.name}")
        result = energy.analyze(
            str(run_path), str(sensor_path), record["baseline_start_unix_us"],
            record["baseline_end_unix_us"], max_gap_s,
        )
        results.append({"config": record["config"], "run": record["run"], **result})
    if {row["config"] for row in results} != {"Od", "O2"} or not all(
        row["workload"] == results[0]["workload"] for row in results
    ):
        raise ValueError("Od and O2 must have matching workload parameters")
    expected_runs = list(range(1, manifest["parameters"]["runs"] + 1))
    for config in ("Od", "O2"):
        actual_runs = sorted(row["run"] for row in results if row["config"] == config)
        if actual_runs != expected_runs:
            raise ValueError(f"{config} runs are incomplete or duplicated")
    fields = ("duration_s", "sensor_energy_j", "baseline_mean_power_w", "incremental_energy_j_estimate")
    medians = {
        config: {
            field: statistics.median(row[field] for row in results if row["config"] == config)
            for field in fields
        }
        for config in ("Od", "O2")
    }
    output = directory / "energy-summary.json"
    write_json(output, {"scope": "cpu_package", "medians": medians, "runs": results})
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path, help="results/energy-... directory containing the capture")
    parser.add_argument("--utc-offset", help="Override the local UTC offset saved by the collector")
    parser.add_argument("--max-gap-s", type=float, default=1.0, help="Maximum allowed sample gap in seconds")
    args = parser.parse_args()
    try:
        print(analyze_session(args.session, args.utc_offset, args.max_gap_s))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Energy analysis: {exc}\n")


if __name__ == "__main__":
    main()
