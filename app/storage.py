import hashlib
import json
import math
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .schema import Fault


def connect():
    p = Path(os.getenv("STATE_DIR", ".local"))
    p.mkdir(exist_ok=True, parents=True)
    db = sqlite3.connect(p / "triage.sqlite", timeout=3)
    db.execute("CREATE TABLE IF NOT EXISTS spend (id INTEGER PRIMARY KEY, usd REAL NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return db


def reserve(maximum):
    limit = float(os.getenv("LIVE_BUDGET_USD", "0"))
    if not math.isfinite(limit) or limit <= 0:
        raise Fault("budget", 503)
    with closing(connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        total = db.execute("SELECT coalesce(sum(usd),0) FROM spend").fetchone()[0]
        if total + maximum > limit:
            raise Fault("budget", 503)
        return db.execute("INSERT INTO spend(usd) VALUES (?)", (maximum,)).lastrowid


def settle(row, actual):
    with closing(connect()) as db, db:
        db.execute("UPDATE spend SET usd=? WHERE id=?", (actual, row))


def cache_key(model, messages, config):
    return hashlib.sha256(json.dumps([model, messages, config], sort_keys=True).encode()).hexdigest()


def cached(key, value=None):
    with closing(connect()) as db, db:
        if value is not None:
            db.execute("INSERT OR REPLACE INTO cache VALUES (?,?)", (key, json.dumps(value)))
        row = db.execute("SELECT value FROM cache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
