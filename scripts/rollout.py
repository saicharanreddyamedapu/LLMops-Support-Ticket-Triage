import argparse
import json
import os
import stat
import tempfile
from pathlib import Path


def weight(percent, path=None):
    path = Path(path or os.getenv("ROLLOUT_PATH","config/rollout.json"))
    cfg = json.loads(path.read_text())
    if not 0 <= percent <= 100:
        raise ValueError("percent outside 0..100")
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        json.dump(dict(cfg, percent=percent), f, indent=2)
        f.write("\n")
    try:
        os.chmod(f.name, stat.S_IMODE(path.stat().st_mode))
        os.replace(f.name,path)
    finally:
        Path(f.name).unlink(missing_ok=True)
    return percent


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Set the candidate traffic percentage.")
    parser.add_argument("percent", type=int)
    parser.add_argument("--path", type=Path, help="Use the rollout file for a specific service.")
    args = parser.parse_args()
    print("candidate percent", weight(args.percent, args.path))
