"""Deploy successful GitHub main builds to a loopback registry and staging API."""
import argparse
import json
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.schema import Served

REGISTRY = "127.0.0.1:15000"
SERVICE = "triage-staging-api"
REGISTRY_CONTAINER = "triage-staging-registry"
LABEL = "support-ticket-triage.local-staging"
STATUS = Path(".local/staging/deployment.json")


def output(*command):
    return subprocess.check_output(command,text=True).strip()


def container(name):
    result = subprocess.run(["docker","container","inspect",name],capture_output=True,text=True)
    if result.returncode:
        if "no such" in result.stderr.lower():
            return None
        raise RuntimeError(result.stderr.strip())
    item = json.loads(result.stdout)[0]
    if (item["Config"].get("Labels") or {}).get(LABEL) != "true":
        raise RuntimeError(f"Container name already in use: {name}")
    return item


def successful_run(repository, sha):
    owner = repository.split("/")[0]
    credential = subprocess.run(["git","credential","fill"], input=f"protocol=https\nhost=github.com\nusername={owner}\n\n",
                                capture_output=True,text=True,check=True)
    token = dict(line.split("=",1) for line in credential.stdout.splitlines() if "=" in line)["password"]
    url = f"https://api.github.com/repos/{repository}/actions/workflows/verify.yml/runs"
    response = httpx.get(url, params={"head_sha":sha,"branch":"main","event":"push","per_page":10},
                         headers={"Authorization":"Bearer "+token},timeout=20)
    response.raise_for_status()
    runs = response.json()["workflow_runs"]
    run = max(runs,key=lambda r:r["run_number"],default=None)
    if run and run["head_sha"] == sha and run["conclusion"] == "success":
        return run["html_url"]


def smoke(sha, port):
    with httpx.Client(base_url=f"http://127.0.0.1:{port}",trust_env=False,timeout=8) as client:
        for _ in range(60):
            try:
                if client.get("/readyz").json().get("mode") == "fixture":
                    break
            except (httpx.RequestError, ValueError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError("Local staging readiness timed out")
        response = client.post("/triage",json={"text":"How can I track my order?"},
                               headers={"Authorization":"Bearer fixture-demo"})
        response.raise_for_status()
        value = Served.model_validate(response.json())
        if value.git_sha != sha or value.mode != "fixture":
            raise RuntimeError("Staging response revision or mode differs")
        return value.prompt_version


def deploy():
    origin = output("git","remote","get-url","origin")
    parsed = urlparse(origin)
    repository = parsed.path.strip("/").removesuffix(".git")
    if parsed.scheme != "https" or parsed.netloc != "github.com" or not re.fullmatch(r"[\w.-]+/[\w.-]+",repository):
        raise ValueError("Expected an HTTPS GitHub origin")
    sha = output("git","ls-remote","--exit-code",origin,"refs/heads/main").split()[0]
    if not re.fullmatch(r"[a-f0-9]{40}",sha):
        raise ValueError("Invalid main revision")
    old = container(SERVICE)
    image = f"{REGISTRY}/support-ticket-triage:{sha}"
    if old and old["State"]["Running"] and old["Config"]["Image"] == image and STATUS.exists():
        return
    run_url = successful_run(repository,sha)
    if not run_url:
        return
    registry = container(REGISTRY_CONTAINER)
    if not registry:
        subprocess.run(["docker","run","-d","--name",REGISTRY_CONTAINER,"--label",LABEL+"=true",
                        "-p",REGISTRY+":5000","-v","triage-staging-registry:/var/lib/registry",
                        "registry:2.8.3"],check=True)
    elif not registry["State"]["Running"]:
        subprocess.run(["docker","start",REGISTRY_CONTAINER],check=True)
    subprocess.run(["git","fetch","--no-tags","origin","main"],check=True)
    if output("git","rev-parse","FETCH_HEAD") != sha:
        return
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["git","archive","--format=tar",sha],stdout=archive,check=True)
        archive.seek(0)
        subprocess.run(["docker","build","--build-arg","GIT_SHA="+sha,"-t",image,"-"],stdin=archive,check=True)
    subprocess.run(["docker","push",image],check=True)
    # Validate before replacing the last deployed revision.
    trial = output("docker","run","-d","--rm","--label",LABEL+"=true","-p","127.0.0.1::8000",image)
    try:
        port = int(output("docker","port",trial,"8000/tcp").rsplit(":",1)[1])
        smoke(sha,port)
    finally:
        subprocess.run(["docker","stop",trial],check=True)
    if output("git","ls-remote","--exit-code",origin,"refs/heads/main").split()[0] != sha:
        return
    previous = SERVICE+"-previous"
    if old:
        if container(previous):
            raise RuntimeError("A previous staging recovery is pending")
        subprocess.run(["docker","rename",SERVICE,previous],check=True)
        subprocess.run(["docker","stop",previous],check=True)
    try:
        subprocess.run(["docker","run","-d","--name",SERVICE,"--label",LABEL+"=true",
                        "-p","127.0.0.1:8767:8000",image],check=True)
        version = smoke(sha,8767)
    except Exception:
        if container(SERVICE):
            subprocess.run(["docker","rm","-f",SERVICE],check=True)
        if old:
            subprocess.run(["docker","rename",previous,SERVICE],check=True)
            subprocess.run(["docker","start",SERVICE],check=True)
        raise
    if old:
        subprocess.run(["docker","rm",previous],check=True)
    STATUS.parent.mkdir(parents=True,exist_ok=True)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(timestamp=datetime.now(timezone.utc).isoformat(),git_sha=sha,
                   verify_run=run_url,image=image,prompt_version=version,mode="fixture",
                   endpoint="http://127.0.0.1:8767"),indent=2)+"\n",encoding="utf-8")
    tmp.replace(STATUS)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch",action="store_true")
    args=parser.parse_args()
    while True:
        try:
            deploy()
        except (OSError, RuntimeError, ValueError, httpx.HTTPError, subprocess.CalledProcessError) as error:
            if not args.watch:
                raise
            print(type(error).__name__,str(error),flush=True)
        if not args.watch:
            break
        time.sleep(120)
