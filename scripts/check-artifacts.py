"""Consistency checks for generated artifacts, without rerunning the workload."""
import hashlib
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]
checks=[]
measurements=json.loads((ROOT/"results/measurements.json").read_text(encoding="utf-8"))
for relative,expected in measurements["source_sha256"].items():
    if Path(relative).name in ("benchmark.cpp","kernels.cpp"):
        assert hashlib.sha256((ROOT/relative).read_bytes()).hexdigest()==expected,relative
    elif hashlib.sha256((ROOT/relative).read_bytes()).hexdigest()!=expected:
        checks.append(relative+": auxiliary profiler changed since timing run; not linked into measured EXE")
checks.append("Measured source file hashes match current source")
for mode in ("Od","O2"):
    summary=json.loads((ROOT/f"results/profiles/{mode}.summary.json").read_text(encoding="utf-8"))
    rows=(ROOT/f"results/profiles/{mode}.folded").read_text(encoding="utf-8").splitlines()
    total=sum(int(row.rsplit(" ",1)[1]) for row in rows)
    assert total==summary["counts"]["folded_sample_weight"]
    assert summary["counts"]["samples_with_stack"]==summary["counts"]["target_profile_in_workload"]
    assert summary["trace"]["events_lost"]==summary["trace"]["buffers_lost"]==0
    assert any("!make_data;" in row for row in rows)
    assert any("!sequential_min;" in row for row in rows)
    doc=ET.parse(ROOT/f"graphs/{mode}.svg")
    width,height=map(float,doc.getroot().attrib["viewBox"].split()[2:])
    for rect in doc.iter("{http://www.w3.org/2000/svg}rect"):
        x,y,w,h=(float(rect.attrib[k]) for k in ("x","y","width","height"))
        assert min(x,y,w,h)>=0
        assert x+w<=width+0.01
        assert y+70+h<=height+0.01
    checks.append(f"{mode}: SVG XML and bounds, folded weights, phase symbols, 0 lost ETW events")
pmc=json.loads((ROOT/"results/wpr-20261117-114145/pmc-summary.json").read_text(encoding="utf-8"))
for r in pmc["runs"]:
    assert r["all_target_slices_attributed"]
    assert r["quality"]["target_switch_out_events"]==r["quality"]["attributed_target_slices"]
    for name,value in r["lifetime_counts"].items():
        assert 0<r["workload_counts_lower_bound"][name]<=r["workload_counts_upper_bound"][name]<=value
    checks.append(f"{r['config']}: PMC slice continuity and bounds")
for filename in ("README.md","ENERGY.md"):
    contents=(ROOT/filename).read_text(encoding="utf-8")
    for link in re.findall(r"\]\(([^)]+)\)",contents):
        if not link.startswith(("http:","https:","#")):
            assert (ROOT/link.split("#")[0]).exists(),(filename,link)
    checks.append(filename+": local links exist")
energy_summaries = sorted((ROOT/"results").glob("energy-*/energy-summary.json"))
energy_status = "awaiting_actual_sensor_log"
for path in energy_summaries:
    summary = json.loads(path.read_text(encoding="utf-8"))
    assert summary["scope"] == "cpu_package"
    assert {run["config"] for run in summary["runs"]} == {"Od", "O2"}
    for run in summary["runs"]:
        assert run["duration_s"] > 0 and run["sensor_energy_j"] > 0
        assert math.isclose(run["incremental_energy_j_estimate"],
                            run["sensor_energy_j"] - run["baseline_mean_power_w"] * run["duration_s"])
    checks.append(f"{path.parent.name}: both CPU energy runs and baseline subtraction")
    energy_status = "cpu_package_measured_and_incremental_energy_estimated"
result={"passed":True,"checks":checks,"energy_measurement_status":energy_status,
        "visual_qa":"SVG XML/geometry checked; headless browser rendering unavailable in sandbox"}
(ROOT/"results/artifact-checks.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
print("\n".join(checks))
