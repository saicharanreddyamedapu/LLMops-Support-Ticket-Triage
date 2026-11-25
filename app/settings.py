import json
import hashlib
import os
import random
from collections import deque
from functools import lru_cache
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / "config/catalog.json").read_text())
PRICES = json.loads((ROOT / "config/prices.json").read_text())["models"]
MODE = os.getenv("TRIAGE_MODE", "fixture")
if MODE not in {"live", "fixture", "replay", "local"}:
    raise ValueError("invalid TRIAGE_MODE")


@lru_cache
def revision():
    if os.getenv("GIT_SHA"):
        return os.environ["GIT_SHA"]
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


@lru_cache
def prompt(version):
    if not re.fullmatch(r"v[0-9]+", version):
        raise ValueError("invalid prompt version")
    p = yaml.safe_load((ROOT / f"prompts/triage/{version}.yaml").read_text())
    if p["version"] != version or p["primary"].split("/")[0] == p["secondary"].split("/")[0]:
        raise ValueError("invalid prompt registry")
    if any(p[k] not in PRICES for k in ("primary", "secondary")) or not 1 <= p["params"]["max_tokens"] <= 512:
        raise ValueError("missing price or unsafe output cap")
    if MODE == "local" or (MODE == "replay" and os.getenv("REPLAY_SOURCE", "live") == "local"):
        from .schema import Triage
        local = yaml.safe_load((ROOT / "prompts/local.yaml").read_text())
        p.update(primary=local["model"], secondary=local["model"], local_digest=local["digest"])
        p["params"].update(local["params"])
        p["params"]["response_format"] = {"type":"json_schema", "json_schema":{"name":"triage","schema":Triage.model_json_schema()}}
    return p


def rollout():
    r = json.loads(Path(os.getenv("ROLLOUT_PATH", ROOT / "config/rollout.json")).read_text())
    if not 0 <= r["percent"] <= 100:
        raise ValueError("invalid canary share")
    return r


def rollback_marker(r):
    if os.getenv("AUTO_ROLLBACK") != "1":
        return None
    identity = json.dumps([r["stable"], r["candidate"], r.get("rollout_id")]).encode()
    return Path(os.getenv("STATE_DIR", ".local")) / ("rollback-" + hashlib.sha256(identity).hexdigest())


@lru_cache(maxsize=1)
def health_windows(marker):
    return {role: deque(maxlen=20) for role in ("stable", "candidate")}


def observe_route(plan, cohort, status, duration, now):
    marker = rollback_marker(plan)
    if marker is None or cohort not in ("stable", "candidate"):
        return
    if cohort == "candidate" and status >= 500:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return
    if not plan.get("stable_failover") or not 0 < plan["percent"] < 100 or marker.exists():
        return
    if status >= 400 and status < 500:
        return
    windows = health_windows(marker)
    windows[cohort].append((now, status >= 500, duration > 4))
    for samples in windows.values():
        while samples and now - samples[0][0] > 1800:
            samples.popleft()
    candidate = windows["candidate"]
    stable = windows["stable"]
    if len(candidate) < 20 or any(error for _, error, _ in candidate) or sum(slow for _, _, slow in candidate) > 1:
        return
    consecutive_errors = len(stable) >= 3 and all(error for _, error, _ in list(stable)[-3:])
    slow_p95 = len(stable) == 20 and sum(slow for _, _, slow in stable) > 1
    if consecutive_errors or slow_p95:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.with_suffix(".failover").touch()


def effective_percent(r):
    marker = rollback_marker(r)
    if marker and marker.exists():
        return 0
    if marker and r.get("stable_failover") and r["percent"] > 0 and marker.with_suffix(".failover").exists():
        return 100
    return r["percent"]


def choose(r=None):
    r = rollout() if r is None else r
    cohort = "candidate" if random.randrange(100) < effective_percent(r) else "stable"
    return cohort, prompt(r[cohort])


def client_keys():
    raw = os.getenv("TRIAGE_KEYS", "fixture-demo" if MODE == "fixture" else "")
    keys = [x.strip() for x in raw.split(",") if x.strip()]
    if not keys or (MODE != "fixture" and "fixture-demo" in keys):
        raise ValueError("configure TRIAGE_KEYS")
    return keys
