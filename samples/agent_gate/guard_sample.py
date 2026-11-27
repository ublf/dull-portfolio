"""Generic sketch of a PreToolUse guard for an AI coding agent.

The agent host calls this before each shell command with JSON on stdin
({"tool_input": {"command": "..."}}). Exit code 2 blocks the command and the
stderr text is shown to the agent. Text matching is a safety net, not a
sandbox: indirect forms (a script that runs the command) can slip through,
so it is combined with permission rules and the review-bound commit gate.

Run the self-test:  python guard_sample.py --self-test
"""

from __future__ import annotations

import json
import re
import sys

RULES: list[tuple[str, str]] = [
    (r"\bgit\s+commit\b.*(--no-verify|\s-n\b|--amend|\s-a\b|\s-am\b)", "commit flags that skip or widen the review"),
    (r"\bgit\s+push\b.*(--force|\s-f\b|--force-with-lease)", "force push"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f)", "destructive git command"),
    (r"\bgit\s+add\s+(\.|-A|--all)(\s|$)", "stage exact paths only"),
    (r"\bgit\s+(update-ref|commit-tree|hash-object\s+-w)\b", "git plumbing"),
    (r"\brm\s+-[a-z]*r[a-z]*f", "recursive delete"),
    (r"\badb\b.*\b(pm\s+clear|uninstall)\b", "would erase app data on the phone"),
    (r"\badb\b.*\b(uiautomator\s+dump|monkey)\b", "phone automation that changed device settings"),
    (r"\badb\b.*(accelerometer_rotation|user_rotation|wm\s+rotation)", "rotation settings are user-owned"),
    (r"private_local_data", "folder with private settings (example name)"),
]


def check(command: str) -> str | None:
    for pattern, reason in RULES:
        if re.search(pattern, command, re.IGNORECASE):
            return reason
    return None


def main() -> int:
    data = json.load(sys.stdin)
    reason = check(str(data.get("tool_input", {}).get("command", "")))
    if reason:
        print(f"[guard] blocked: {reason}", file=sys.stderr)
        return 2
    return 0


def self_test() -> int:
    cases = {
        "git status": None,
        "git add lib/a.dart test/a_test.dart": None,
        "git add .": "stage exact paths only",
        "git commit --no-verify -m x": "commit flags that skip or widen the review",
        "git push --force origin main": "force push",
        "adb shell pm clear com.example.app": "would erase app data on the phone",
        "adb shell settings put system accelerometer_rotation 1": "rotation settings are user-owned",
        "adb install -r app.apk": None,
    }
    bad = 0
    for cmd, want in cases.items():
        got = check(cmd)
        ok = (got is None) == (want is None)
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {cmd!r} -> {got}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else main())
