"""Exercise real loopback HTTP and OTLP without a container engine or credentials."""
import argparse
import asyncio
import json
import os
import socket
import threading
import time
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import uvicorn
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import Status

from scripts.common import quantile, write


def unpack(span):
    def simple(v):
        key=v.WhichOneof("value")
        if key=="array_value":
            return [simple(item) for item in v.array_value.values]
        return getattr(v,key)
    return dict(name=span.name,trace_id=span.trace_id.hex(),span_id=span.span_id.hex(),parent=span.parent_span_id.hex(),
                start_ns=span.start_time_unix_nano,duration_ms=(span.end_time_unix_nano-span.start_time_unix_nano)/1e6,
                status=Status.StatusCode.Name(span.status.code), attrs={a.key:simple(a.value) for a in span.attributes},
                events=[e.name for e in span.events])


@contextmanager
def service(rollout=None):
    received=[]
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/v1/traces":
                self.send_error(404)
                return
            data=ExportTraceServiceRequest.FromString(self.rfile.read(int(self.headers["Content-Length"])))
            received.extend(unpack(s) for r in data.resource_spans for scope in r.scope_spans for s in scope.spans)
            self.send_response(200)
            self.end_headers()

        def log_message(self,*args):
            pass

    with TemporaryDirectory() as temp, ThreadingHTTPServer(("127.0.0.1",0),Receiver) as receiver, socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        url=f"http://127.0.0.1:{sock.getsockname()[1]}"
        os.environ.update(TRIAGE_MODE="fixture",TRIAGE_KEYS="fixture-demo",CLIENT_RPS="12",STATE_DIR=temp,
                          OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=f"http://127.0.0.1:{receiver.server_port}/v1/traces",
                          FIXTURE_FAULT_FILE=str(Path(temp)/"fault.json"),ROLLOUT_PATH=str(Path(temp)/"rollout.json"),DRILL_URL=url)
        write(os.environ["ROLLOUT_PATH"], rollout or json.loads(Path("config/rollout.json").read_text()))
        from app.main import app
        from app.observe import provider
        server=uvicorn.Server(uvicorn.Config(app,access_log=False,log_level="warning"))
        otlp=threading.Thread(target=receiver.serve_forever,daemon=True)
        api=threading.Thread(target=server.run,kwargs={"sockets":[sock]},daemon=True)
        otlp.start()
        api.start()
        try:
            for _ in range(200):
                if server.started:
                    break
                time.sleep(.05)
            if not server.started:
                raise RuntimeError("API startup timed out")
            yield url, received
        finally:
            provider.force_flush()
            server.should_exit=True
            api.join(5)
            receiver.shutdown()
            otlp.join(3)


async def load(url, seconds=15):
    async with httpx.AsyncClient(base_url=url,headers={"Authorization":"Bearer fixture-demo"},trust_env=False) as client:
        async def one():
            start=time.perf_counter()
            resp=await client.post("/triage",json={"text":"How can I track my order?"})
            return resp.status_code,(time.perf_counter()-start)*1000
        await one()
        results=[]
        for rate in (2,6,12,24):
            await asyncio.sleep(1.1)
            start=time.perf_counter()
            pending=[]
            for index in range(seconds*rate):
                await asyncio.sleep(max(0,start+index/rate-time.perf_counter()))
                pending.append(asyncio.create_task(one()))
            rows=await asyncio.gather(*pending)
            elapsed=max(seconds,time.perf_counter()-start)
            counts=Counter(status for status,_ in rows)
            durations=[ms for _,ms in rows]
            ok=[ms for status,ms in rows if status==200]
            results.append(dict(target_rps=rate,n=len(rows),elapsed_s=elapsed,achieved_rps=len(rows)/elapsed,status=counts,
                         all_p50_ms=quantile(durations,.5),all_p95_ms=quantile(durations,.95),all_p99_ms=quantile(durations,.99),
                         success_p95_ms=quantile(ok,.95)))
        write("results/fixture/local-load.json",dict(mode="fixture",driver="Python HTTPX open-loop, 1 client, Windows Uvicorn", stages=results))


def failover(url,received):
    from app.observe import provider
    from scripts.drill import injected
    with httpx.Client(base_url=url,headers={"Authorization":"Bearer fixture-demo"},trust_env=False) as c:
        with injected(primary_down=True):
            response=c.post("/triage",json={"text":"Please check my refund"})
        response.raise_for_status()
        assert response.json()["fallback"]
        assert provider.force_flush(5000)
        root=next(s for s in reversed(received) if "failover" in s["events"])
        related=[s for s in received if s["trace_id"]==root["trace_id"]]
        calls=sorted((s for s in related if s["parent"]==root["span_id"] and s["name"].startswith("chat ")),key=lambda s:s["start_ns"])
        if len(calls)!=3 or not calls[-1]["attrs"].get("app.secondary") or calls[-1]["status"]!="STATUS_CODE_OK":
            raise AssertionError([(s["name"], list(s["attrs"]), s["status"]) for s in related])
        write("results/fixture/failover-trace.json",dict(mode="fixture",transport="OTLP HTTP protobuf to local receiver",spans=related,
             response={k:response.json()[k] for k in ("model","first_pass","fallback","prompt_version","git_sha")}))
        print("export verified",root["trace_id"],len(calls),"child calls",flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("task",choices=["trace","load","canary","all"])
    task=parser.parse_args().task
    from scripts.drill import DRILL_ROLLOUT, exercise
    with service(DRILL_ROLLOUT if task in ("canary", "all") else None) as (url,received):
        if task in ("trace","all"):
            failover(url,received)
            time.sleep(5.1)
        if task in ("canary","all"):
            exercise(url)
            print("canary rollback verified",flush=True)
        if task in ("load","all"):
            asyncio.run(load(url))
            print("load sweep complete",flush=True)
