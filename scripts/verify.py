"""Small correctness/CLI checks for both compiler builds (not a performance test)."""
import json
from pathlib import Path
import subprocess
ROOT = Path(__file__).resolve().parents[1]
checks=[]
fingerprints=[]
for config in ("Od","O2"):
    exe=ROOT/"build"/config/"benchmark.exe"
    result=subprocess.run([str(exe),"--self-test"],capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)["self_test"] == "passed"
    checks.append(config+": fixtures, signed extremes, uneven partitions, fixed seed, empty/K guards")
    for mode in ("sequential","parallel"):
        p=subprocess.run([str(exe),"--n","101","--repeats","3","--batches","2","--mode",mode,"--threads","4"],
                         capture_output=True,text=True,check=True)
        row=json.loads(p.stdout)
        assert row["verified"] and row["checksum"] == row["minimum"]*6 and row["batches"] == 2
        fingerprints.append((row["minimum"],row["checksum"],row["input_sum"]))
        checks.append(config+": "+mode+" results verified")
    for args in (["--n","0"],["--mode","invalid"],["--mode","parallel","--n","1","--threads","2"],["--n","-1"]):
        p=subprocess.run([str(exe),*args],capture_output=True,text=True)
        assert p.returncode != 0, args
        checks.append(config+": rejected "+" ".join(args))
assert len(set(fingerprints)) == 1, fingerprints
(ROOT/"results"/"verification.json").write_text(json.dumps({"passed":True,"checks":checks,"matching_fingerprints":fingerprints},indent=2)+"\n",encoding="utf-8")
print(f"{len(checks)} grouped checks passed; both builds and execution modes agree.")
