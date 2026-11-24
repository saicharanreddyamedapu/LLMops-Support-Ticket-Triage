"""Collect hourly end-to-end probes and the matching seven-day Prometheus report."""
import argparse
import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic import ValidationError

from app.schema import Served
from scripts.slo_report import availability_report

PERIOD = 3600
SLOTS = 7 * 24
DIRECTORY = Path(".local/availability-watch")
TICKETS = ("How can I track my order?", "Which payment methods do you accept?",
           "Where can I find my invoice?", "How can I update my address?")


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def initialize(db, now):
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS window (id INTEGER PRIMARY KEY CHECK(id=1), start INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS probes (slot INTEGER PRIMARY KEY, timestamp INTEGER, outcome TEXT, "
                   "latency_ms REAL, prompt TEXT, revision TEXT, trace TEXT)")
        db.execute("INSERT OR IGNORE INTO window VALUES (1, ?)", (int(now)//60*60,))
    return db.execute("SELECT start FROM window WHERE id=1").fetchone()[0]


def probe(client, ticket, key):
    try:
        ready = client.get("/readyz", timeout=10)
        ready.raise_for_status()
        if ready.json().get("mode") != "local":
            return "unexpected_mode", None, None, None
        response = client.post("/triage", json={"text":ticket}, headers={"Authorization":"Bearer "+key})
        response.raise_for_status()
        result = Served.model_validate(response.json())
        if result.mode != "local":
            return "unexpected_mode", None, None, None
        return "ok", result.prompt_version, result.git_sha, response.headers.get("X-Trace-Id")
    except httpx.HTTPStatusError as exc:
        return f"http_{exc.response.status_code}", None, None, None
    except (ValueError, ValidationError, httpx.RequestError) as exc:
        return type(exc).__name__, None, None, None


def sample(db, start, now, client, key):
    slot = (int(now)-start)//PERIOD
    if not 0 <= slot < SLOTS:
        return
    # Reserve before dispatch. If the observer is interrupted, that slot stays
    # unknown and restarting cannot silently replace it with a later success.
    with db:
        reserved = db.execute("INSERT OR IGNORE INTO probes(slot,timestamp,outcome) VALUES(?,?,?)",
                              (slot,int(now),"interrupted")).rowcount
    if reserved:
        tick = time.monotonic()
        outcome, version, revision, trace = probe(client,TICKETS[slot%len(TICKETS)],key)
        with db:
            db.execute("UPDATE probes SET outcome=?,latency_ms=?,prompt=?,revision=?,trace=? WHERE slot=?",
                       (outcome, round((time.monotonic()-tick)*1000,2), version, revision, trace, slot))


def snapshot(db, start, now, prom):
    end = start + SLOTS*PERIOD
    counts = dict(db.execute("SELECT outcome,COUNT(*) FROM probes GROUP BY outcome"))
    due = max(0,min(SLOTS, (int(now)-start)//PERIOD+1))
    missing = due - sum(counts.values())
    passed = counts.get("ok",0)
    try:
        counters = availability_report(prom,"local",min(now,end))
    except (ValueError, httpx.HTTPError) as exc:
        counters = {"status":"prometheus_unavailable","error":type(exc).__name__}
    status = "collecting"
    if now >= end:
        if missing or counts.get("interrupted") or counters["status"] not in {"met","missed"}:
            status = "incomplete"
        elif counters["status"] == "met" and passed/SLOTS >= .99:
            status = "met"
        else:
            status = "missed"
    return dict(status=status, start_utc=utc(start), end_utc=utc(end), updated_utc=utc(now),
                planned_hourly_probes=SLOTS, due=due, missing=missing, outcomes=counts,
                successful_share_of_due=passed/due if due else None, prometheus=counters)


def save(report):
    with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=DIRECTORY,delete=False) as f:
        json.dump(report,f,indent=2,allow_nan=False)
        f.write("\n")
    try:
        os.replace(f.name,DIRECTORY/"status.json")
    finally:
        Path(f.name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status",action="store_true",help="Show the persisted window without sending a probe")
    args = parser.parse_args()
    if args.status:
        print((DIRECTORY/"status.json").read_text(encoding="utf-8"))
        return
    key = os.environ["TRIAGE_KEYS"].split(",")[0]
    DIRECTORY.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(DIRECTORY/"observations.sqlite3",timeout=30) as db, \
         httpx.Client(base_url="http://127.0.0.1:8766",trust_env=False,timeout=185) as client, \
         httpx.Client(base_url="http://127.0.0.1:19090",trust_env=False,timeout=30) as prom:
        start = initialize(db,time.time())
        end = start+SLOTS*PERIOD
        while True:
            now = time.time()
            sample(db,start,now,client,key)
            report = snapshot(db,start,time.time(),prom)
            save(report)
            if now >= end:
                return
            next_slot = start+((int(now)-start)//PERIOD+1)*PERIOD
            time.sleep(max(1,min(end,next_slot)-time.time()))


if __name__ == "__main__":
    main()
