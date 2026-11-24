"""Run k6 against the fixture Compose API or the host's pinned local model."""
import argparse
import json
import os
import subprocess
import urllib.request
from pathlib import Path

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode",choices=["fixture","local"],default="fixture")
    mode = parser.parse_args().mode
    local = mode == "local"
    port = 8766 if local else 8765
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=5) as response:
        if json.load(response)["mode"] != mode:
            raise SystemExit("API readiness differs from the requested load-test mode")
    out = Path("results",mode).resolve()
    out.mkdir(parents=True,exist_ok=True)
    extra = []
    env = os.environ.copy()
    if local:
        env["TRIAGE_KEY"] = env["TRIAGE_KEYS"].split(",")[0]
        extra = ["-e","TRIAGE_KEY","-e","INTERVAL=4","-e","SECONDS=16","-e","URL=http://host.docker.internal:8766/triage"]
    for rate in ((1,2,4) if local else (2,6,12,24)):
        subprocess.run(["docker","run","--rm","--network","ticket-triage_default","-v",f"{Path('ops/load.js').resolve()}:/load.js:ro",
                        "-v",f"{out}:/results","-e",f"RATE={rate}","-e",f"MODE={mode}",*extra,
                        "grafana/k6:1.3.0","run","--quiet","/load.js"],check=True,env=env)
