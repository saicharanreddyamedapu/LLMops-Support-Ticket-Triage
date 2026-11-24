# Operating ticket triage

## Initial checks

GitHub dispatches verification to a repository runner labeled `triage-verification`. The local runner image is defined in `ops/runner.Dockerfile`; it includes promtool for the alert tests. The dedicated Docker runner has no host directory mounts or Docker socket. If checks queue, check Docker Desktop and the repository's Actions runner status. The staging publisher runs separately and checks only the verified main revision.

The separate local staging publisher runs from the checkout with `python -m scripts.local_staging --watch`. It checks the GitHub main verification every two minutes, builds the accepted Git archive, pushes a SHA-tagged image to the registry on loopback port 15000, then validates a trial container before updating staging on port 8767. It serves fixture traffic with no hosted provider calls. The deployment receipt is `.local/staging/deployment.json`. Its two Docker services are independent of the Compose and Qwen services. Run only one publisher from a checkout. Repository polling uses the Git credential helper for the configured GitHub HTTPS origin.

The Collector scrapes the API's Prometheus endpoint every two seconds and publishes those series on its internal port 8889. Prometheus scrapes `collector:8889`. If the target is healthy but a panel is empty, inspect the Collector's upstream scrape errors and verify that the API itself exposes the expected series.

Run `docker compose ps`, then open `http://127.0.0.1:8765/readyz`, `http://127.0.0.1:19090/targets`, the [Grafana dashboard](http://127.0.0.1:13000/d/triage) and [Phoenix](http://127.0.0.1:16006). The configured service runs one worker. Rate limits and breakers are process-local. A shared rate limiter and replicated budget ledger are prerequisites for multi-host operation.

Use the request timestamp, prompt version, model and Git revision to correlate symptoms. Copy only sanitized request metadata. Trace IDs identify single requests. A ready result verifies configuration. Provider availability requires a successful call that reports the serving model.

## TriageFastBurn

For a prospective seven-day local observation, set `TRIAGE_KEYS` to the local service's client key and run `python -m scripts.availability_watch` from the project root. It sends one authenticated synthetic ticket per hour to the loopback Qwen service, validates the response, and stores each outcome under `.local/availability-watch/`. The observer fixes the window at startup, resumes from its SQLite journal, and leaves missed hours visible. Keep the machine, Docker, Qwen and the observer running during the window. Read the latest status with `python -m scripts.availability_watch --status`. A final pass requires both the 99% hourly probe target and complete per-instance Prometheus evidence. This measures synthetic availability on this host.

Select the alert's API instance in Grafana. Run `python -m scripts.slo_report --mode local` for the Qwen host or `--mode fixture` for Compose. Each report checks 10,080 one-minute buckets ending at the latest completed UTC minute. A bucket is observed when Prometheus has an upstream `up` sample for that API. Missing buckets or observed failed scrapes mark the history incomplete. A successful scrape samples the service at one instant. Request volumes are Prometheus counter-increase estimates and can be fractional. An idle instance can have scrape coverage and no measurable request availability.

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
4. The local staging publisher mounts a separate rollout directory for each deployed revision. To roll back staging in PowerShell, use the commands below from the project root. The container rereads that file for each request. Inspect the next response headers and the mounted file to confirm recovery.
5. Stop the local stack with `docker compose stop`. Restart with one `docker compose up -d --wait` and inspect `docker compose logs --tail 50` if a container is unhealthy. Keep volumes during diagnosis.

```powershell
$revision = (Get-Content .local/staging/deployment.json -Raw | ConvertFrom-Json).git_sha
python -m scripts.rollout 0 --path ".local/staging/rollouts/$revision/rollout.json"
```

For a rehearsal, run `python -m scripts.drill`. This starts an isolated fixture instance on an ephemeral port. It evaluates v1 and v2, applies the canary policy, and saves the measured rollback to `.local/canary.json`.

## Live evaluation and two-provider drill

For the zero-provider-fee local model, start the host API using the README. A 503 from its readiness endpoint means it cannot verify the configured Ollama tag and digest or another runtime setting is invalid. Check `ollama list`, the registry digest, the loopback listener on 11434 and the local client key. Check port 14318 on the Collector for missing host traces. Use `python -m scripts.load --mode local` for the short host capacity sweep. Run benchmarks and load tests sequentially on a single GPU.

Approve a destination and budget first. Recheck the official model lists, prices, regional availability and account access. Set provider-side spending controls where supported. Configure TRIAGE_MODE, TRIAGE_KEYS, OPENAI_API_KEY, ANTHROPIC_API_KEY, LIVE_BUDGET_USD and STATE_DIR using the secret store. Use an isolated persistent budget ledger for the approved run. Keep RECORD_REPLAY=0 when measuring latency or spend.

Run `python -m scripts.dataset validate`; obtain at least 200 actual reviews. Evaluate the two prompt versions separately with `python -m scripts.evaluate --mode live --version v1 --judge --output results/live/baseline.json` and then v2 with `--output results/live/current.json`. Judge usage is reported separately. Calibrate the judge against human ratings before using it as a release criterion. Compare the matching reports with `python -m scripts.gate`. Reconcile amounts with billing and keep the fixture evidence intact.

For failover, use an isolated live staging process at 0% canary. Verify one request on the primary. Temporarily configure an invalid primary key on this isolated process, restart it, and send one approved synthetic ticket. A primary auth error should route directly to the second provider. Verify actual model, token usage, OK secondary span, return the valid key, restart and confirm recovery. Never rotate or revoke a production credential for this drill. Stop when the approved budget is exhausted.
### Local automatic rollback

Start the API with `AUTO_ROLLBACK=1` and a persistent writable `STATE_DIR`.
Check `GET /rollout/status` for the stable prompt, candidate prompt, effective candidate percentage and rollback state.
During a canary, a candidate response with HTTP 5xx stops subsequent candidate traffic. In-flight calls finish normally.
The stop persists across process restarts and traffic percentage changes. Client errors and practice requests do not trigger it.
After correcting the candidate and passing its release checks, set a fresh `rollout_id` in the rollout configuration for the next attempt.
Latency, cost and quality regressions still use the existing thresholds and operator rollback procedure.
