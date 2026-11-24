"""Verify trace delivery and screenshot the provisioned Grafana dashboard under fixture traffic."""
import argparse
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

from scripts.common import write
from scripts.drill import injected

API="http://127.0.0.1:8765"
PHOENIX="http://127.0.0.1:16006"
GRAFANA="http://127.0.0.1:13000"
PROM="http://127.0.0.1:19090"


def issue(c, mode="fixture"):
    local=mode=="local"
    origin="http://127.0.0.1:8766" if local else API
    key=os.environ["TRIAGE_KEYS"].split(",")[0] if local else "fixture-demo"
    r=c.post(origin+"/triage",json={"text":"I have a question about my refund."},headers={"Authorization":"Bearer "+key})
    r.raise_for_status()
    assert r.json()["mode"]==mode
    return r.json(), r.headers["X-Trace-Id"]


def phoenix(c):
    with injected(primary_down=True):
        result, trace_id=issue(c)
        assert result["fallback"] and result["mode"]=="fixture"
        for _ in range(15):
            time.sleep(.25)
            issue(c)
    for _ in range(15):
        response=c.get(PHOENIX+"/v1/projects/ticket-triage/spans",params={"limit":100})
        response.raise_for_status()
        spans=response.json()["data"]
        for root in spans:
            if root["context"]["trace_id"] != trace_id or not any(e["name"]=="retry" for e in root["events"]):
                continue
            calls=sorted([s for s in spans if s["parent_id"]==root["context"]["span_id"]],key=lambda s:s["start_time"])
            if len(calls)!=3 or not calls[-1]["attributes"].get("app.secondary") or calls[-1]["status_code"]!="OK":
                continue
            selected=[root,*calls]
            for s in selected:
                s["duration_ms"]=(datetime.fromisoformat(s["end_time"])-datetime.fromisoformat(s["start_time"])).total_seconds()*1000
            write("results/fixture/phoenix-trace.json",dict(mode="fixture",transport="Service -> Collector -> Phoenix",
                       trace_id=root["context"]["trace_id"],spans=selected))
            return
        time.sleep(1)
    raise RuntimeError("Phoenix did not return a complete successful failover trace")


def capture(c, mode="fixture"):
    origin="host.docker.internal:8766" if mode=="local" else "api:8000"
    board=c.get(GRAFANA+"/api/dashboards/uid/triage").json()["dashboard"]
    stop=threading.Event()
    errors=[]
    def background():
        try:
            while not stop.wait(.23):
                issue(c,mode)
        except Exception as exc:
            errors.append(type(exc).__name__)
    sender=threading.Thread(target=background)
    sender.start()
    try:
        query=f'sum(rate(triage_requests_total{{instance="{origin}",outcome="ok"}}[30s]))'
        deadline=time.monotonic()+120
        while True:
            data=c.get(PROM+"/api/v1/query",params={"query":query}).json()
            if any(float(s["value"][1])>0 for s in data["data"]["result"]):
                break
            if errors or time.monotonic()>=deadline:
                raise RuntimeError(f"No successful traffic observed for {origin}: {errors}")
            time.sleep(2)
        with sync_playwright() as pw:
            options={"executable_path":os.environ["BROWSER_EXECUTABLE"]} if os.getenv("BROWSER_EXECUTABLE") else {}
            browser=pw.chromium.launch(**options)
            page=browser.new_page(viewport={"width":1540,"height":1250},device_scale_factor=1)
            page.goto(GRAFANA+f"/d/triage/support-ticket-triage?orgId=1&var-instance={origin}&from=now-5m&to=now&refresh=5s&kiosk",
                      wait_until="domcontentloaded",timeout=60000)
            for panel in board["panels"]:
                page.get_by_text(panel["title"],exact=True).first.wait_for(timeout=60000)
            page.wait_for_timeout(8000)
            page.get_by_text("No data",exact=True).first.wait_for(state="hidden",timeout=60000)
            page.screenshot(path=f"results/{mode}/grafana-load.png",full_page=True)
            browser.close()
        rules=c.get(PROM+"/api/v1/rules").json()["data"]["groups"]
        targets=c.get(PROM+"/api/v1/targets").json()["data"]["activeTargets"]
        collector_url="http://collector:8889/metrics"
        assert any(t["scrapeUrl"]==collector_url and t["health"]=="up" for t in targets)
        source=c.get(PROM+"/api/v1/query",params={"query":f'sum(rate(triage_requests_total{{instance="{origin}"}}[30s]))'}).json()
        assert any(float(s["value"][1])>0 for s in source["data"]["result"]), "No traffic from the selected scrape source"
        checks={}
        for panel in board["panels"]:
            series=0
            for target in panel["targets"]:
                data=c.get(PROM+"/api/v1/query",params={"query":target["expr"].replace("$instance",origin)}).json()
                assert data["status"]=="success"
                series+=len(data["data"]["result"])
            checks[panel["title"]]=series
        assert all(checks.values()) and not errors
        write(f"results/{mode}/monitoring.json",dict(mode=mode,dashboard=board["uid"],series=checks,metrics_target=collector_url,source=origin,
             alerts=[r["name"] for group in rules for r in group["rules"] if r["type"]=="alerting"],sender_errors=errors))
    finally:
        stop.set()
        sender.join()


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--mode",choices=["fixture","local"],default="fixture")
    mode=parser.parse_args().mode
    with httpx.Client(trust_env=False,timeout=120) as c:
        if mode=="fixture":
            assert c.get(API+"/readyz").json()["mode"]=="fixture"
            phoenix(c)
            time.sleep(1)
        else:
            origin="http://127.0.0.1:8766"
            assert c.get(origin+"/readyz").json()["mode"]=="local"
            assert c.post(origin+"/triage",json={"text":"authentication control"}).status_code==401
        capture(c,mode)
