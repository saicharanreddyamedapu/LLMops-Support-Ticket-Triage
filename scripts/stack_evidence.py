"""Verify trace delivery and screenshot the provisioned Grafana dashboard under fixture traffic."""
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


def issue(c):
    r=c.post(API+"/triage",json={"text":"I have a question about my refund."},headers={"Authorization":"Bearer fixture-demo"})
    r.raise_for_status()
    return r.json()


def phoenix(c):
    with injected(primary_down=True):
        result=issue(c)
        assert result["fallback"] and result["mode"]=="fixture"
        for _ in range(15):
            time.sleep(.25)
            issue(c)
    for _ in range(15):
        response=c.get(PHOENIX+"/v1/projects/ticket-triage/spans",params={"limit":100})
        response.raise_for_status()
        spans=response.json()["data"]
        for root in spans:
            if not any(e["name"]=="retry" for e in root["events"]):
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


def capture(c):
    board=c.get(GRAFANA+"/api/dashboards/uid/triage").json()["dashboard"]
    stop=threading.Event()
    errors=[]
    def background():
        try:
            while not stop.wait(.23):
                issue(c)
        except Exception as exc:
            errors.append(type(exc).__name__)
    sender=threading.Thread(target=background)
    sender.start()
    try:
        with sync_playwright() as pw:
            options={"executable_path":os.environ["BROWSER_EXECUTABLE"]} if os.getenv("BROWSER_EXECUTABLE") else {}
            browser=pw.chromium.launch(**options)
            page=browser.new_page(viewport={"width":1540,"height":1250},device_scale_factor=1)
            page.goto(GRAFANA+"/d/triage/support-ticket-triage?orgId=1&from=now-5m&to=now&refresh=5s&kiosk",
                      wait_until="domcontentloaded",timeout=60000)
            for panel in board["panels"]:
                page.get_by_text(panel["title"],exact=True).first.wait_for(timeout=60000)
            page.wait_for_timeout(8000)
            page.screenshot(path="results/fixture/grafana-load.png",full_page=True)
            browser.close()
        rules=c.get(PROM+"/api/v1/rules").json()["data"]["groups"]
        checks={}
        for panel in board["panels"]:
            expr=panel["targets"][0]["expr"]
            data=c.get(PROM+"/api/v1/query",params={"query":expr}).json()
            assert data["status"]=="success"
            checks[panel["title"]]=len(data["data"]["result"])
        assert checks["Request rate"]>0 and checks["Secondary responses (%)"]>0
        write("results/fixture/monitoring.json",dict(mode="fixture",dashboard=board["uid"],series=checks,
             alerts=[r["name"] for group in rules for r in group["rules"] if r["type"]=="alerting"],sender_errors=errors))
    finally:
        stop.set()
        sender.join()


if __name__=="__main__":
    with httpx.Client(trust_env=False,timeout=10) as c:
        assert c.get(API+"/readyz").json()["mode"]=="fixture"
        phoenix(c)
        time.sleep(1)
        capture(c)
