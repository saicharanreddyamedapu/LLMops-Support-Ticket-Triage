import json
import os
import stat
import sys
import tempfile
from pathlib import Path


def weight(percent):
    path = Path(os.getenv("ROLLOUT_PATH","config/rollout.json"))
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
    print("candidate percent",weight(int(sys.argv[1])))
