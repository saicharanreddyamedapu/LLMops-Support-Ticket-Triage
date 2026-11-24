import json
import os
import random
import re
import subprocess
from functools import lru_cache
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


def choose():
    r = rollout()
    cohort = "candidate" if random.randrange(100) < r["percent"] else "stable"
    return cohort, prompt(r[cohort])


def client_keys():
    raw = os.getenv("TRIAGE_KEYS", "fixture-demo" if MODE == "fixture" else "")
    keys = [x.strip() for x in raw.split(",") if x.strip()]
    if not keys or (MODE != "fixture" and "fixture-demo" in keys):
        raise ValueError("configure TRIAGE_KEYS")
    return keys
