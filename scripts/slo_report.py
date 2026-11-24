"""Measure seven-day request availability and the visible scrape history for one API."""
import argparse
import time
from datetime import datetime, timezone

import httpx

from scripts.common import write

MINUTES = 7 * 24 * 60
ORIGINS = {"local": "host.docker.internal:8766", "fixture": "api:8000"}


def query(client, endpoint, expression, **params):
    response = client.get("/api/v1/" + endpoint, params={"query": expression, **params})
    response.raise_for_status()
    payload = response.json()
    if payload["status"] != "success":
        raise ValueError(payload.get("error", "Prometheus query failed"))
    return payload["data"]["result"]


def availability_report(client, mode, now):
    end = int(now) // 60 * 60
    start = end - 60 * MINUTES
    origin = ORIGINS[mode]
    selector = f'instance="{origin}",job="triage"'
    # Each evaluation samples the preceding non-overlapping minute. Missing
    # buckets remain missing rather than turning into successful scrapes.
    history = query(client, "query_range", f"min(min_over_time(up{{{selector}}}[1m]))",
                    start=start+60, end=end, step=60)
    samples = history[0]["values"] if history else []
    down = sum(float(value) == 0 for _, value in samples)

    def increase(extra):
        expr = f'sum(increase(triage_requests_total{{{selector},cohort!="unassigned"{extra}}}[7d]))'
        result = query(client, "query", expr, time=end)
        return float(result[0]["value"][1]) if result else None

    eligible, successful = increase(""), increase(',outcome="ok"')
    availability = successful / eligible if eligible and successful is not None else None
    missing = MINUTES - len(samples)
    if missing or down:
        status = "incomplete_scrape_history"
    elif availability is None:
        status = "no_measurable_requests"
    else:
        status = "met" if availability >= .99 else "missed"

    return dict(mode=mode, instance=origin, target_availability=.99, window_days=7,
                window_start_utc=datetime.fromtimestamp(start,timezone.utc).isoformat(),
                window_end_utc=datetime.fromtimestamp(end,timezone.utc).isoformat(),
                expected_minutes=MINUTES, observed_minutes=len(samples), missing_minutes=missing,
                minutes_with_failed_scrape=down, estimated_eligible_requests=eligible,
                estimated_successful_requests=successful, observed_availability=availability,
                status=status)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=ORIGINS, default="local")
    parser.add_argument("--output")
    args = parser.parse_args()
    with httpx.Client(base_url="http://127.0.0.1:19090",trust_env=False,timeout=30) as client:
        report = availability_report(client,args.mode,time.time())
    write(args.output or f"results/{args.mode}/slo-window.json", report)
    print(f"{report['instance']}  {report['observed_minutes']}/{MINUTES} minutes observed  {report['status']}")
