"""Synthetic attribution checks: these data are not PC measurements."""
import json
from pathlib import Path
import tempfile
import unittest
import pmc
ROOT=Path(__file__).resolve().parents[1]

class CounterAttribution(unittest.TestCase):
    def make(self, directory, missing=False):
        epoch=1_000_000
        trace=(116444736000000000+epoch*10)
        child=dict(n=1,repeats=1,batches=1,mode="sequential",verified=True,start_unix_us=epoch+5,end_unix_us=epoch+25)
        (directory/"Od-child.json").write_text(json.dumps(child),encoding="utf-8")
        lines=["BeginHeader","EndHeader",f"Events Lost: 0, Buffers lost: 0, Trace Start: {trace}",
               'P-Start, 1, benchmark.exe (12), 0, 1, 0, 0, 0, SID, "D:\\build\\Od\\benchmark.exe" --n 1 --repeats 1 --batches 1 --mode sequential']
        def switch(t,cpu,new,old,values,oldproc="Idle (0)",include=True):
            if include: lines.append("Pmc, "+", ".join(map(str,[t,new,*values])))
            row=["CSwitch",str(t),"new process",str(new),"0","0","0","0",oldproc,str(old),"0","0","Running","Executive","0","0",str(cpu)]
            lines.append(", ".join(row))
        switch(2,0,101,0,(100,200,300,400))
        switch(10,0,0,101,(105,207,400,500),"benchmark.exe (12)",not missing)
        switch(12,1,101,0,(1000,2000,3000,4000))
        switch(22,1,0,101,(1011,2013,3300,4400),"benchmark.exe (12)")
        lines.append("P-End, 30, benchmark.exe (12), 0")
        (directory/"Od-dump.txt").write_text("\n".join(lines)+"\n",encoding="utf-8")
    def test_per_cpu_outgoing_thread_and_boundary_bounds(self):
        with tempfile.TemporaryDirectory(dir=ROOT/"build") as folder:
            directory=Path(folder).resolve()
            self.assertTrue(directory.is_relative_to(ROOT.resolve()))
            self.make(directory)
            r=pmc.analyze(directory,"Od")
            self.assertEqual(r["lifetime_counts"],dict(zip(pmc.NAMES,(16,20,400,500))))
            self.assertEqual(r["workload_counts_lower_bound"],dict(zip(pmc.NAMES,(11,13,300,400))))
            self.assertEqual(r["workload_counts_upper_bound"],r["lifetime_counts"])
            self.assertTrue(r["all_target_slices_attributed"])
    def test_missing_snapshot_is_reported(self):
        with tempfile.TemporaryDirectory(dir=ROOT/"build") as folder:
            directory=Path(folder).resolve()
            self.assertTrue(directory.is_relative_to(ROOT.resolve()))
            self.make(directory,missing=True)
            r=pmc.analyze(directory,"Od")
            self.assertFalse(r["all_target_slices_attributed"])
            self.assertEqual(r["quality"]["target_switches_missing_pmc"],1)

if __name__=="__main__": unittest.main()
