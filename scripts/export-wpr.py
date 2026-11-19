"""Decode WPR ETLs with local symbols, then generate CPU flamegraphs and PMC statistics."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trace-dir",type=Path,required=True)
    a=p.parse_args()
    trace=a.trace_dir.resolve()
    xperf=Path(os.environ.get("ProgramFiles(x86)",r"C:\Program Files (x86)"))/"Windows Kits"/"10"/"Windows Performance Toolkit"/"xperf.exe"
    if not xperf.is_file(): raise SystemExit("Windows Performance Toolkit / xperf is required")
    env=os.environ.copy()
    env["_NT_SYMCACHE_PATH"]=str(ROOT/"results"/"symcache")
    results=[]
    for mode in ("Od","O2"):
        symbols=trace/"symbols"/mode
        if not symbols.is_dir(): symbols=ROOT/"build"/mode
        env["_NT_SYMBOL_PATH"]=str(symbols)
        cmd=[str(xperf),"-i",str(trace/f"{mode}.etl"),"-symbols","-o",str(trace/f"{mode}-dump.txt"),"-a","dumper","-stacktimeshifting"]
        result=subprocess.run(cmd,env=env,capture_output=True)
        # Native xperf diagnostic output is ASCII for this tool invocation.
        warnings=result.stderr.decode("utf-8",errors="replace")
        (trace/f"{mode}-decode-warnings.txt").write_text(warnings,encoding="utf-8")
        (trace/f"{mode}-decode-stdout.txt").write_text(result.stdout.decode("utf-8",errors="replace"),encoding="utf-8")
        if result.returncode: raise SystemExit(f"xperf failed for {mode}: see decode logs")
        subprocess.run([str(xperf),"-i",str(trace/f"{mode}.etl"),"-o",str(trace/f"{mode}-tracestats.txt"),"-a","tracestats"],env=env,check=True,capture_output=True)
        results.append({"config":mode,"command":cmd,"returncode":result.returncode,"symbols":str(symbols),
                        "invalid_event_warning_count":warnings.count("signaled an Invalid Event")})
        print(f"{mode}: exported; {results[-1]['invalid_event_warning_count']} decoder warnings",flush=True)
    (trace/"export.json").write_text(json.dumps(results,indent=2)+"\n",encoding="utf-8")
    for script in ("etl.py","pmc.py"):
        subprocess.run([sys.executable,str(ROOT/"scripts"/script),"--trace-dir",str(trace)],check=True)
if __name__=="__main__": main()
