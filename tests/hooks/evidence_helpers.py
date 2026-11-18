"""Test helpers: write what the verification gate now requires.

The gate accepts only an evidence record bound to the issue's files, for an
issue the mirror knows. These helpers produce exactly that, through the same
module the gate validates with, so tests exercise the real check.
"""
import json
import os
import stat
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOKS = os.path.join(REPO, ".claude", "hooks")
if HOOKS not in sys.path:
    sys.path.insert(0, HOOKS)
import evidence  # noqa: E402

# A small, stable tracked file: hashing it is cheap and does not move under a
# developer who is editing the repository while the suite runs.
ISSUE_FILES = ["tests/hooks/__init__.py"]
RUN_COMMAND = "pytest tests/auth -q"


def state_dir(home):
    return os.path.join(str(home), ".claude", "logs")


def make_known(home, issue="AGENT-14", project="AGENT", labels=None, files=None,
               run=RUN_COMMAND, **extra):
    """Record the issue in the mirror, as a read of it from Jira would."""
    d = os.path.join(state_dir(home), "jira-mirror")
    os.makedirs(d, exist_ok=True)
    event = {"op": "sync", "key": issue, "status": "In Progress",
             "labels": labels if labels is not None else ["role-coding", "spec-x"],
             "files": ISSUE_FILES if files is None else files, "run": run}
    event.update(extra)
    with open(os.path.join(d, project + ".jsonl"), "a") as fh:
        fh.write(json.dumps(event) + "\n")


def evidence_path(home, issue="AGENT-14", project="AGENT"):
    return os.path.join(state_dir(home), "verified", project, issue + ".verified")


def write_evidence(home, issue="AGENT-14", project="AGENT", kind="run", files=None,
                   command=RUN_COMMAND, known=True, checks=None, **overrides):
    """A valid evidence record for `issue` (and the mirror entry it is checked against)."""
    files = ISSUE_FILES if files is None else files
    if known:
        make_known(home, issue, project, files=files, run=command)
    now = time.time()
    record = evidence.make_record(issue, kind, command, 0,
                                  evidence.tree_hash(REPO, files), now - 1, now, "ok",
                                  checks=checks)
    record.update(overrides)
    path = evidence_path(home, issue, project)
    evidence.write_record(path, record)
    return path


def make_tool(directory, name, body="exit 0"):
    """An executable standing in for a static tool (a linter), for hook-run gates.

    Hook-run gates may only be programs that live OUTSIDE the working tree and
    are not interpreters, so tests cannot use `python -c`. `directory` must be
    outside the repository -- pytest's tmp_path is.
    """
    os.makedirs(str(directory), exist_ok=True)
    path = os.path.join(str(directory), name)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path
