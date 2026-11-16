import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
env = dict(os.environ, PYTHONPATH=str(project / "src"))


def cli(db, tenant, *args):
    completed = subprocess.run([sys.executable, "-m", "cohortlab.cli", "--db", str(db), "--tenant", tenant, *map(str, args)],
                               env=env, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    db = root / "cohort.sqlite"
    definition = project / "examples" / "query.json"
    cli(db, "demo", "ingest", project / "examples" / "population.json")
    first = cli(db, "demo", "query", definition, "--principal", "analyst")
    cached = cli(db, "demo", "query", definition, "--principal", "analyst")
    if first["total"] != 3 or not cached["cache_hit"]: raise RuntimeError("sequence query or persistent cache failed")
    empty_tenant = cli(db, "other", "query", definition, "--principal", "analyst")
    if empty_tenant["total"] is not None: raise RuntimeError("tenant isolation failed")
    frozen = root / "snapshot-query.json"
    frozen.write_text(json.dumps({"snapshot": first["snapshot"], "group_by": ["region"]}))
    snapshot = cli(db, "demo", "query", frozen, "--principal", "analyst")
    if snapshot["total"] != 3: raise RuntimeError("cohort snapshot is not reproducible")
    grouped = json.loads(definition.read_text())
    grouped.pop("where")
    grouped.pop("sequence")
    grouped.pop("max_gap_seconds")
    small = root / "small.json"
    small.write_text(json.dumps(grouped))
    suppressed = cli(db, "demo", "query", small, "--principal", "analyst")
    if any(cell["count"] is not None for cell in suppressed["cells"]): raise RuntimeError("complementary suppression failed")
    print(json.dumps({"ok": True, "cohort_size": 3, "persistent_cache": True, "tenant_isolation": True, "suppression": True, "cli_processes": 6}))
