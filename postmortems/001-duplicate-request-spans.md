# Duplicate request spans hid the parent of model attempts

**Impact.** On 24 November 2026 IST, the first local OTLP integration assertion failed for a successful fixture failover. The API returned a valid response. A trace consumer looking for model attempts directly under the application's request span could not find the expected three children. No customer traffic or paid provider was involved.

|Time (IST, approximate)|Observation|
|---|---|
|00:36|The in-memory unit suite passed. It checked the shared trace ID and successful secondary, without checking parent IDs.|
|00:42–00:44|A real HTTP request exported over OTLP produced two request spans plus FastAPI dependency, endpoint and serialization spans. Inspection showed the LLM spans were nested under framework instrumentation.|
|00:44–00:45|Disabled FastAPI's overlapping native telemetry for this service and retained explicit sanitized request/model instrumentation. Added assertions for one root and every model span's direct parent.|
|After correction|The receiver decoded one application root, two failed primary calls and an OK secondary call in the same trace. The 15-test suite passed.|

**Root cause.** The locked FastAPI version enables native tracing when a global OpenTelemetry provider exists. The service also created its own span. The original assertion for a shared trace ID did not detect the extra parent.

**Verification.** Run `python -m scripts.local_lab trace` and `python -m pytest -q`. The [captured trace](../results/fixture/failover-trace.json) contains status, parent IDs, timings and attempt events. Rerun this integration assertion after upgrades that affect tracing. The fault that exposed this instrumentation defect was injected. The duplicate instrumentation was a real build defect.
