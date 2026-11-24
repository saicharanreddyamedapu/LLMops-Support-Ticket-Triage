import hmac
import json
import logging
import math
import os
import time
from collections import defaultdict, deque

import httpx
from fastapi import FastAPI, Request, Security
from fastapi.security import HTTPBearer
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import gateway, observe as obs, settings
from .schema import Failure, Fault, Served, Ticket

# This service owns the sanitized request span and its child model attempts.
app = FastAPI(title="LLMops Support Ticket Triage", version="1.0",
              telemetry={"tracing":False, "operation_spans":False, "metrics":False, "logs":False})
log = logging.getLogger("uvicorn.error")
windows = defaultdict(deque)
ticket_security = HTTPBearer(auto_error=False, description="Enter the local triage access key.")


def authorize(request):
    value = request.headers.get("authorization", "")
    keys = settings.client_keys()
    client = next((i for i, key in enumerate(keys) if hmac.compare_digest(value.encode(), ("Bearer " + key).encode())), None)
    if client is None:
        raise Fault("unauthorized", 401)
    now = time.monotonic()
    window = windows[client]
    while window and window[0] <= now - 1:
        window.popleft()
    if len(window) >= int(os.getenv("CLIENT_RPS", "12")):
        raise Fault("client_rate_limit", 429, 1)
    window.append(now)


@app.exception_handler(RequestValidationError)
async def invalid(request, exc):
    routing = getattr(request.state, "routing", None)
    return error("invalid_request", 422, routing[1]["version"] if routing else "unavailable")


def error(code, status, version="unavailable", wait=0):
    body = Failure(error=code, prompt_version=version, model="unavailable", git_sha=settings.revision())
    return JSONResponse(body.model_dump(), status_code=status, headers={"Retry-After":str(int(wait))} if wait else {})


@app.get("/healthz")
def health():
    return {"status":"ok"}


def local_model_ready(config):
    with httpx.Client(trust_env=False, timeout=2) as client:
        response = client.get("http://127.0.0.1:11434/api/tags")
        response.raise_for_status()
        name = config["primary"].split("/", 1)[1]
        return any(m.get("name") == name and m.get("digest") == config["local_digest"]
                   for m in response.json()["models"])


@app.get("/readyz")
def ready():
    try:
        settings.client_keys()
        r = settings.rollout()
        for role in ("stable","candidate"):
            settings.prompt(r[role])
        if settings.MODE == "local" and not local_model_ready(settings.prompt(r["stable"])):
            raise ValueError("pinned local model unavailable")
        if int(os.getenv("CLIENT_RPS", "12")) < 1:
            raise ValueError("invalid client rate limit")
        if settings.MODE == "live":
            budget = float(os.getenv("LIVE_BUDGET_USD", "0"))
            if not math.isfinite(budget) or budget <= 0 or not all(os.getenv(k) for k in ("OPENAI_API_KEY","ANTHROPIC_API_KEY")):
                raise ValueError("live configuration")
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        return error("configuration", 503)
    return {"status":"ready", "mode":settings.MODE}


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/rollout/status")
def rollout_status():
    """Show the active prompt rollout and candidate traffic after rollback."""
    plan = settings.rollout()
    marker = settings.rollback_marker(plan)
    rolled_back = marker is not None and marker.exists()
    return dict(stable=plan["stable"], candidate=plan["candidate"],
                candidate_percent=0 if rolled_back else plan["percent"],
                automatic_rollback=marker is not None, rolled_back=rolled_back)


@app.middleware("http")
async def instrument(request: Request, call_next):
    if request.url.path not in {"/triage", "/practice/track-order"}:
        response = await call_next(request)
        response.headers["X-Triage-Prompt-Version"] = "not-used"
        response.headers["X-Triage-Model"] = "not-used"
        response.headers["X-Triage-Git-Sha"] = settings.revision()
        return response
    start = time.monotonic()
    cohort = "unassigned"
    version = model = "unavailable"
    outcome = "internal"
    with obs.tracer.start_as_current_span("POST /triage", attributes={"openinference.span.kind":"CHAIN"}, record_exception=False) as span:
        try:
            authorize(request)
            if request.url.path == "/practice/track-order":
                if settings.MODE != "local" or not os.getenv("LLMOPS_GROQ_API_KEY"):
                    raise Fault("practice_unavailable", 503)
                cohort = "practice"
                cfg = dict(settings.prompt("v3"), secondary="groq/openai/gpt-oss-20b")
            else:
                plan = settings.rollout()
                cohort, cfg = settings.choose(plan)
            version = cfg["version"]
            request.state.routing = (cohort, cfg, span)
            response = await call_next(request)
            response.headers["X-Triage-Cohort"] = cohort
            outcome = getattr(request.state, "outcome", "invalid_request")
            model = getattr(request.state, "model", model)
        except Fault as exc:
            outcome = exc.code
            response = error(outcome, exc.status, version, exc.retry_after)
        except Exception:
            response = error(outcome, 500, version)
        if cohort == "candidate" and response.status_code >= 500:
            marker = settings.rollback_marker(plan)
            if marker is not None:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
        duration = time.monotonic() - start
        attrs = dict(prompt_version=version, model=model, git_sha=settings.revision(), cohort=cohort,
                     outcome=outcome, duration_ms=round(duration*1000,2), mode=settings.MODE)
        obs.REQ.labels(cohort,outcome).inc()
        obs.LAT.labels(cohort).observe(duration)
        span.set_attributes({"app."+k:v for k,v in attrs.items()})
        span.set_status(Status(StatusCode.OK if outcome == "ok" else StatusCode.ERROR))
        response.headers["X-Trace-Id"] = format(span.get_span_context().trace_id, "032x")
        response.headers["X-Triage-Prompt-Version"] = version
        response.headers["X-Triage-Model"] = model
        response.headers["X-Triage-Git-Sha"] = attrs["git_sha"]
        log.info(json.dumps(attrs))
        return response


@app.post("/triage", response_model=Served, dependencies=[Security(ticket_security)],
          responses={c:{"model":Failure} for c in (401,413,422,429,502,503,504)})
async def triage(ticket: Ticket, request: Request):
    cohort, cfg, span = request.state.routing
    try:
        result = await gateway.infer(ticket.text, cfg, cohort, span, single=os.getenv("SINGLE_PROVIDER") == "1")
    except Fault as exc:
        request.state.outcome = exc.code
        return error(exc.code, exc.status, cfg["version"], exc.retry_after)
    if cfg.get("product_from_category"):
        result["product"] = settings.CATALOG[result["category"]]
    request.state.model, request.state.outcome = result["model"], "ok"
    return result


@app.post("/practice/track-order", response_model=Served, dependencies=[Security(ticket_security)],
          responses={401: {"model": Failure}})
async def practice_track_order(request: Request):
    """Try local Qwen first, with Groq backup for this fixed synthetic ticket."""
    return await triage(Ticket(text="How can I track my order?"), request)
