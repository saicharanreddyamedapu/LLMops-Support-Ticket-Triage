import json
import math
import sys
from pathlib import Path

METRICS = {"category":("floor",.02),"product":("floor",.02),"priority":("floor",.02),
           "first_pass":("floor",.02),"availability":("floor",.02),"p95_ms":("ratio",.15),
           "priced_usd_per_request":("ratio",.15)}


def compare(base, candidate):
    for key in ("mode","method","dataset_digest","revision","split","n","reviewed","cache"):
        if base[key] != candidate[key]:
            raise ValueError("incomparable "+key)
    if base["split"] != "test" or base["n"] <= 0 or base["mode"] not in ("live","fixture"):
        raise ValueError("requires held-out non-replayed measurements")
    metrics = METRICS.copy()
    if base["mode"] == "live":
        for report in (base, candidate):
            if report.get("unknown_usage_attempts") != 0:
                raise ValueError("live cost comparison requires complete usage")
            if report.get("human_review_total", 0) < 200 or report["reviewed"] <= 0:
                raise ValueError("live gate requires 200 human priority reviews and held-out labels")
            if report.get("judge_errors") != 0 or not report.get("judge_n") or report["judge_n"] != round(report["availability"] * report["n"]):
                raise ValueError("live gate requires reply grades for every successful request")
        metrics["draft_reply_judge"] = ("floor", .02)
    rejected = False
    rows = ["|Metric|Base|Candidate|Gate|","|---|---:|---:|---|"]
    for key,(kind,tolerance) in metrics.items():
        a,b = base[key],candidate[key]
        if key == "priority" and base["reviewed"] == 0 and a is None and b is None:
            rows.append("|priority|pending|pending|Human review required|")
            continue
        if not all(isinstance(v,(int,float)) and math.isfinite(v) and v>=0 for v in (a,b)):
            raise ValueError("missing or invalid metric "+key)
        if kind == "floor" and max(a,b) > 1:
            raise ValueError("rate above one for "+key)
        failed = b < a-tolerance-1e-12 if kind == "floor" else b > a*(1+tolerance)+1e-12
        rejected |= failed
        rows.append(f"|{key}|{a:.7g}|{b:.7g}|{'REJECT' if failed else 'pass'}|")
    rows.append(f"\nMode {base['mode']}, n={base['n']}.")
    if base["mode"] == "fixture":
        rows.append("Human-reviewed live provider evidence remains pending.")
    return rejected,"\n".join(rows)


if __name__ == "__main__":
    failed,table = compare(*(json.loads(Path(f).read_text()) for f in sys.argv[1:3]))
    print(table)
    if len(sys.argv)>3:
        Path(sys.argv[3]).write_text(table+"\n",encoding="utf-8")
    raise SystemExit(int(failed))
