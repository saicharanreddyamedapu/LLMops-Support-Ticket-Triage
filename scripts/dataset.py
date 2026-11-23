import csv
import hashlib
import io
import json
import re
import sys
import os
import tempfile
import urllib.request
from collections import defaultdict
from pathlib import Path

REV = "430d1a89bd93bd1fa23c16f29dd53e73f0087443"
FILE = "Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv"
URL = f"https://huggingface.co/datasets/bitext/Bitext-customer-support-llm-chatbot-training-dataset/resolve/{REV}/{FILE}"
SHA = "6f81102b0100b97b8468eb04368033a23206bf1fde9d53500d5806ec1001a434"
QUEUE = Path("data/reviews.csv")
SAMPLE = Path("data/sample.jsonl")


def make_sample():
    path = Path(".local/bitext.csv")
    if not path.exists():
        path.parent.mkdir(exist_ok=True)
        urllib.request.urlretrieve(URL, path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SHA:
        raise ValueError("CSV digest mismatch")
    catalog = json.loads(Path("config/catalog.json").read_text())
    groups, seen = defaultdict(list), set()
    for index, row in enumerate(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))), 1):
        ticket = row["instruction"]
        norm = " ".join(re.findall(r"\w+", ticket.casefold()))
        key = hashlib.sha256(norm.encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        cat = row["category"]
        groups[cat].append(dict(id=key, row=index, text=ticket, category=cat, product=catalog[cat], reference=row["response"]))
    result = []
    for cat in sorted(groups):
        for index, row in enumerate(sorted(groups[cat], key=lambda r: r["id"])[:24]):
            result.append(dict(row, split="dev" if index % 2 == 0 else "test"))
    SAMPLE.write_text("".join(json.dumps(row) + "\n" for row in result), encoding="utf-8")
    refresh_queue(result)
    print(len(result), "sampled")


def refresh_queue(rows):
    """Add the ticket text beside its review cells while preserving human edits."""
    existing = {}
    if QUEUE.exists():
        with QUEUE.open(encoding="utf-8-sig", newline="") as stream:
            records = list(csv.DictReader(stream))
        existing = {r["id"]: r for r in records}
        if len(existing) != len(records) or set(existing) != {r["id"] for r in rows}:
            raise ValueError("review IDs differ from frozen sample; preserve and reconcile the queue")
    with tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8", dir=QUEUE.parent, delete=False) as stream:
        writer = csv.writer(stream)
        writer.writerow(["id", "text", "priority", "reviewer", "rationale"])
        for row in rows:
            saved = existing.get(row["id"], {})
            writer.writerow([row["id"], row["text"], *(saved.get(k, "") for k in ("priority","reviewer","rationale"))])
    try:
        os.replace(stream.name, QUEUE)
    finally:
        Path(stream.name).unlink(missing_ok=True)


def reviews():
    rows = [json.loads(line) for line in SAMPLE.read_text(encoding="utf-8").splitlines()]
    ids = {r["id"] for r in rows}
    with QUEUE.open(encoding="utf-8-sig") as stream:
        records = list(csv.DictReader(stream))
    if len(ids) != len(rows) or len(records) != len(ids) or {r["id"] for r in records} != ids:
        raise ValueError("duplicate or missing review IDs")
    labels = {}
    for r in records:
        if any(r[k].strip() for k in ("priority", "reviewer", "rationale")):
            if r["priority"] not in ("low", "normal", "high", "urgent") or not all(r[k].strip() for k in ("reviewer", "rationale")):
                raise ValueError(f"incomplete review {r['id']}")
            labels[r["id"]] = r["priority"]
    return rows, labels


def dataset_digest():
    # Preserve the original four-column fingerprint when display columns change.
    canonical = io.StringIO(newline="")
    fields = ["id", "priority", "reviewer", "rationale"]
    writer = csv.DictWriter(canonical, fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    with QUEUE.open(encoding="utf-8-sig", newline="") as source:
        writer.writerows(csv.DictReader(source))
    return hashlib.sha256(SAMPLE.read_bytes().replace(b"\r\n", b"\n") + canonical.getvalue().encode()).hexdigest()


def provisional_priorities(rows):
    path = Path("data/provisional-priorities.csv")
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    records = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    labels = {r["id"]: r["priority"] for r in records}
    if len(records) != len(labels) or labels.keys() != {r["id"] for r in rows}:
        raise ValueError("provisional priority IDs differ from the frozen sample")
    if any(r["priority"] not in {"low","normal","high","urgent"} or
           r["source"] != "assistant-rubric-v1" or not r["rationale"].strip() for r in records):
        raise ValueError("invalid provisional annotation")
    return labels, hashlib.sha256(raw).hexdigest()


if __name__ == "__main__":
    if sys.argv[1:] == ["queue"]:
        refresh_queue([json.loads(line) for line in SAMPLE.read_text(encoding="utf-8").splitlines()])
    elif sys.argv[1:] == ["build"]:
        make_sample()
    else:
        rows, labels = reviews()
        print(f"{len(labels)} reviewed of {len(rows)}; minimum 200")
        raise SystemExit(int(len(labels) < 200))
