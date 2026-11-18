"""No credential may live in a tracked file or be injected through settings `env`.

A Jira admin token was once committed in .claude/settings.json under `env`, which
also put it in every agent's shell. These tests keep that from coming back.
"""
import json
import os
import re
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Atlassian API tokens, AWS access key ids, GitHub tokens, private key blocks.
_SECRET_PATTERNS = [
    re.compile(r"ATATT[A-Za-z0-9_\-=]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
# `_TOKEN` as a suffix, so a size limit such as MAX_MCP_OUTPUT_TOKENS is not a hit.
_CREDENTIAL_ENV_NAME = re.compile(
    r"(^|_)TOKEN$|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|CREDENTIAL", re.I)


def _tracked_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True,
                         check=True).stdout.decode()
    return [p for p in out.split("\0") if p]


def test_no_secret_shaped_string_in_any_tracked_file():
    hits = []
    for rel in _tracked_files():
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path) or os.path.getsize(path) > 2_000_000:
            continue
        if os.path.abspath(path) == os.path.abspath(__file__):
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                hits.append("{} matches {}".format(rel, pattern.pattern))
    assert not hits, "secret-shaped strings in tracked files:\n" + "\n".join(hits)


def test_settings_env_injects_no_credential_named_variable():
    settings = json.load(open(os.path.join(REPO, ".claude", "settings.json")))
    bad = [k for k in settings.get("env", {}) if _CREDENTIAL_ENV_NAME.search(k)]
    assert not bad, (
        "settings.json `env` is inherited by every agent shell; these keys look "
        "like credentials: {}".format(bad))


def test_local_settings_file_is_gitignored():
    proc = subprocess.run(["git", "check-ignore", "-q", ".claude/settings.local.json"],
                          cwd=REPO)
    assert proc.returncode == 0, ".claude/settings.local.json must be ignored by this repo"


def test_local_settings_file_is_not_tracked():
    assert ".claude/settings.local.json" not in _tracked_files()
