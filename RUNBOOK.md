# Operating ticket triage

## Initial checks

The Collector scrapes the API's Prometheus endpoint every two seconds and publishes those series on its internal port 8889. Prometheus scrapes `collector:8889`. If the target is healthy but a panel is empty, inspect the Collector's upstream scrape errors and verify that the API itself exposes the expected series.

Run `docker compose ps`, then open `http://127.0.0.1:8765/readyz`, `http://127.0.0.1:19090/targets`, the [Grafana dashboard](http://127.0.0.1:13000/d/triage) and [Phoenix](http://127.0.0.1:16006). The configured service runs one worker. Rate limits and breakers are process-local. A shared rate limiter and replicated budget ledger are prerequisites for multi-host operation.

Use the request timestamp, prompt version, model and Git revision to correlate symptoms. Copy only sanitized request metadata. Trace IDs identify single requests. A ready result verifies configuration. Provider availability requires a successful call that reports the serving model.

## TriageFastBurn

**Meaning.** The accepted-client failure ratio exceeds 14.4 times a 1% seven-day budget in both 5-minute and 1-hour windows for at least one minute. Excluded pre-admission requests still count in errors by class. Early in a new deployment, windows contain only available history.

**Check.** Inspect Prometheus targets and scrape continuity, then group request outcomes and failed attempts by error class. Look at the recent version and secondary percentage. Check the provider timeout, 429, provider 5xx, invalid input and schema failure series. The rate-limit or input cap may be acting as designed. Prometheus contains the rules. Connect an Alertmanager receiver before relying on paging in staging.

**Mitigate.** If the candidate differs, run `python -m scripts.rollout 0` in the checkout whose config directory is mounted. Confirm incoming responses have the stable version. For upstream rate limits, respect Retry-After and inspect whether the secondary succeeds. Shed excess demand with CLIENT_RPS. For schema failures, compare schema and prompt versions on dev examples. Keep one repair limit. If both providers fail, retain the typed failure and protect the budget.

**Escalate.** Contact the staging service owner if both providers fail, if the last rollback fails to restore the SLO, or if burn persists after mitigation. Supply time range, affected versions, error counts, fallback percentage and one trace ID. Add the actual owner and notification destination when staging is assigned.

## TriageCostSpike

**Meaning.** The five-minute average exceeds $0.065/min for two minutes. At five requests/s the fixture baseline estimates $0.0491/min. The threshold has roughly 32% headroom. These are provisional fixture targets, with actual live SLOs pending.

**Check.** Compare admitted request rate, input/output token rates, active model, retries, repairs and fallback. For billing gaps inspect `triage_unknown_usage_total`. Dispatched live calls without confirmed usage retain a conservative reservation. Inspect the provider invoice before releasing a reservation. Replay traffic cannot measure spend.

**Mitigate.** Roll back a more expensive prompt, lower admitted RPS, or stop live traffic. Preserve the SQLite state volume. Adjust a provider-side spending limit only with approval. The application guard rejects a call if its upper-bound reservation would exceed LIVE_BUDGET_USD. Reservations persist across restarts of the same volume.

**Escalate.** If billed amounts differ from the measured list-price estimate, notify the budget owner with dates, model IDs, attempt counts, cached-input status and usage gaps. Confirm provider prices before restoring traffic.

## Rollout, restore and evidence

1. Set the version registry and `config/rollout.json` in Git. Validate on dev, freeze, and run the held-out gate. On staging, use the mounted config directory or an equivalent externally managed config volume.
2. Run `python -m scripts.rollout 10`. The fixture drill evaluates both cohorts after at least 25 observations each. A rollback triggers below 99% schema validity or availability, a one-point relative-to-stable quality drop in either, p95 above 1.5× stable, or unit price above 1.15× stable. The file `config/canary.json` declares the thresholds.
3. Set the weight to zero to roll back. Sample responses and cohort metric deltas. Keep the degraded candidate out until the cause is resolved. The repeatable local drill uses isolated configuration and restores the initial weight on exit.
4. An image-only single container needs external persistent runtime storage and mutable rollout config for equivalent behavior. Configure those before enabling staging deployments.
5. Stop the local stack with `docker compose stop`. Restart with one `docker compose up -d --wait` and inspect `docker compose logs --tail 50` if a container is unhealthy. Keep volumes during diagnosis.

## Live evaluation and two-provider drill

Approve a destination and budget first. Recheck the official model lists, prices, regional availability and account access. Set provider-side spending controls where supported. Configure TRIAGE_MODE, TRIAGE_KEYS, OPENAI_API_KEY, ANTHROPIC_API_KEY, LIVE_BUDGET_USD and STATE_DIR using the secret store. Use an isolated persistent budget ledger for the approved run. Keep RECORD_REPLAY=0 when measuring latency or spend.

Run `python -m scripts.dataset validate`; obtain at least 200 actual reviews. Evaluate the two prompt versions separately with `python -m scripts.evaluate --mode live --version v1 --judge --output results/live/baseline.json` and then v2 with `--output results/live/current.json`. Judge usage is reported separately. Calibrate the judge against human ratings before using it as a release criterion. Compare the matching reports with `python -m scripts.gate`. Reconcile amounts with billing and keep the fixture evidence intact.

For failover, use an isolated live staging process at 0% canary. Verify one request on the primary. Temporarily configure an invalid primary key on this isolated process, restart it, and send one approved synthetic ticket. A primary auth error should route directly to the second provider. Verify actual model, token usage, OK secondary span, return the valid key, restart and confirm recovery. Never rotate or revoke a production credential for this drill. Stop when the approved budget is exhausted.
