"""Fixture-only canary drill against a running Compose API; restores original config."""
import json
import os
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from prometheus_client.parser import text_string_to_metric_families

from scripts.common import quantile, write
from scripts.rollout import weight

URL = os.getenv("DRILL_URL", "http://127.0.0.1:8765")
FAULT = Path(os.getenv("FIXTURE_FAULT_FILE", "config/fixture-fault.json"))
POLICY = json.loads(Path("config/canary.json").read_text())


@contextmanager
def injected(**faults):
    if FAULT.exists():
        raise RuntimeError("existing fault configuration present; inspect before running")
    tmp = FAULT.with_suffix(".tmp")
    write(tmp,faults)
    tmp.replace(FAULT)
    try:
        yield
    finally:
        FAULT.unlink(missing_ok=True)


def snapshot(c):
    result = Counter()
    for family in text_string_to_metric_families(c.get("/metrics").text):
        for s in family.samples:
            if s.name.endswith("_total") and "cohort" in s.labels:
                result[s.name, s.labels["cohort"], s.labels.get("outcome",s.labels.get("valid",""))] += s.value
    return result


def cohort_stats(delta, times, name):
    n = sum(v for (metric,c,_),v in delta.items() if metric=="triage_requests_total" and c==name)
    valid = delta["triage_first_schema_total",name,"true"]
    invalid = delta["triage_first_schema_total",name,"false"]
    return dict(n=n, availability=delta["triage_requests_total",name,"ok"]/n if n else 0,
                first_pass=valid/(valid+invalid) if valid+invalid else 0,p95_ms=quantile(times,.95),
                priced_usd_per_request=delta["triage_cost_usd_total",name,""]/n if n else None)


def breaches(stable, candidate):
    if min(stable["n"],candidate["n"]) < POLICY["minimum_per_cohort"]:
        raise ValueError("insufficient cohort traffic")
    return [key for key,failed in {
        "availability":candidate["availability"] < max(POLICY["min_availability"],stable["availability"]-.01),
        "first_pass":candidate["first_pass"] < max(POLICY["min_schema_valid"],stable["first_pass"]-.01),
        "p95_ms":candidate["p95_ms"] > stable["p95_ms"]*POLICY["max_p95_ratio"],
        "priced_usd_per_request":candidate["priced_usd_per_request"] > stable["priced_usd_per_request"]*POLICY["max_cost_ratio"]}.items() if failed]


def ticket(c):
    start=time.perf_counter()
    r=c.post("/triage",json={"text":"Please help me track my order."})
    ms=(time.perf_counter()-start)*1000
    value=r.json()
    time.sleep(.085)
    return value,ms


def run():
    original=json.loads(Path(os.getenv("ROLLOUT_PATH","config/rollout.json")).read_text())["percent"]
    gate=Path("results/fixture/gate.md").read_text()
    if "REJECT" in gate or "|p95_ms|" not in gate:
        raise RuntimeError("candidate must pass offline gate")
    with httpx.Client(base_url=URL,headers={"Authorization":"Bearer fixture-demo"}) as c:
        assert c.get("/readyz").json()["mode"]=="fixture"
        try:
            weight(10)
            with injected(slow_candidate=True):
                start=snapshot(c)
                samples={"stable":[],"candidate":[]}
                versions=Counter()
                for _ in range(900):
                    item,ms=ticket(c)
                    if "cohort" in item:
                        samples[item["cohort"]].append(ms)
                        versions[item["prompt_version"]] += 1
                    if min(map(len,samples.values()))>=POLICY["minimum_per_cohort"]:
                        break
                delta=snapshot(c)-start
                stats={name:cohort_stats(delta,times,name) for name,times in samples.items()}
                reasons=breaches(**stats)
                if "p95_ms" not in reasons:
                    raise RuntimeError("latency drill did not trip threshold")
                weight(0)
                start=snapshot(c)
                recovered=[ticket(c) for _ in range(30)]
                assert all(v.get("cohort")=="stable" and v["prompt_version"]=="v1" for v,_ in recovered)
                recovery=cohort_stats(snapshot(c)-start,[t for _,t in recovered],"stable")
                assert recovery["availability"]==1 and recovery["p95_ms"] <= stats["stable"]["p95_ms"]*POLICY["max_p95_ratio"]
                write("results/fixture/canary.json",dict(mode="fixture",timestamp=datetime.now(timezone.utc).isoformat(),
                      offline_gate="results/fixture/gate.md", percent=10, versions=versions, before=stats,breaches=reasons,
                      rollback_percent=0,recovered_versions={"v1":30},after=recovery, policy=POLICY))
        finally:
            weight(original)


if __name__=="__main__":
    run()
