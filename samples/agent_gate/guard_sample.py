"""Generic sketch of a PreToolUse guard for an AI coding agent.

The agent host calls this before each shell command with JSON on stdin
({"tool_input": {"command": "..."}}). Exit code 2 blocks the command and the
stderr text is shown to the agent.

git commands are tokenized with shlex, global options (-C <path>,
-c <key=value>, --git-dir=..., ...) are skipped to find the subcommand, and
the values of -m / -F are never inspected, so a commit message that mentions
a flag is not blocked. Any mention of hooksPath is blocked anywhere.

This is a safety net, not a sandbox: indirect forms (a script that runs the
command) can slip through, so it is combined with permission rules and the
review-bound commit gate (see commit_gate_sample.py).

Run the self-test:  python guard_sample.py --self-test
"""

from __future__ import annotations

import json
import re
import shlex
import sys

# git global options that take a separate value: `git -C path commit`.
GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}

# commit options whose next token is a value (never inspected).
COMMIT_VALUE_OPTS = {"-m", "--message", "-F", "--file", "-C", "-c", "--reuse-message",
                     "--reedit-message", "--author", "--date", "-t", "--template"}

# Plain text rules, checked on the whole command.
TEXT_RULES: list[tuple[str, str]] = [
    (r"(?i)hookspath", "changing the hooks path disables the gate"),
    (r"\brm\s+-[a-z]*r[a-z]*f", "recursive delete"),
    (r"\badb\b.*\b(pm\s+clear|uninstall)\b", "would erase app data on the phone"),
    (r"\badb\b.*\b(uiautomator\s+dump|monkey)\b", "phone automation that changed device settings"),
    (r"\badb\b.*(accelerometer_rotation|user_rotation|wm\s+rotation)", "rotation settings are user-owned"),
    (r"private_local_data", "folder with private settings (example name)"),
]


def split_commands(command: str) -> list[list[str]]:
    """Tokenize and split on shell separators (;, &&, ||, |)."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return [["<unparsable>"]]
    parts, cur = [], []
    for tok in tokens:
        if tok and set(tok) <= set(";&|"):
            if cur:
                parts.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        parts.append(cur)
    return parts


def git_subcommand(tokens: list[str]) -> tuple[str | None, list[str]]:
    """Return (subcommand, its args) for a git invocation, else (None, [])."""
    if not tokens or not re.fullmatch(r"(?i)git(\.exe)?", tokens[0].replace("\\", "/").rsplit("/", 1)[-1]):
        return None, []
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in GIT_GLOBAL_WITH_VALUE:
            i += 2
        elif tok.startswith("-"):
            i += 1  # e.g. --no-pager, --git-dir=x, -P
        else:
            return tok, tokens[i + 1:]
    return None, []


def check_commit(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return "commit with pathspecs widens what was reviewed"
        if tok in COMMIT_VALUE_OPTS:
            i += 2  # skip the value (message text, file name, ...)
            continue
        if tok.startswith("--"):
            name = tok.split("=", 1)[0]
            if name in {"--no-verify", "--amend", "--all", "--include", "--only"}:
                return f"commit {name} skips or widens the review"
        elif re.fullmatch(r"-[A-Za-z]+", tok):
            flags = tok[1:]
            for j, ch in enumerate(flags):
                if ch in "aino":
                    return f"commit -{ch} skips or widens the review"
                if ch in "mFCct":  # value-taking flag ends the cluster
                    if j == len(flags) - 1:
                        i += 1  # its value is the next token
                    break
        i += 1
    return None


def check_push(args: list[str]) -> str | None:
    for tok in args:
        if tok.startswith("--force") or tok in {"--mirror", "--delete", "--prune"}:
            return "force / destructive push"
        if re.fullmatch(r"-[A-Za-z]*[fd][A-Za-z]*", tok):
            return "force / destructive push"
        if tok.startswith("+"):
            return "force push via +refspec"
    return None


def check_add(args: list[str]) -> str | None:
    for tok in args:  # also after "--"
        if tok in {".", "-A", "--all", "-u", "--update", "*"}:
            return "stage exact paths only"
    return None


def check(command: str) -> str | None:
    for pattern, reason in TEXT_RULES:
        if re.search(pattern, command):
            return reason
    for tokens in split_commands(command):
        sub, args = git_subcommand(tokens)
        if sub is None:
            continue
        if sub == "commit":
            reason = check_commit(args)
        elif sub == "push":
            reason = check_push(args)
        elif sub == "add":
            reason = check_add(args)
        elif sub in {"commit-tree", "update-ref", "filter-branch", "replace"}:
            reason = "git plumbing"
        elif sub == "reset" and "--hard" in args:
            reason = "destructive git command"
        elif sub == "clean" and any(re.fullmatch(r"-[a-z]*f[a-z]*", a) for a in args):
            reason = "destructive git command"
        else:
            reason = None
        if reason:
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
    blocked, allowed = True, False
    cases = {
        "git status": allowed,
        "git add lib/a.dart test/a_test.dart": allowed,
        'git commit -m "note -a flag"': allowed,
        'git commit -m "mention --no-verify in text"': allowed,
        "git commit -F msg.txt": allowed,
        "git push origin feature/x": allowed,
        "adb install -r app.apk": allowed,
        "git add .": blocked,
        "git add -- .": blocked,
        "git add -A": blocked,
        "git add -u": blocked,
        "git commit --no-verify -m x": blocked,
        "git -C . commit --no-verify": blocked,
        "git commit -n -m x": blocked,
        'git commit -am "x"': blocked,
        "git -c core.hooksPath=/dev/null commit": blocked,
        "git config core.hooksPath /tmp/empty": blocked,
        "git push --force origin main": blocked,
        "git push -f origin main": blocked,
        "git push origin +main": blocked,
        "git status && git push --force-with-lease": blocked,
        "adb shell pm clear com.example.app": blocked,
        "adb shell settings put system accelerometer_rotation 1": blocked,
    }
    bad = 0
    for cmd, want in cases.items():
        got = check(cmd)
        ok = (got is not None) == want
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {cmd!r} -> {got}")
    print(f"{len(cases) - bad}/{len(cases)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else main())
