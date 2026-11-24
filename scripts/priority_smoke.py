"""Exercise rare priority classes over HTTP against an already running local Qwen service."""
import argparse
import os
import time

import httpx

from scripts.common import write

CASES = [
    ("urgent", "Someone is in my account right now placing orders I did not authorize."),
    ("urgent", "I can see unauthorized card charges from your store still appearing today."),
    ("high", "My account is locked and I cannot sign in to see any of my orders."),
    ("high", "You charged me twice for the same order. Please help reverse the duplicate."),
    ("high", "Checkout declines every payment and I cannot complete my purchase."),
    ("normal", "Please change the delivery address on my existing order."),
    ("low", "Which payment methods does your store accept?"),
    ("low", "Where can I leave feedback about a recent delivery?"),
]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--verify-trace", action="store_true", help="Verify the last HTTP request in the local Compose Phoenix")
    args = parser.parse_args()
    records = []
    with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", trust_env=False, timeout=185,
                      headers={"Authorization":"Bearer "+os.environ["TRIAGE_KEYS"].split(",")[0]}) as client:
        assert client.get("/readyz").json() == {"status":"ready","mode":"local"}
        for expected,text in CASES:
            response = client.post("/triage",json={"text":text})
            response.raise_for_status()
            value = response.json()
            assert value["mode"] == "local"
            records.append(dict(text=text,expected=expected,observed=value["priority"],prompt_version=value["prompt_version"],
                                model=value["model"],git_sha=value["git_sha"],trace_id=response.headers["X-Trace-Id"]))
    hits = sum(r["expected"] == r["observed"] for r in records)
    write("results/local/priority-smoke.json",dict(method="synthetic development probes over host HTTP",
          label_source="assistant-authored examples and expectations",n=len(records),matches=hits,records=records))
    print(f"{hits}/{len(records)} synthetic priority probes matched")
    if args.verify_trace:
        trace_id = records[-1]["trace_id"]
        with httpx.Client(trust_env=False,timeout=5) as client:
            for _ in range(12):
                response = client.get("http://127.0.0.1:16006/v1/projects/ticket-triage/spans",params={"limit":100})
                response.raise_for_status()
                spans = [s for s in response.json()["data"] if s["context"]["trace_id"] == trace_id]
                root = next((s for s in spans if s["name"] == "POST /triage"), None)
                if root and any(s["parent_id"] == root["context"]["span_id"] and s["status_code"] == "OK"
                                and s["attributes"].get("gen_ai.usage.output_tokens",0) > 0 for s in spans):
                    write("results/local/phoenix-trace.json",dict(mode="local",trace_id=trace_id,spans=spans))
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Phoenix did not return the Qwen request and token-bearing child")
    raise SystemExit(int(hits != len(records)))
