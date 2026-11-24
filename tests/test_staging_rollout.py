import json
import subprocess

import pytest

from scripts import local_staging, recorded_gate
from scripts.common import write
from scripts.rollout import weight


def test_staging_revision_preserves_its_operator_rollback(tmp_path, monkeypatch):
    monkeypatch.setattr(local_staging, "STATUS", tmp_path / "deployment.json")
    config = {"stable": "v1", "candidate": "v2", "percent": 10}
    monkeypatch.setattr(local_staging, "output", lambda *args: json.dumps(config))
    sha = "b" * 40
    mount = local_staging.rollout_mount(sha)
    path = tmp_path / "rollouts" / sha / "rollout.json"
    weight(0, path)
    assert local_staging.rollout_mount(sha) == mount
    assert json.loads(path.read_text()) == {**config, "percent": 0}
    other = "a" * 40
    assert local_staging.rollout_mount(other) != mount
    assert json.loads((tmp_path / "rollouts" / other / "rollout.json").read_text()) == config


def test_old_v1_measurement_cannot_cover_an_api_handler_change(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "--quiet")
    handler = tmp_path / "app" / "main.py"
    handler.parent.mkdir()
    handler.write_text("implementation = 1")
    write(tmp_path / "config/rollout.json", {"stable":"v1", "candidate":"v1", "percent":0})
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "record baseline inputs")
    write(tmp_path / "report.json", {"prompt_version":"v1", "mode":"local", "git_sha":git("rev-parse", "HEAD")})
    handler.write_text("implementation = 2")
    with pytest.raises(ValueError, match="app/main.py"):
        recorded_gate.measured(tmp_path, "stable", "report.json")
