import asyncio
import json
import time
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import gateway, observe, settings, storage
from app.main import app, windows
from app.schema import Fault
from scripts.dataset import reviews
from scripts.gate import compare

client = TestClient(app)
spans = InMemorySpanExporter()
observe.provider.add_span_processor(SimpleSpanProcessor(spans))


@pytest.fixture(autouse=True)
def isolate(monkeypatch,tmp_path):
    from scripts.common import write
    monkeypatch.setattr(settings,"MODE","fixture")
    monkeypatch.setenv("TRIAGE_KEYS","test-client,second-client")
    monkeypatch.setenv("ROLLOUT_PATH",str(tmp_path/"rollout.json"))
    write(tmp_path/"rollout.json",{"stable":"v1","candidate":"v2","percent":0})
    monkeypatch.setenv("FIXTURE_FAULT_FILE",str(tmp_path/"fault.json"))
    monkeypatch.setenv("STATE_DIR",str(tmp_path))
    gateway.breaker.states.clear()
    windows.clear()
    spans.clear()


def post(ticket="check my refund", key="test-client"):
    return client.post("/triage",json={"text":ticket},headers={"Authorization":"Bearer "+key})


def test_auth_schemas_and_probes():
    assert client.get("/healthz").is_success and client.get("/readyz").is_success
    assert post(key="wrong").status_code == 401
    assert post(" ").status_code == 422
    assert post("x"*4900).status_code == 413
    result = post().json()
    assert result["category"] == "REFUND" and result["model"].startswith("fixture/openai/")
    assert result["prompt_version"] == "v1" and result["git_sha"]


def test_rate_limit_per_client(monkeypatch):
    monkeypatch.setenv("CLIENT_RPS","1")
    assert post().is_success
    assert post().status_code == 429 and post(key="second-client").is_success


@pytest.mark.parametrize("fault,status,calls", [("repair",200,2),("invalid",502,2),("primary_down",200,3)])
def test_fault_paths(fault,status,calls,tmp_path):
    (tmp_path/"fault.json").write_text(json.dumps({fault:True}))
    result = post()
    assert result.status_code == status
    children = [s for s in spans.get_finished_spans() if s.name.startswith("chat ")]
    assert len(children) == calls
    if fault == "repair":
        assert not result.json()["first_pass"]
    if fault == "primary_down":
        assert result.json()["fallback"] and children[-1].attributes["app.secondary"]
        assert children[-1].status.is_ok
        assert len({s.context.trace_id for s in children}) == 1
        roots = [s for s in spans.get_finished_spans() if s.name == "POST /triage"]
        assert len(roots) == 1
        assert all(s.parent.span_id == roots[0].context.span_id for s in children)
        assert post().is_success
        assert len([s for s in spans.get_finished_spans() if s.name.startswith("chat ")]) == 4
    if fault == "invalid":
        assert result.json()["error"] == "schema_fail"


def test_no_request_body_or_secret_in_traces_and_logs(caplog):
    raw = "ticket for private.recipient@example.com at 202-555-0132"
    result = post(raw)
    assert "[REDACTED]" in result.json()["summary"]
    output = caplog.text + "".join(s.to_json() for s in spans.get_finished_spans())
    for forbidden in (raw,"private.recipient","202-555","test-client"):
        assert forbidden not in output


@pytest.mark.parametrize("fault,count", [("provider_request",1),("timeout",4),("provider_auth",2)])
def test_retry_classes(monkeypatch,fault,count):
    calls = []
    async def fail(model,*args):
        calls.append(model)
        raise Fault(fault,502)
    monkeypatch.setattr(gateway,"dispatch",fail)
    assert post().status_code == 502 and len(calls)==count


def test_long_retry_after_switches_without_retry(monkeypatch):
    original = gateway.dispatch
    models = []
    async def busy(model,*args):
        models.append(model)
        if model.startswith("openai/"):
            raise Fault("provider_rate_limit",503,120)
        return await original(model,*args)
    monkeypatch.setattr(gateway,"dispatch",busy)
    assert post().json()["fallback"] and len(models)==2
    assert post().json()["fallback"] and len(models)==3
    assert gateway.retry_delay("7") == 7
    assert 3 < gateway.retry_delay(format_datetime(datetime.now(timezone.utc)+timedelta(seconds=6))) <= 6


def test_breaker_half_open():
    b = gateway.Breaker()
    b.failure("m",0)
    b.failure("m",0)
    assert not b.available("m")
    b.states["m"]=(2,time.monotonic()-1)
    assert b.available("m") and not b.available("m")


def test_deadline(monkeypatch):
    monkeypatch.setattr(gateway,"DEADLINE",.005)
    assert post().json()["error"] == "deadline"


def test_budget_and_replay(monkeypatch):
    with pytest.raises(Fault):
        storage.reserve(.01)
    monkeypatch.setenv("LIVE_BUDGET_USD",".02")
    row = storage.reserve(.015)
    with pytest.raises(Fault):
        storage.reserve(.01)
    storage.settle(row,.001)
    storage.reserve(.01)
    cfg=settings.prompt("v1")
    messages=[{"role":"user","content":"ticket"}]
    k=storage.cache_key(cfg["primary"],messages,cfg)
    assert k != storage.cache_key(cfg["primary"],messages,settings.prompt("v2"))
    storage.cached(k,{"model":"saved"})
    monkeypatch.setattr(settings,"MODE","replay")
    assert asyncio.run(gateway.dispatch(cfg["primary"],messages,cfg,"stable"))["model"] == "saved"


def test_frozen_split():
    rows,_=reviews()
    dev,test=({r["id"] for r in rows if r["split"]==s} for s in ("dev","test"))
    assert len(test)==len(dev)==132 and not dev & test


def test_gate_rejects_incomparable_and_degraded():
    base=dict(mode="fixture",method="test",dataset_digest="a",revision="a",split="test",n=10,reviewed=0,cache="bypass",
              category=1,product=1,priority=None,first_pass=1,availability=1,p95_ms=10,priced_usd_per_request=1)
    for key in ("category","product","p95_ms","priced_usd_per_request"):
        assert compare(base,dict(base,**{key:.96 if key in ("product","category") else 1.2*base[key]}))[0]
    with pytest.raises(ValueError):
        compare(base,dict(base,dataset_digest="b"))


def test_rollout_preserves_file_mode_and_other_fields(tmp_path):
    from scripts.rollout import weight
    path = tmp_path / "rollout.json"
    path.chmod(0o644)
    mode = path.stat().st_mode
    weight(75)
    assert path.stat().st_mode == mode
    assert json.loads(path.read_text()) == {"stable":"v1","candidate":"v2","percent":75}


def test_readiness_rejects_nonfinite_budget(monkeypatch):
    monkeypatch.setattr(settings, "MODE", "live")
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, "dummy")
    monkeypatch.setenv("LIVE_BUDGET_USD", "nan")
    assert client.get("/readyz").status_code == 503


def test_error_is_attributed_to_admitted_cohort(tmp_path):
    (tmp_path / "fault.json").write_text('{"invalid":true}')
    response = post()
    assert response.status_code == 502 and response.headers["X-Triage-Cohort"] == "stable"


def test_late_success_preserves_concurrent_retry_after(monkeypatch):
    cfg = settings.prompt("v1")
    async def overlapping_call(model, *args):
        gateway.breaker.failure(model, 120)
        return {"model":model}
    monkeypatch.setattr(gateway, "call", overlapping_call)
    with observe.tracer.start_as_current_span("test") as span:
        asyncio.run(gateway.route([], cfg, "stable", span, single=True))
    gateway.breaker.failure(cfg["primary"], 1)
    assert gateway.breaker.states[cfg["primary"]][1] > time.monotonic() + 110


def test_live_gate_requires_labels_and_complete_usage():
    base = json.loads(settings.ROOT.joinpath("results/fixture/baseline.json").read_text())
    base.update(mode="live", unknown_usage_attempts=0)
    with pytest.raises(ValueError, match="priority reviews"):
        compare(base, base)
    base.update(human_review_total=200, reviewed=100, priority=.8, judge_n=base["n"], draft_reply_judge=.9)
    with pytest.raises(ValueError, match="complete usage"):
        compare(base, dict(base, unknown_usage_attempts=1))
    assert compare(base, dict(base, draft_reply_judge=.7))[0]


@pytest.mark.parametrize(("metric", "value"), [
    ("availability", .90), ("first_pass", .95), ("p95_ms", 80), ("priced_usd_per_request", .0002),
])
def test_canary_decision(metric, value):
    from scripts.drill import breaches
    stable = dict(n=30, availability=1, first_pass=1, p95_ms=50, priced_usd_per_request=.00015)
    assert breaches(stable, dict(stable, **{metric:value})) == [metric]
    with pytest.raises(ValueError, match="insufficient"):
        breaches(stable, dict(stable, n=24))


def test_provider_http_adapters_with_loopback_servers(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    paths = []
    primary_status = 429
    answer = json.dumps(dict(category="ORDER", product="storefront", priority="normal",
                             summary="Order tracking", draft_reply="Please check the order page."))
    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            paths.append(self.path)
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "chat/completions" in self.path:
                self.send_response(primary_status)
                if primary_status == 200:
                    body = {"id":"chat_test","object":"chat.completion","created":1,"model":payload["model"],
                            "choices":[{"index":0,"message":{"role":"assistant","content":answer},"finish_reason":"stop"}],
                            "usage":{"prompt_tokens":30,"completion_tokens":24,"total_tokens":54}}
                elif primary_status == 429:
                    self.send_header("Retry-After", "90")
                    body = {"error":{"message":"fixture busy","type":"rate_limit_error"}}
                else:
                    body = {"error":{"message":"invalid request","type":"invalid_request_error"}}
            else:
                self.send_response(200)
                body = {"type":"message","id":"msg_test","model":payload["model"],"role":"assistant",
                        "content":[{"type":"text","text":answer}],"stop_reason":"end_turn",
                        "usage":{"input_tokens":30,"output_tokens":24}}
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Upstream) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        complete = gateway.litellm.acompletion
        async def use_loopback(**kwargs):
            origin = f"http://127.0.0.1:{server.server_port}"
            base = origin + "/v1" if kwargs["model"].startswith("openai/") else origin
            return await complete(**kwargs, api_base=base, api_key="test-local-only")
        monkeypatch.setattr(gateway.litellm, "acompletion", use_loopback)
        monkeypatch.setattr(settings, "MODE", "live")
        monkeypatch.setenv("LIVE_BUDGET_USD", "0.01")  # Isolated test ledger and loopback providers
        try:
            response = post()
            assert response.is_success, (response.json(), paths)
            assert response.json()["fallback"] and response.json()["model"].startswith("claude-")
            assert len(paths) == 2 and "chat/completions" in paths[0] and "/messages" in paths[1]
            gateway.breaker.states.clear()
            paths.clear()
            primary_status = 400
            assert post().json()["error"] == "provider_request"
            assert paths == ["/v1/chat/completions"]
            primary_status = 200
            assert post().json()["fallback"] is False
            monkeypatch.setenv("LIVE_BUDGET_USD","0")
            paths.clear()
            assert post().json()["error"] == "budget"
            assert paths == []
        finally:
            server.shutdown()
            thread.join()
