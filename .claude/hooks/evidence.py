"""Verification evidence: what the transition gate accepts as proof, and why.

The gate used to accept any file at the sentinel path -- an empty `touch` moved a
never-claimed issue to Done. Evidence is now a JSON record that only
`scripts/verify_issue.py` writes, after it has itself executed the issue's
`Run:` command, and that is bound to the content of the files the issue owns.

Three properties are checked by the gate:
  - the command recorded is the one Jira holds for the issue (so a different,
    easier command cannot stand in for it);
  - it exited 0;
  - the issue's files have not changed since (the tree hash still matches).

This is tamper-EVIDENT, not tamper-proof: an agent that sets out to forge a
record can compute the same hash. What it removes is the accidental and the
lazy path -- declaring done without running anything -- and it gives the
reviewer and CI something exact to re-check.

`.claude/gates.json` lists the project's checks (`agentChecks`: lint, tests).
`scripts/verify_issue.py` runs them in the agent's sandboxed shell and records
each result in the evidence. A hook runs none of them: a hook is outside the
sandbox, and a tool run there over a tree the worker writes is a way for the
worker to act unsandboxed. So this evidence is agent-produced throughout. It
catches the honest mistake; CI on the pull request is the independent re-run
and the authority.

Standard library only; imported by hooks and by scripts.
"""
import hashlib
import json
import os
import subprocess
import time

SCHEMA = 1
KINDS = ("run", "review")
GATES_PATH_ENV = "CLAUDE_HARNESS_GATES_PATH"
_GLOB_CHARS = set("*?[")


def _pathspecs(files):
    """git pathspecs for an issue's declared `Files:` entries.

    An entry is an exact path, a directory, or a glob. Directories and exact
    paths are native pathspecs; a glob needs the `:(glob)` magic or git reads
    `*` as a literal. No entries means the whole tree.
    """
    specs = []
    for entry in files or []:
        entry = str(entry).strip()
        if not entry:
            continue
        specs.append(":(glob)" + entry if _GLOB_CHARS & set(entry) else entry)
    return specs or ["."]


def tree_hash(cwd, files):
    """Hash of the working-tree content of the issue's files, or None.

    Tracked and untracked-but-not-ignored files under the pathspecs are listed
    by git and hashed by content; a tracked file deleted from the working tree
    is recorded as absent, so deleting a file changes the hash too. HEAD is
    deliberately not part of it: another agent committing elsewhere must not
    invalidate this issue's evidence.

    None means the hash could not be computed (not a git repository, git
    missing). Callers must treat that as "cannot verify", never as a match.
    """
    # `Files:` entries are repo-relative, so both the pathspecs and the paths
    # git prints are resolved against the top level, wherever cwd happens to be.
    top = _toplevel(cwd)
    if top is None:
        return None
    try:
        proc = subprocess.run(
            GIT + ["-C", top, "ls-files", "-z", "--cached", "--others",
                   "--exclude-standard", "--"] + _pathspecs(files),
            capture_output=True, timeout=30, env=git_env())
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    paths = sorted(set(p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p))
    digest = hashlib.sha256()
    budget = [TREE_HASH_MAX_BYTES]
    deadline = time.time() + TREE_HASH_MAX_SECONDS
    for rel in paths:
        digest.update(rel.encode("utf-8", "replace") + b"\0")
        try:
            digest.update(_entry_digest(os.path.join(top, rel), budget, deadline))
        except _OverBudget:
            return None
    return digest.hexdigest()


# The gate hashes whatever git lists under an issue's `Files:`, from a hook, with
# no sandbox. What is listed is the worker's to choose, so nothing here may
# block or grow without bound: a symlink to a FIFO made `open()` wait forever and
# one to /dev/zero made `read()` never end (review cycle 4), and a hook that
# stalls is cancelled by Claude Code and the tool call goes ahead.
TREE_HASH_MAX_BYTES = 1 << 30
TREE_HASH_MAX_SECONDS = 60
RECORD_MAX_BYTES = 4 << 20


class _OverBudget(Exception):
    pass


def _entry_digest(full, budget, deadline):
    """Digest of one listed path, by what it IS rather than what it leads to.

    A symlink is hashed as its link text, never followed: retargeting it changes
    the hash, and the gate never opens a file outside the tree on a worker's
    say-so. Only a regular file is read, without blocking and in bounded pieces.
    """
    import stat
    try:
        st = os.lstat(full)
    except OSError:
        return b"<absent>"
    if stat.S_ISLNK(st.st_mode):
        try:
            return b"<link>" + os.readlink(full).encode("utf-8", "replace")
        except OSError:
            return b"<absent>"
    if stat.S_ISDIR(st.st_mode):
        return b"<dir>"
    if not stat.S_ISREG(st.st_mode):
        return b"<special>"
    inner = hashlib.sha256()
    try:
        fd = os.open(full, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return b"<absent>"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return b"<special>"          # swapped between the lstat and the open
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            budget[0] -= len(chunk)
            if budget[0] < 0 or time.time() > deadline:
                raise _OverBudget()
            inner.update(chunk)
    except OSError:
        return b"<absent>"
    finally:
        os.close(fd)
    return inner.digest()


def read_regular_file(path, max_bytes=RECORD_MAX_BYTES):
    """Bytes of a regular file, or None. Never blocks, never follows a final
    symlink, never reads more than `max_bytes` (a larger file is None).

    For state a worker's shell can write and a hook then reads.
    """
    import stat
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            return None
        data = b""
        while len(data) <= max_bytes:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return data
            data += chunk
        return None
    except OSError:
        return None
    finally:
        os.close(fd)


# git runs programs named in its configuration: `core.fsmonitor` is executed by
# `ls-files`, and a worker can point git at configuration of its own (a linked
# worktree's `.git` file, or a `commondir` file) without touching `.git/config`.
# This code runs in a hook, outside the sandbox, so every git call here switches
# those off on the command line, where no configuration file can override it.
#
# `core.fsmonitor` is not the only one. In a repository marked as a partial
# clone, `ls-files` meeting a missing object fetches it from the promisor
# remote, and runs `remote.<name>.uploadpack` through a shell to do so (review
# cycle 4: that wrote an AWS approval marker from the gate's own git call). No
# git call here has any business on a transport, so lazy fetching is switched
# off in the environment and every transport is refused on the command line.
GIT = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
       "-c", "core.pager=cat", "-c", "core.untrackedCache=false",
       "-c", "protocol.allow=never"]


def git_env():
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
               GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1",
               # Empty = no protocol allowed. Unlike `-c protocol.allow=never`,
               # a `protocol.file.allow=always` in the repository's own
               # configuration does not override this.
               GIT_ALLOW_PROTOCOL="")
    for name in ("GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_SSH_COMMAND", "GIT_ASKPASS"):
        env.pop(name, None)
    return env


def _toplevel(cwd):
    try:
        proc = subprocess.run(GIT + ["-C", cwd, "rev-parse", "--show-toplevel"],
                              capture_output=True, timeout=10, env=git_env())
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip() or None


def normalize_command(command):
    """Whitespace-insensitive form of a Run: command, for comparison only."""
    return " ".join(str(command or "").split())


def make_record(issue_key, kind, command, exit_code, tree, started, finished,
                output_tail="", checks=None):
    return {
        "checks": dict(checks or {}),     # {agent check name: exit code}
        "schema": SCHEMA,
        "issue": issue_key,
        "kind": kind,
        "command": command,
        "exit_code": exit_code,
        "tree": tree,
        "started": started,
        "finished": finished,
        "duration_s": round(max(0.0, finished - started), 3),
        "output_tail": output_tail,
    }


def write_record(path, record):
    """Atomic write: a reader never sees a half-written record."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "{}.tmp.{}".format(path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def load_record(path):
    """The record at `path`, or None if it is missing or not a JSON object."""
    data = read_regular_file(path)
    if data is None:
        return None
    try:
        record = json.loads(data.decode("utf-8"))
    except Exception:
        return None
    return record if isinstance(record, dict) else None


def validate(record, issue_key, expected_command, current_tree, required_kind=None,
             required_checks=None):
    """(ok, reason). `reason` is written for the agent that has to fix it."""
    if not isinstance(record, dict):
        return False, ("the evidence file is empty or not a record written by "
                       "scripts/verify_issue.py")
    if record.get("schema") != SCHEMA:
        return False, "the evidence record has an unknown schema"
    if record.get("issue") != issue_key:
        return False, "the evidence record is for {}, not {}".format(
            record.get("issue"), issue_key)
    if record.get("kind") not in KINDS:
        return False, "the evidence record has no valid kind"
    if required_kind and record.get("kind") != required_kind:
        return False, ("this transition needs '{}' evidence but the record is '{}'"
                       .format(required_kind, record.get("kind")))
    if record.get("exit_code") != 0 or isinstance(record.get("exit_code"), bool):
        return False, "the recorded verification did not exit 0 (exit_code={})".format(
            record.get("exit_code"))
    if not expected_command:
        # Nothing to compare the record against means anything would match.
        return False, ("the issue declares no Run: command in the local mirror, so there is "
                       "nothing to verify against -- the lead adds one, or applies "
                       "skip-verify")
    if (normalize_command(record.get("command"))
            != normalize_command(expected_command)):
        return False, ("the recorded command is not this issue's Run: command "
                       "(recorded: {!r}; issue: {!r})".format(
                           record.get("command"), expected_command))
    if current_tree is None:
        return False, "the current state of the issue's files could not be hashed"
    if record.get("tree") != current_tree:
        return False, ("the issue's files changed after verification ran -- "
                       "the evidence is stale")
    recorded = record.get("checks") if isinstance(record.get("checks"), dict) else {}
    for name in required_checks or ():
        code = recorded.get(name)
        if code != 0 or isinstance(code, bool):
            return False, ("the project check '{}' is not recorded as passing in the "
                           "evidence ({}). Run scripts/verify_issue.py again; it runs the "
                           "project checks after the issue's Run: command".format(
                               name, "not run" if code is None else "exit {}".format(code)))
    return True, "evidence valid"


# --- project checks ---------------------------------------------------------
#
# A hook runs NO project tool. It used to run the lint itself ("hook-run
# gates"), on the reasoning that an agent cannot attest to what the hook
# executes. But a hook is outside the sandbox and the tool reads a tree the
# worker writes: five review cycles each found another way for the tree to
# steer the tool (its config file, ignore files, noqa, symlinks, default
# excludes) or the tool to act for the worker (rewriting files, reading files
# back). The surface could not be closed by enumeration, so it was removed.
# Every check now runs in the agent's sandboxed shell and is recorded in the
# evidence; the independent re-run is CI on the pull request.

def gates_path(project_dir):
    override = os.environ.get(GATES_PATH_ENV)
    if override:
        return os.path.expanduser(override)
    return os.path.join(project_dir, ".claude", "gates.json")


PYTHON_TOKEN = "{python}"


def _read_gate_file(project_dir):
    """(config dict or None, problem or None). A missing file is neither."""
    path = gates_path(project_dir)
    if not os.path.exists(path):
        return None, None
    try:
        with open(path) as fh:
            cfg = json.load(fh)
    except Exception as exc:
        return None, "unreadable gates file {}: {}".format(path, exc)
    if not isinstance(cfg, dict):
        return None, "gates file {} is not a JSON object".format(path)
    if cfg.get("gates"):
        return None, ("gates file {} lists hook-run `gates`, which no longer exist: a hook "
                      "runs no project tool. Move each entry to `agentChecks`".format(path))
    return cfg, None


def _entries(cfg, key):
    """(entries, problem) for a list of {name, command} objects under `key`."""
    out = []
    for gate in cfg.get(key) or []:
        if not isinstance(gate, dict):
            return [], "a `{}` entry is not an object".format(key)
        command = gate.get("command")
        if (not isinstance(command, list) or not command
                or not all(isinstance(a, str) and a for a in command)):
            return [], "{} {!r}: `command` must be a non-empty list of strings".format(
                key, gate.get("name"))
        when = gate.get("when", "always")
        if when not in CHECK_WHEN:
            return [], "{} {!r}: `when` must be one of {}".format(
                key, gate.get("name"), ", ".join(CHECK_WHEN))
        out.append({"name": str(gate.get("name") or command[0]), "command": command,
                    "when": when})
    return out, None


# When a project check runs. `always`: on every verification, the worker's and
# the reviewer's. `review`: only on the reviewer's (`--review`) run, which is the
# one that stands behind `Done`. The full test suite is `review`: it used to run
# on both, twice per issue, and the worker's run proved nothing the reviewer's
# did not. The issue's own `Run:` command always runs and holds its targeted tests.
CHECK_WHEN = ("always", "review")


def checks_for(checks, kind):
    """The checks a verification of this kind runs, and the gate then requires."""
    return [c for c in checks if kind == "review" or c.get("when", "always") == "always"]


def load_agent_checks(project_dir):
    """(checks, problem): project checks `scripts/verify_issue.py` runs and records.

    Lint, tests, anything else the project wants run before an issue moves.
    They run in the agent's sandboxed shell, never in a hook. They are
    agent-produced evidence: the gate confirms each is recorded as passing
    against the current files, and CI on the pull request is what re-runs them
    independently.
    """
    cfg, problem = _read_gate_file(project_dir)
    if problem or cfg is None:
        return [], problem
    return _entries(cfg, "agentChecks")


def resolve_python(cwd, project_dir=None):
    """The interpreter an agent check runs under.

    The working directory's virtualenv, then the main checkout's (a linked
    worktree has none of its own), then python3 on PATH (CI has no virtualenv).
    """
    for root in (cwd, project_dir):
        if root:
            candidate = os.path.join(root, ".venv", "bin", "python")
            if os.path.exists(candidate):
                return candidate
    import shutil
    import sys
    return shutil.which("python3") or sys.executable


def resolve_command(command, cwd, project_dir=None):
    return [resolve_python(cwd, project_dir) if arg == PYTHON_TOKEN else arg for arg in command]


