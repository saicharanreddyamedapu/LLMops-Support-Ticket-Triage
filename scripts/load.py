"""Run k6 against the running, fixture-only Compose service."""
import json
import subprocess
import urllib.request
from pathlib import Path

if __name__ == "__main__":
    with urllib.request.urlopen("http://127.0.0.1:8765/readyz") as response:
        assert json.load(response)["mode"] == "fixture", "Fixture load tests require fixture mode"
    out = Path("results/fixture").resolve()
    out.mkdir(parents=True,exist_ok=True)
    for rate in (2, 6, 12, 24):
        subprocess.run(["docker","run","--rm","--network","ticket-triage_default","-v",f"{Path('ops/load.js').resolve()}:/load.js:ro",
                        "-v",f"{out}:/results","-e",f"RATE={rate}","grafana/k6:1.3.0","run","--quiet","/load.js"],check=True)
