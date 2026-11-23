import hmac
import json
import logging
import os
import time
from collections import defaultdict, deque

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import gateway, observe as obs, settings
from .schema import Failure, Fault, Served, Ticket

# This service owns the sanitized request span and its child model attempts.
app = FastAPI(title="Support ticket triage", version="1.0",
              telemetry={"tracing":False, "operation_spans":False, "metrics":False, "logs":False})
log = logging.getLogger("uvicorn.error")
windows = defaultdict(deque)


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
    return error("invalid_request", 422)


def error(code, status, version="unavailable", wait=0):
    body = Failure(error=code, prompt_version=version, model="unavailable", git_sha=settings.revision())
    return JSONResponse(body.model_dump(), status_code=status, headers={"Retry-After":str(int(wait))} if wait else {})


@app.get("/healthz")
def health():
    return {"status":"ok"}


@app.get("/readyz")
def ready():
    try:
        settings.client_keys()
        r = settings.rollout()
        for role in ("stable","candidate"):
            settings.prompt(r[role])
        if settings.MODE == "live" and (not all(os.getenv(k) for k in ("OPENAI_API_KEY","ANTHROPIC_API_KEY")) or
                                        float(os.getenv("LIVE_BUDGET_USD","0")) <= 0):
            raise ValueError("live configuration")
    except (OSError, ValueError, KeyError):
        return error("configuration", 503)
    return {"status":"ready", "mode":settings.MODE}


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.middleware("http")
async def instrument(request: Request, call_next):
    if request.url.path != "/triage":
        return await call_next(request)
    start = time.monotonic()
    cohort = "unassigned"
    version = model = "unavailable"
    outcome = "internal"
    with obs.tracer.start_as_current_span("POST /triage", attributes={"openinference.span.kind":"CHAIN"}, record_exception=False) as span:
        try:
            authorize(request)
            cohort, cfg = settings.choose()
            version = cfg["version"]
            request.state.routing = (cohort, cfg, span)
            response = await call_next(request)
            outcome = getattr(request.state, "outcome", "invalid_request")
            model = getattr(request.state, "model", model)
        except Fault as exc:
            outcome = exc.code
            response = error(outcome, exc.status, version, exc.retry_after)
        except Exception:
            response = error(outcome, 500, version)
        duration = time.monotonic() - start
        attrs = dict(prompt_version=version, model=model, git_sha=settings.revision(), cohort=cohort,
                     outcome=outcome, duration_ms=round(duration*1000,2), mode=settings.MODE)
        obs.REQ.labels(cohort,outcome).inc()
        obs.LAT.labels(cohort).observe(duration)
        span.set_attributes({"app."+k:v for k,v in attrs.items()})
        span.set_status(Status(StatusCode.OK if outcome == "ok" else StatusCode.ERROR))
        log.info(json.dumps(attrs))
        return response


@app.post("/triage", response_model=Served, responses={c:{"model":Failure} for c in (401,413,422,429,502,503,504)})
async def triage(ticket: Ticket, request: Request):
    cohort, cfg, span = request.state.routing
    try:
        result = await gateway.infer(ticket.text, cfg, cohort, span, single=os.getenv("SINGLE_PROVIDER") == "1")
    except Fault as exc:
        request.state.outcome = exc.code
        return error(exc.code, exc.status, cfg["version"], exc.retry_after)
    request.state.model, request.state.outcome = result["model"], "ok"
    return result
