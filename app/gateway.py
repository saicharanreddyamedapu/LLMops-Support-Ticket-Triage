import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from importlib.util import find_spec
from pathlib import Path

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
import litellm
from opentelemetry.trace import Status, StatusCode
from pydantic import ValidationError
from tokenizers import Tokenizer

from . import fixture, observe as obs, settings, storage
from .schema import Fault, Triage

litellm.suppress_debug_info = True
litellm.turn_off_message_logging = True
for logger_name in ("LiteLLM", "LiteLLM Proxy", "LiteLLM Router", "httpx", "httpcore", "openai", "anthropic"):
    logging.getLogger(logger_name).disabled = True

TOKENIZER = Tokenizer.from_file(str(Path(find_spec("litellm").origin).parent / "litellm_core_utils/tokenizers/anthropic_tokenizer.json"))
PII = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")
CALL_TIMEOUT = 3.0
DEADLINE = 9.0


def retry_delay(value):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


def normalize(exc):
    if isinstance(exc, Fault):
        return exc
    status = getattr(exc, "status_code", 0)
    if isinstance(exc, (TimeoutError, litellm.Timeout)) or status == 408:
        return Fault("timeout", 504)
    headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
    if status == 429:
        return Fault("provider_rate_limit", 503, retry_delay(headers.get("retry-after")))
    if isinstance(status, int) and 500 <= status < 600:
        return Fault("provider_5xx", 502, retry_delay(headers.get("retry-after")))
    return Fault("provider_auth" if status in (401, 403) else "provider_request", 502)


RETRYABLE = {"timeout", "provider_rate_limit", "provider_5xx"}


class Breaker:
    def __init__(self):
        self.states = {}

    def available(self, model):
        count, until = self.states.get(model, (0, 0))
        if count < 2:
            return True
        if time.monotonic() < until:
            return False
        self.states[model] = (count, time.monotonic() + 5)
        return True

    def failure(self, model, wait):
        count = self.states.get(model, (0, 0))[0] + 1
        self.states[model] = (count, time.monotonic() + max(5, wait))
        if wait:
            self.states[model] = (max(count, 2), time.monotonic() + wait)


breaker = Breaker()


def price(model, tin, tout):
    i, o = settings.PRICES[model]
    return (i*tin + o*tout) / 1_000_000


async def dispatch(model, messages, config, cohort, secondary=False):
    mode = settings.MODE
    params = config["params"]
    # Each UTF-8 byte can be one token. Add slack for provider chat framing.
    upper = sum(len(m["content"].encode()) + 32 for m in messages) + 32
    if upper > 4800:
        raise Fault("input_cap", 413)
    key = storage.cache_key(model, messages, config)
    if mode == "replay":
        if result := storage.cached(key):
            return result
        raise Fault("replay_miss", 503)
    kwargs = {}
    row = None
    if mode == "fixture":
        fault_file = Path(os.getenv("FIXTURE_FAULT_FILE", ".local/fixture-fault.json"))
        faults = json.loads(fault_file.read_text()) if fault_file.is_file() else {}
        if not secondary and faults.get("primary_down"):
            raise Fault("provider_5xx", 502)
        if cohort == "candidate" and faults.get("slow_candidate"):
            await asyncio.sleep(.18)
        output = await fixture.answer(messages[1]["content"], config["version"])
        if faults.get("invalid") or (faults.get("repair") and len(messages) == 2):
            output = "{}"
        tin = sum(len(TOKENIZER.encode(m["content"]).ids) + 4 for m in messages)
        tout = len(TOKENIZER.encode(output).ids)
        kwargs.update(mock_response={"model": "fixture/" + model, "choices": [{"index":0, "message":{"role":"assistant","content":output}, "finish_reason":"stop"}],
                                     "usage":{"prompt_tokens":tin,"completion_tokens":tout,"total_tokens":tin+tout}}, api_key="fixture")
    else:
        row = storage.reserve(price(model, upper, params["max_tokens"]))
    try:
        reply = await litellm.acompletion(model=model, messages=messages, **params, **kwargs,
                                          timeout=CALL_TIMEOUT, num_retries=0, caching=False)
        usage = reply.usage
        value = dict(text=reply.choices[0].message.content or "", model=reply.model or "unavailable",
                     tin=usage.prompt_tokens, tout=usage.completion_tokens, finish=reply.choices[0].finish_reason or "unknown")
        cost = price(model, value["tin"], value["tout"])
        if row is not None:
            storage.settle(row, cost)
            if os.getenv("RECORD_REPLAY") == "1":
                storage.cached(key, value)
        obs.TOK.labels(cohort,"in").inc(value["tin"])
        obs.TOK.labels(cohort,"out").inc(value["tout"])
        obs.COST.labels(cohort).inc(cost)
        return value
    except BaseException:
        if row is not None:
            obs.UNKNOWN.labels(cohort).inc()
        raise


async def call(model, messages, config, cohort, secondary):
    attrs = {"gen_ai.operation.name":"chat", "gen_ai.provider.name":model.split("/")[0],
             "gen_ai.request.model":model, "app.prompt_version":config["version"], "app.mode":settings.MODE,
             "openinference.span.kind":"LLM", "app.secondary":secondary}
    with obs.tracer.start_as_current_span("chat " + model, attributes=attrs, record_exception=False, set_status_on_exception=False) as span:
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                value = await dispatch(model, messages, config, cohort, secondary)
            span.set_attributes({"gen_ai.response.model":value["model"], "gen_ai.usage.input_tokens":value["tin"],
                                 "gen_ai.usage.output_tokens":value["tout"], "gen_ai.response.finish_reasons":[value["finish"]]})
            span.set_status(Status(StatusCode.OK))
            return value
        except Exception as exc:
            fault = normalize(exc)
            span.set_status(Status(StatusCode.ERROR, fault.code))
            span.set_attribute("error.type", fault.code)
            obs.ATTEMPTS.labels(fault.code).inc()
            raise fault from None


async def route(messages, cfg, cohort, root, single):
    last = Fault("circuit_open", 503)
    roles = ("primary",) if single else ("primary", "secondary")
    for role in roles:
        model = cfg[role]
        secondary = role == "secondary"
        if not breaker.available(model):
            root.add_event("circuit_open", {"model":model})
            continue
        if secondary:
            root.add_event("failover", {"model":model})
        for attempt in range(2):
            try:
                value = await call(model, messages, cfg, cohort, secondary)
                breaker.states.pop(model, None)
                return value, secondary
            except Fault as exc:
                last = exc
                if exc.code not in RETRYABLE:
                    if exc.code == "provider_auth":
                        breaker.failure(model, 5)
                        break
                    raise
                breaker.failure(model, exc.retry_after)
                delay = max(exc.retry_after, .04 * 2**attempt + random.uniform(0,.02))
                if attempt or delay > .4:
                    break
                root.add_event("retry", {"delay_s":delay})
                await asyncio.sleep(delay)
    raise last


async def infer(text, cfg, cohort, root, single=False):
    messages = [{"role":"system","content":cfg["system"]}, {"role":"user","content":PII.sub("[REDACTED]", text)}]
    try:
        async with asyncio.timeout(DEADLINE):
            for attempt in range(2):
                value, secondary = await route(messages, cfg, cohort, root, single)
                try:
                    item = Triage.model_validate_json(value["text"])
                except ValidationError:
                    if attempt:
                        raise Fault("schema_fail", 502) from None
                    obs.FIRST.labels(cohort,"false").inc()
                    root.add_event("repair")
                    messages.append({"role":"user","content":cfg["repair"]})
                    continue
                if not attempt:
                    obs.FIRST.labels(cohort,"true").inc()
                if secondary:
                    obs.FALLBACK.labels(cohort).inc()
                return dict(**item.model_dump(), model=value["model"], prompt_version=cfg["version"], git_sha=settings.revision(),
                            mode=settings.MODE, cohort=cohort, first_pass=not attempt, fallback=secondary)
    except TimeoutError:
        raise Fault("deadline", 504) from None
