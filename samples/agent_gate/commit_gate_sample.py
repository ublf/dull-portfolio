"""Generic, simplified illustration of a review-bound commit gate.

Idea: an AI reviewer may only "approve" an exact staged tree. Approval is a
small JSON verdict signed with HMAC-SHA256. A pre-commit hook recomputes the
fingerprint of what is being committed and refuses the commit unless a valid,
matching verdict exists. Changing even one staged byte after review breaks it.

This is a teaching sample, not the gate used in DULL. In a real setup:
  - the key lives outside the repository, readable only by hook scripts,
    and the agent's tools are denied access to it
  - only a hook that runs when the reviewer finishes writes the verdict;
    the reviewer itself has no write tools
  - a separate PreToolUse guard blocks commands such as `commit --no-verify`

Usage inside any git repository with something staged:
  python commit_gate_sample.py fingerprint
  python commit_gate_sample.py sign      # what the reviewer-stop hook would do
  python commit_gate_sample.py verify    # what the pre-commit hook would do
Key: path in the GATE_SAMPLE_KEY environment variable (a demo key is created
in the system temp folder if it is not set). Never commit a key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def staged_fingerprint() -> str:
    """SHA-256 over HEAD + the tree id of the index.

    `git write-tree` returns the exact tree `git commit` would record, so it
    covers every staged byte without depending on diff formatting.
    """
    try:
        head = git("rev-parse", "--verify", "HEAD")
    except subprocess.CalledProcessError:
        head = "none"  # first commit
    tree = git("write-tree")
    return hashlib.sha256(f"gate-sample-v1\nhead={head}\ntree={tree}".encode()).hexdigest()


def load_key() -> bytes:
    path = Path(os.environ.get("GATE_SAMPLE_KEY", Path(tempfile.gettempdir()) / "gate_sample.key"))
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
    return path.read_bytes()


def verdict_path() -> Path:
    return Path(git("rev-parse", "--git-dir")) / "gate-sample" / "verdict.json"


def sign() -> None:
    fp = staged_fingerprint()
    payload = json.dumps({"fingerprint": fp, "created": int(time.time())}, sort_keys=True)
    mac = hmac.new(load_key(), payload.encode(), hashlib.sha256).hexdigest()
    p = verdict_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"payload": payload, "mac": mac}))
    print(f"verdict written for {fp[:12]}...")


def verify(max_age_s: int = 3600) -> int:
    p = verdict_path()
    if not p.exists():
        print("BLOCKED: no review verdict")
        return 1
    v = json.loads(p.read_text())
    expected = hmac.new(load_key(), v["payload"].encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v["mac"]):
        print("BLOCKED: verdict signature invalid")
        return 1
    payload = json.loads(v["payload"])
    if payload["fingerprint"] != staged_fingerprint():
        print("BLOCKED: staged content changed after review")
        return 1
    if time.time() - payload["created"] > max_age_s:
        print("BLOCKED: verdict too old")
        return 1
    p.unlink()  # one verdict, one commit
    print("OK: reviewed content matches")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "fingerprint"
    if cmd == "fingerprint":
        print(staged_fingerprint())
    elif cmd == "sign":
        sign()
    elif cmd == "verify":
        sys.exit(verify())
    else:
        sys.exit(f"unknown command: {cmd}")
