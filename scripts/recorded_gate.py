"""Compare the two recorded local-model runs and detect stale inference inputs."""
import json
import math
import re
import subprocess
import sys
from contextlib import chdir
from pathlib import Path

import yaml

from app.schema import Served
from scripts.dataset import dataset_digest, provisional_priorities, reviews
from scripts.gate import compare


def verify_scores(root, report):
    with chdir(root):
        rows, human = reviews()
        labels, digest = provisional_priorities(rows)
        if (report["priority_source"] != "assistant-rubric-v1" or report["provisional_digest"] != digest
                or report["dataset_digest"] != dataset_digest() or report["human_review_total"] != len(human)):
            raise ValueError("priority or dataset provenance mismatch")
        tickets = {r["id"]:r for r in rows if r["split"] == "test"}
        path = Path("results/local") / (report["prompt_version"]+"-outputs.json")
        outputs = json.loads(path.read_text(encoding="utf-8"))
    n = len(tickets)
    if len(outputs) != n or {r["id"] for r in outputs} != tickets.keys() or report["n"] != n or report["reviewed"] != n:
        raise ValueError("held-out ticket IDs or counts differ")
    counts = dict(category=0, product=0, priority=0, first_pass=0, availability=0)
    grades = []
    for row in outputs:
        if "error" in row["output"]:
            continue
        value = Served.model_validate(row["output"])
        if (value.mode,value.git_sha,value.prompt_version) != ("local",report["git_sha"],report["prompt_version"]):
            raise ValueError("per-ticket inference provenance mismatch")
        ticket = tickets[row["id"]]
        counts["availability"] += 1
        counts["first_pass"] += value.first_pass
        counts["category"] += value.category == ticket["category"]
        counts["product"] += value.product == ticket["product"]
        counts["priority"] += value.priority == labels[row["id"]]
        if type(row["draft_reply_pass"]) is not bool:
            raise ValueError("missing reply grade")
        grades.append(row["draft_reply_pass"])
    if not grades or len(grades) != report["judge_n"] or not math.isclose(sum(grades)/len(grades),report["draft_reply_judge"]):
        raise ValueError("reply grades differ from report")
    if any(not math.isclose(count/n,report[key]) for key,count in counts.items()):
        raise ValueError("per-ticket scores differ from report")


def measured(checkout, role, path):
    root = Path(checkout)
    report = json.loads((root / path).read_text(encoding="utf-8"))
    version = json.loads((root / "config/rollout.json").read_text())[role]
    if not re.fullmatch(r"v[0-9]+", version) or report["prompt_version"] != version:
        raise ValueError(f"{role} report must measure the configured prompt version")
    rev = report["git_sha"]
    if report["mode"] != "local" or not re.fullmatch(r"[0-9a-f]{40}", rev):
        raise ValueError("expected a recorded local model run with a full source revision")
    subprocess.run(["git","-C",str(root),"merge-base","--is-ancestor",rev,"HEAD"], check=True)
    inputs = ["app/schema.py","app/gateway.py","app/settings.py","prompts/local.yaml",
              "prompts/judge.yaml",f"prompts/triage/{version}.yaml","data/sample.jsonl",
              "data/reviews.csv","data/provisional-priorities.csv","config/catalog.json","uv.lock"]
    if yaml.safe_load((root/f"prompts/triage/{version}.yaml").read_text()).get("product_from_category"):
        inputs.append("app/main.py")
    changed = subprocess.run(["git","-C",str(root),"diff","--name-only",rev,"--",*inputs],
                             check=True,capture_output=True,text=True).stdout.strip()
    if changed:
        raise ValueError(f"repeat the {role} local evaluation after changes to:\n{changed}")
    verify_scores(root, report)
    return report


if __name__ == "__main__":
    base = measured(sys.argv[1], "stable", "results/baseline.json")
    candidate = measured(sys.argv[2], "candidate", "results/local/current.json")
    rejected, table = compare(base, candidate)
    print(table)
    raise SystemExit(int(rejected))
