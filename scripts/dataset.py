import csv
import hashlib
import io
import json
import re
import sys
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
    if not QUEUE.exists():
        with QUEUE.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["id", "priority", "reviewer", "rationale"])
            writer.writerows((r["id"], "", "", "") for r in result)
    print(len(result), "sampled")


def reviews():
    rows = [json.loads(line) for line in SAMPLE.read_text(encoding="utf-8").splitlines()]
    ids = {r["id"] for r in rows}
    with QUEUE.open(encoding="utf-8") as stream:
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


if __name__ == "__main__":
    if sys.argv[1:] == ["build"]:
        make_sample()
    else:
        rows, labels = reviews()
        print(f"{len(labels)} reviewed of {len(rows)}; minimum 200")
        raise SystemExit(int(len(labels) < 200))
