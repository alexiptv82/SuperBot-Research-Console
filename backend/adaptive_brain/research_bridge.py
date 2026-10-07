from __future__ import annotations
import csv, hashlib, json
from pathlib import Path

class ResearchBridge:
    """Read-only bridge. Historical diagnostics never become direct trade instructions."""
    def __init__(self, backend_root: str|Path):
        self.backend_root=Path(backend_root)
        self.report_dir=self.backend_root/"recovery"/"reports"/"v1_2"

    @staticmethod
    def _sha(path: Path):
        h=hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
        return h.hexdigest()

    def snapshot(self):
        manifest=self.report_dir/"stage3_manifest.json"; diag=self.report_dir/"stage3_diagnostics.csv"
        out={"available":manifest.exists() and diag.exists(),"mode":"READ_ONLY","direct_trade_signal_export":False,"manifest":None,"diagnostics":None}
        if manifest.exists():
            raw=json.loads(manifest.read_text(encoding="utf-8"))
            out["manifest"]={"sha256":self._sha(manifest),"runtime_commit":raw.get("runtime_commit") or raw.get("commit"),"new36_opened":raw.get("new36_opened"),"frozen_analysis_engine_state":raw.get("frozen_analysis_engine_state")}
        if diag.exists():
            rows=0; counts={}
            with diag.open("r",encoding="utf-8",newline="") as f:
                for row in csv.DictReader(f):
                    rows+=1; axis=row.get("axis") or row.get("family") or row.get("experiment") or "UNKNOWN"
                    counts[axis]=counts.get(axis,0)+1
            out["diagnostics"]={"sha256":self._sha(diag),"rows":rows,"axis_counts":counts}
        return out
