"""A warmed, serial API benchmark. Fixture and live output go to separate paths."""
import argparse
import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from prometheus_client import REGISTRY
from pydantic import BaseModel, ConfigDict, Field

from scripts.common import quantile, write
from scripts.dataset import REV, SAMPLE, QUEUE, reviews


def counter(name, **labels):
    return REGISTRY.get_sample_value(name, {"cohort":"stable", **labels}) or 0


def scores():
    return [counter("triage_cost_usd_total"), counter("triage_tokens_total",direction="in"),
            counter("triage_tokens_total",direction="out"), counter("triage_unknown_usage_total")]


class Grade(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    passed: bool = Field(alias="pass")


async def judge(row, reply, cfg):
    from app import gateway, observe, settings
    rubric = yaml.safe_load(Path("prompts/judge.yaml").read_text())
    messages = [{"role":"system","content":rubric["system"]}, {"role":"user","content":json.dumps(
        {"ticket":row["text"],"reference":row["reference"],"draft_reply":reply})}]
    judge_cfg = dict(cfg, version=rubric["version"], params={"temperature":0,"max_tokens":32})
    with observe.tracer.start_as_current_span("judge draft reply") as span:
        value = await gateway.call(cfg["secondary"], messages, judge_cfg, "judge", True)
    return Grade.model_validate_json(value["text"]).passed


async def run(args, temp):
    os.environ.update(TRIAGE_MODE=args.mode, CLIENT_RPS="10000", SINGLE_PROVIDER="1", ROLLOUT_PATH=str(temp/"rollout.json"))
    write(temp/"rollout.json", {"stable":args.version,"candidate":args.version,"percent":0})
    from app import settings
    from app.main import app
    rows, labels = reviews()
    cases = [r for r in rows if r["split"] == args.split]
    records, durations = [], []
    correct = dict(category=0, product=0, priority=0)
    reviewed = sum(r["id"] in labels for r in cases)
    valid = ok = grounded = draft_pass = judged = judge_errors = 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://local",
                                 headers={"Authorization":"Bearer "+settings.client_keys()[0]}) as client:
        if (await client.get("/readyz")).status_code != 200:
            raise SystemExit("Missing runtime configuration")
        if args.mode == "fixture":
            warm = await client.post("/triage",json={"text":"Warm up the benchmark"})
            warm.raise_for_status()
        before = scores()
        for row in cases:
            start = time.perf_counter()
            response = await client.post("/triage", json={"text":row["text"]})
            durations.append((time.perf_counter()-start)*1000)
            value = response.json()
            if response.is_success:
                ok += 1
                valid += value["first_pass"]
                for field in ("category","product"):
                    correct[field] += value[field] == row[field]
                correct["priority"] += value["priority"] == labels.get(row["id"])
                # Lexical grounding proxy, not a semantic factuality score.
                words = lambda s: set(re.findall(r"[a-z0-9]+", s.lower()))
                summary = words(value["summary"])
                grounded += bool(summary) and len(summary & words(row["text"])) / len(summary) >= .5
                if args.judge:
                    try:
                        draft_pass += await judge(row, value["draft_reply"], settings.prompt(args.version))
                        judged += 1
                    except Exception:
                        judge_errors += 1
            records.append({"id":row["id"], "output":value})
    cost, tin, tout, unknown = [end-start for end,start in zip(scores(),before)]
    n = len(cases)
    digest = hashlib.sha256(SAMPLE.read_bytes().replace(b"\r\n",b"\n") + QUEUE.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    result = dict(mode=args.mode, method="asgi-serial-v1", split=args.split, revision=REV, dataset_digest=digest,
                  n=n, timestamp=datetime.now(timezone.utc).isoformat(), git_sha=settings.revision(), prompt_version=args.version,
                  primary=settings.prompt(args.version)["primary"], providers=1, cache="bypass" if args.mode!="replay" else "replay",
                  availability=ok/n, first_pass=valid/n, category=correct["category"]/n, product=correct["product"]/n,
                  priority=correct["priority"]/reviewed if reviewed else None, reviewed=reviewed, human_review_total=len(labels),
                  summary_grounding=grounded/n, draft_reply_judge=draft_pass/judged if judged else None,
                  judge_n=judged, judge_errors=judge_errors, judge_cost_usd=REGISTRY.get_sample_value("triage_cost_usd_total",{"cohort":"judge"}) or 0,
                  p50_ms=quantile(durations,.5),p95_ms=quantile(durations,.95),p99_ms=quantile(durations,.99),
                  input_tokens=tin,output_tokens=tout,unknown_usage_attempts=unknown,priced_usd=cost,priced_usd_per_request=cost/n,
                  priced_usd_per_1000_success=cost*1000/ok if ok else None, monthly_usd_at_100k=100000*cost/n,
                  provider_spend_usd=0 if args.mode!="live" else None)
    write(args.output or f"results/{args.mode}/current.json", result)
    if args.outputs:
        if args.mode != "fixture":
            raise SystemExit("Publish only fixture outputs via --outputs")
        write(args.outputs,records)
    print(" | ".join(f"{k}={v:.5g}" if isinstance(v,float) else f"{k}={v}" for k,v in result.items()))
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode",choices=["fixture","live","replay"],default="fixture")
    p.add_argument("--version",default="v2")
    p.add_argument("--split",choices=["dev","test"],default="test")
    p.add_argument("--output")
    p.add_argument("--outputs")
    p.add_argument("--judge",action="store_true")
    a = p.parse_args()
    if a.judge and a.mode != "live":
        p.error("--judge requires live mode")
    with tempfile.TemporaryDirectory() as temp:
        asyncio.run(run(a,Path(temp)))


if __name__ == "__main__":
    main()
