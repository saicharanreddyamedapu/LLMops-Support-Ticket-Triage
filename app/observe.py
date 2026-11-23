import os
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram

provider = TracerProvider(resource=Resource.create({"service.name": "ticket-triage", "openinference.project.name": "ticket-triage"}))
if endpoint := os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint), schedule_delay_millis=500))
trace.set_tracer_provider(provider)
tracer = provider.get_tracer("ticket-triage", schema_url="https://opentelemetry.io/schemas/1.37.0")
REQ = Counter("triage_requests", "Completed requests", ["cohort", "outcome"])
LAT = Histogram("triage_latency_seconds", "API request duration", ["cohort"], buckets=(.01,.025,.04,.05,.075,.1,.15,.25,.5,1,2,4,8,16))
TOK = Counter("triage_tokens", "Usage across model attempts", ["cohort", "direction"])
COST = Counter("triage_cost_usd", "Usage at configured uncached list price", ["cohort"])
FIRST = Counter("triage_first_schema", "First model output validation", ["cohort", "valid"])
FALLBACK = Counter("triage_fallback", "Requests served by secondary", ["cohort"])
ATTEMPTS = Counter("triage_call_errors", "Failed model calls", ["kind"])
UNKNOWN = Counter("triage_unknown_usage", "Dispatched live attempts without reported usage", ["cohort"])

for cohort in ("stable", "candidate"):
    REQ.labels(cohort, "ok")
    FALLBACK.labels(cohort)
    for valid in ("true", "false"):
        FIRST.labels(cohort, valid)
