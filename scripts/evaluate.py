"""A warmed, serial API benchmark. Fixture and live output go to separate paths."""
import argparse
import asyncio
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
from scripts.dataset import REV, dataset_digest, provisional_priorities, reviews


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
    judge_cfg = dict(cfg, version=rubric["version"], params={**cfg["params"], "temperature":0,"max_tokens":32})
    if settings.MODE == "local":
        judge_cfg["params"]["response_format"] = {"type":"json_schema","json_schema":{"name":"grade","schema":Grade.model_json_schema()}}
    with observe.tracer.start_as_current_span("judge draft reply") as span:
        value = await gateway.call(cfg["secondary"], messages, judge_cfg, "judge", True)
    return Grade.model_validate_json(value["text"]).passed


async def run(args, temp):
    os.environ.update(TRIAGE_MODE=args.mode, CLIENT_RPS="10000", SINGLE_PROVIDER="1", ROLLOUT_PATH=str(temp/"rollout.json"))
    write(temp/"rollout.json", {"stable":args.version,"candidate":args.version,"percent":0})
    from app import settings
    from app.main import app
    if args.mode == "local":
        cfg = settings.prompt(args.version)
        async with httpx.AsyncClient(trust_env=False) as local:
            response = await local.get("http://127.0.0.1:11434/api/tags")
            response.raise_for_status()
            if not any(m["name"] == cfg["primary"].split("/",1)[1] and m["digest"] == cfg["local_digest"]
                       for m in response.json()["models"]):
                raise SystemExit("Pinned local model is unavailable")
        os.environ.setdefault("TRIAGE_KEYS", "local-evaluation")
    rows, labels = reviews()
    human_review_total = len(labels)
    provisional_digest = None
    if args.provisional_priority:
        labels, provisional_digest = provisional_priorities(rows)
    cases = [r for r in rows if r["split"] == args.split]
    records, durations = [], []
    correct = dict(category=0, product=0, priority=0)
    reviewed = sum(r["id"] in labels for r in cases)
    valid = ok = grounded = draft_pass = judged = judge_errors = 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://local",
                                 headers={"Authorization":"Bearer "+settings.client_keys()[0]}) as client:
        if (await client.get("/readyz")).status_code != 200:
            raise SystemExit("Missing runtime configuration")
        if args.mode in ("fixture","local"):
            warm = await client.post("/triage",json={"text":"Question about my order"})
            warm.raise_for_status()
        before = scores()
        for row in cases:
            start = time.perf_counter()
            response = await client.post("/triage", json={"text":row["text"]})
            durations.append((time.perf_counter()-start)*1000)
            value = response.json()
            grade = None
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
                        grade = await judge(row, value["draft_reply"], settings.prompt(args.version))
                        draft_pass += grade
                        judged += 1
                    except Exception:
                        judge_errors += 1
            record = {"id":row["id"], "output":value}
            if args.judge:
                record["draft_reply_pass"] = grade
            records.append(record)
            if args.mode == "local" and len(records) % 20 == 0:
                print(f"Evaluated {len(records)}/{len(cases)} tickets; {ok} successful", flush=True)
    cost, tin, tout, unknown = [end-start for end,start in zip(scores(),before)]
    n = len(cases)
    digest = dataset_digest()
    result = dict(mode=args.mode, method="asgi-serial-v1", split=args.split, revision=REV, dataset_digest=digest,
                  n=n, timestamp=datetime.now(timezone.utc).isoformat(), git_sha=settings.revision(), prompt_version=args.version,
                  primary=settings.prompt(args.version)["primary"], providers=1, cache="bypass" if args.mode!="replay" else "replay",
                  availability=ok/n, first_pass=valid/n, category=correct["category"]/n, product=correct["product"]/n,
                  priority=correct["priority"]/reviewed if reviewed else None, reviewed=reviewed, human_review_total=human_review_total,
                  priority_source="assistant-rubric-v1" if args.provisional_priority else "human",
                  provisional_digest=provisional_digest,
                  summary_grounding=grounded/n, draft_reply_judge=draft_pass/judged if judged else None,
                  judge_n=judged, judge_errors=judge_errors, judge_cost_usd=REGISTRY.get_sample_value("triage_cost_usd_total",{"cohort":"judge"}) or 0,
                  p50_ms=quantile(durations,.5),p95_ms=quantile(durations,.95),p99_ms=quantile(durations,.99),
                  input_tokens=tin,output_tokens=tout,unknown_usage_attempts=unknown,priced_usd=cost,priced_usd_per_request=cost/n,
                  priced_usd_per_1000_success=cost*1000/ok if ok else None, monthly_usd_at_100k=100000*cost/n,
                  provider_spend_usd=0 if args.mode!="live" else None)
    if args.mode == "local":
        result.update(model_digest=cfg["local_digest"], judge_model=cfg["secondary"] if args.judge else None,
                      cost_scope="provider fees only; local hardware and electricity excluded",
                      judge_input_tokens=REGISTRY.get_sample_value("triage_tokens_total",{"cohort":"judge","direction":"in"}) or 0,
                      judge_output_tokens=REGISTRY.get_sample_value("triage_tokens_total",{"cohort":"judge","direction":"out"}) or 0)
    write(args.output or f"results/{args.mode}/current.json", result)
    if args.outputs:
        write(args.outputs,records)
    print(" | ".join(f"{k}={v:.5g}" if isinstance(v,float) else f"{k}={v}" for k,v in result.items()))
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode",choices=["fixture","live","replay","local"],default="fixture")
    p.add_argument("--version",default="v2")
    p.add_argument("--split",choices=["dev","test"],default="test")
    p.add_argument("--output")
    p.add_argument("--outputs")
    p.add_argument("--judge",action="store_true")
    p.add_argument("--provisional-priority",action="store_true",help="Score separately against identified assistant annotations")
    a = p.parse_args()
    if a.judge and a.mode not in ("live","local"):
        p.error("--judge requires a real model in local or live mode")
    if a.outputs and a.mode not in ("fixture","local"):
        p.error("--outputs is for offline evaluations on the public sample")
    with tempfile.TemporaryDirectory() as temp:
        asyncio.run(run(a,Path(temp)))


if __name__ == "__main__":
    main()
