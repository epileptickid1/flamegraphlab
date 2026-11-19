"""Attribute CSwitch PMC deltas to the outgoing thread, separately on each CPU.

The xperf Pmc row is attached to the immediately following CSwitch event.
It holds cumulative hardware-counter snapshots, not a per-process increment.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from collections import Counter
from etl import trace_metadata, find_benchmark_process, PROCESS_RE

NAMES=("BranchMispredictions","CacheMisses","InstructionRetired","TotalCycles")

def analyze(directory: Path, mode: str):
    dump=directory/f"{mode}-dump.txt"
    child=json.loads((directory/f"{mode}-child.json").read_text(encoding="utf-8-sig"))
    _, epoch, lost_events, lost_buffers, _=trace_metadata(dump)
    pid, _, _, _=find_benchmark_process(dump,mode,child)
    start=child["start_unix_us"]-epoch
    end=child["end_unix_us"]-epoch
    per_cpu={}
    life=Counter(); complete=Counter(); overlapping=Counter(); quality=Counter()
    complete_time=overlap_time=life_time=0
    pending=None
    statuses=[]
    header=False
    with dump.open(encoding="utf-8-sig",errors="replace") as stream:
        for line in stream:
            if line.strip()=="EndHeader":
                header=True
                continue
            if not header: continue
            fields=[x.strip() for x in line.split(",")]
            event=fields[0]
            if event=="PmcCorruptionStatus":
                quality["pmc_status_events"]+=1
                statuses.append({"cpu":int(fields[2]),"sources":[[int(v) for v in field.split()] for field in fields[4:]]})
            if event=="Pmc":
                if len(fields)!=7:
                    raise ValueError(f"Unexpected PMC columns: {line!r}")
                pending=(int(fields[1]),int(fields[2]),tuple(map(int,fields[3:])))
                continue
            if event!="CSwitch":
                pending=None
                continue
            stamp=int(fields[1]); new_tid=int(fields[3]); old_tid=int(fields[9]); cpu=int(fields[16])
            old_match=PROCESS_RE.search(fields[8])
            is_target=old_match is not None and int(old_match.group(1))==pid
            if is_target: quality["target_switch_out_events"]+=1
            if pending is None or pending[:2]!=(stamp,new_tid):
                if is_target: quality["target_switches_missing_pmc"]+=1
                per_cpu.pop(cpu,None)
                pending=None
                continue
            values=pending[2]; pending=None
            previous=per_cpu.get(cpu)
            per_cpu[cpu]=(stamp,new_tid,values)
            if not is_target: continue
            if previous is None:
                quality["target_switches_missing_start_snapshot"]+=1
                continue
            before, previous_tid, previous_values=previous
            if previous_tid!=old_tid:
                quality["target_switches_thread_discontinuity"]+=1
                continue
            if stamp<before or any(a<b for a,b in zip(values,previous_values)):
                quality["target_switches_counter_or_clock_reset"]+=1
                continue
            delta=Counter(dict(zip(NAMES,(a-b for a,b in zip(values,previous_values)))))
            life.update(delta); life_time+=stamp-before
            quality["attributed_target_slices"]+=1
            if start<=before<=stamp<=end:
                complete.update(delta); complete_time+=stamp-before
                quality["complete_workload_slices"]+=1
            if before<end and stamp>start:
                overlapping.update(delta)
                overlap_time+=max(0,min(stamp,end)-max(before,start))
                if not start<=before<=stamp<=end:
                    quality["workload_boundary_slices"]+=1
    errors=sum(v for k,v in quality.items() if k.startswith("target_switches_"))
    if not life or life["InstructionRetired"]==0:
        raise ValueError(f"No valid PMC data for {mode}")
    def counters(values):
        return {name:values[name] for name in NAMES}
    return {
        "config":mode,"pid":pid,"counter_order":list(NAMES),
        "attribution":"per-CPU consecutive CSwitch snapshot delta assigned to outgoing thread; continuity checked",
        "scope":"Full benchmark process lifetime, user+kernel and any interrupt work charged to these scheduled intervals",
        "lifetime_counts":counters(life),
        "lifetime_scheduled_interval_us":life_time,
        "lifetime_ipc":life["InstructionRetired"]/life["TotalCycles"],
        "workload_counts_lower_bound":counters(complete),
        "workload_counts_upper_bound":counters(overlapping),
        "workload_fully_covered_interval_us":complete_time,
        "workload_scheduled_overlap_us":overlap_time,
        "workload_wall_duration_us":end-start,
        "quality":dict(quality),
        "pmc_status_records":statuses,
        "lost_events":lost_events,"lost_buffers":lost_buffers,
        "all_target_slices_attributed":errors==0,
        "notes":[
            "Counts are differences, never sums of cumulative snapshots.",
            "Snapshots are paired on the same logical CPU, with previous incoming TID == current outgoing TID.",
            "Boundary-crossing scheduled intervals cannot be apportioned exactly by instruction count.",
            "Workload lower/upper bounds exclude/include the entire boundary intervals; no linear prorating is claimed.",
            "Full-lifetime counters include startup, verification and output, unlike the main workload-only timing table.",
            "These are one profiled run per configuration, not medians of five runs.",
            "No DPC/ISR exclusion was recorded; attribution follows context-switch scheduled intervals."
        ]
    }

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trace-dir",type=Path,required=True)
    a=p.parse_args()
    result={"schema":1,"runs":[analyze(a.trace_dir,m) for m in ("Od","O2")]}
    output=a.trace_dir/"pmc-summary.json"
    output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    for r in result["runs"]:
        print(r["config"],r["lifetime_counts"],"IPC",round(r["lifetime_ipc"],4),
              "complete attribution:",r["all_target_slices_attributed"])
    print(output)
if __name__=="__main__": main()
