"""Headless browser screen recording of new HTTP requests to the local fixture service."""
import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from playwright.sync_api import sync_playwright

from scripts.local_lab import service
from scripts.common import write

PAGE = """<style>
*{box-sizing:border-box}body{margin:48px;background:#11212e;color:#f7f2e9;font:22px system-ui}
h1{font-size:46px}p{color:#c1d7da}pre{font:23px/1.45 monospace;background:#1d3444;padding:28px}
small{color:#a3d7c9}#caption{min-height:58px;max-width:950px}
</style><small>SUPPORT OPERATIONS / LOCAL FIXTURE</small><h1>Provider failover drill</h1>
<h2 id="stage"></h2><p id="caption"></p><pre id="result"></pre><p id="trace"></p>"""


def record():
    with service() as (url,received), TemporaryDirectory() as tmp, sync_playwright() as pw:
        from app.observe import provider
        from scripts.drill import injected
        opts={"headless":True}
        if os.getenv("BROWSER_EXECUTABLE"):
            opts["executable_path"]=os.environ["BROWSER_EXECUTABLE"]
        browser=pw.chromium.launch(**opts)
        context=browser.new_context(viewport={"width":1180,"height":760},record_video_dir=tmp,record_video_size={"width":1180,"height":760})
        page=context.new_page()
        page.set_content(PAGE)
        with httpx.Client(base_url=url,headers={"Authorization":"Bearer fixture-demo"}) as c:
            def segment(title,caption,seconds):
                until=time.monotonic()+seconds
                while time.monotonic()<until:
                    t=time.perf_counter()
                    r=c.post("/triage",json={"text":"How do I track my order?"})
                    r.raise_for_status()
                    value=r.json()
                    latency=round(1000*(time.perf_counter()-t),1)
                    provider.force_flush(2000)
                    root=next(s for s in reversed(received) if s["name"]=="POST /triage")
                    calls=[s for s in received if s["parent"]==root["span_id"]]
                    data=dict(prompt=value["prompt_version"],model=value["model"],category=value["category"],
                              served_by_secondary=value["fallback"],latency_ms=latency,mode=value["mode"])
                    page.evaluate("""([stage,caption,result,trace])=>{for(const [id,t] of Object.entries({stage,caption,result,trace})){
                                      document.getElementById(id).textContent=t}}""",
                                  [title,caption,json.dumps(data,indent=2),"Trace "+root["trace_id"]+"  |  "+
                                   " → ".join(s["status"].replace("STATUS_CODE_","") for s in calls)])
                    page.wait_for_timeout(1850)
            segment("1. Healthy primary","Each refresh sends an authenticated request to FastAPI. The model field identifies the provider double that answered.",24)
            with injected(primary_down=True):
                segment("2. Primary outage","A controlled 5xx activates retry, then fallback. While the circuit is open, requests go straight to the secondary.",48)
            segment("3. Primary recovery","The fault is cleared. After the recovery interval a probe succeeds and subsequent traffic uses the primary again.",42)
        video=page.video
        context.close()
        video.save_as("results/fixture/failover-demo.webm")
        browser.close()


if __name__=="__main__":
    record()
