#!/usr/bin/env python3
"""Run the CI checks against a fresh clone of a commit, on this machine.

    python3 scripts/ci_local.py            # HEAD
    python3 scripts/ci_local.py <commit>

`scripts/verify_issue.py` records evidence against the WORKING TREE. A check
that passes there can still fail on what was committed: a file never added, a
git-ignored config the tests lean on, a leftover build artefact. This script
clones the repository at one commit into a temporary directory and runs the
same checks `.github/workflows/ci.yml` runs, there.

It is a stand-in for CI, not CI. The agent that wrote the code also runs this,
so it catches the honest mistake and nothing more; a required check on a
remote is what an agent cannot write for itself. Run it before closing an Epic
and record the result on the Epic.

Exit codes: 0 every step passed; 1 a step failed; 2 the commit could not be
cloned, or bad arguments.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_TOKEN = "{python}"

# The check steps of .github/workflows/ci.yml, in order. Installing
# dependencies is CI's own concern and is not mirrored.
# tests/test_ci_local.py fails if this list and the workflow differ.
STEPS = [
    ("Secret scan (tracked files)",
     [PYTHON_TOKEN, "-m", "pytest", "tests/test_no_tracked_credentials.py"]),
    ("Lint", [PYTHON_TOKEN, "-m", "ruff", "check", "."]),
    ("Tests", [PYTHON_TOKEN, "-m", "pytest", "-n", "auto"]),
]


def _git(args, cwd):
    # --no-optional-locks: `git status` must not rewrite the index of the
    # checkout it is only looking at.
    return subprocess.run(["git", "--no-optional-locks"] + args, cwd=cwd,
                          capture_output=True, text=True)


def default_python(repo):
    """The interpreter the steps run under: the checkout's own virtualenv when
    it has one, else the interpreter running this script.

    The clone has no virtualenv, and `python3` on PATH usually has neither
    pytest nor ruff. Started as `python3 scripts/ci_local.py`, the script used
    to run every step with that bare interpreter and report a failure that had
    nothing to do with the code. `scripts/verify_issue.py` resolves `{python}`
    the same way.
    """
    candidate = os.path.join(repo, ".venv", "bin", "python")
    return candidate if os.path.exists(candidate) else sys.executable


def resolve(repo, rev):
    """Full hash of `rev` in `repo`, or None."""
    done = _git(["rev-parse", "--verify", "--quiet", rev + "^{commit}"], repo)
    return done.stdout.strip() or None if done.returncode == 0 else None


def dirty(repo):
    """Paths with uncommitted changes: what a run on a commit does NOT cover."""
    done = _git(["status", "--porcelain"], repo)
    return [line[3:] for line in done.stdout.splitlines() if line.strip()]


def clone(repo, commit, workdir):
    """A fresh clone of `repo` at `commit` under `workdir`, or None on failure."""
    target = os.path.join(workdir, "src")
    if _git(["clone", "--quiet", "--no-hardlinks", repo, target], workdir).returncode != 0:
        return None
    if _git(["checkout", "--quiet", "--detach", commit], target).returncode != 0:
        return None
    return target


def run(repo, rev="HEAD", steps=None, python=None, workdir=None, out=sys.stdout):
    """Run `steps` in a fresh clone of `repo` at `rev`.

    Returns (exit_code, failed_step_name_or_None). The clone is removed.
    """
    steps = STEPS if steps is None else steps
    python = python or default_python(repo)
    commit = resolve(repo, rev)
    if commit is None:
        print("cannot resolve {!r} to a commit in {}".format(rev, repo), file=out)
        return 2, None
    made = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="ci-local-")
    try:
        tree = clone(repo, commit, workdir)
        if tree is None:
            print("cannot clone {} at {}".format(repo, commit[:12]), file=out)
            return 2, None
        print("commit {} in a fresh clone, steps run with {}".format(commit[:12], python),
              file=out)
        changed = dirty(repo)
        if changed:
            print("NOTE: the working tree has {} uncommitted change(s), which this run does "
                  "not cover: {}{}".format(len(changed), ", ".join(changed[:5]),
                                           " ..." if len(changed) > 5 else ""), file=out)
        # The clone must not inherit this checkout's identity: a test that asks
        # "where is the project?" has to get the clone.
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_HARNESS_STATE_DIR",
                            "CLAUDE_HARNESS_GATES_PATH", "JIRA_CONFIG_PATH")}
        for name, argv in steps:
            argv = [python if a == PYTHON_TOKEN else a for a in argv]
            out.flush()
            code = subprocess.run(argv, cwd=tree, env=env).returncode
            print("[{}] {}".format("ok  " if code == 0 else "FAIL", name), file=out)
            if code != 0:
                return 1, name
        return 0, None
    finally:
        if made:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            shutil.rmtree(os.path.join(workdir, "src"), ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("rev", nargs="?", default="HEAD", help="commit to test (default HEAD)")
    args = parser.parse_args(argv)
    code, failed = run(REPO, args.rev)
    if code == 0:
        print("PASSED: every CI step passed on the committed tree")
    elif code == 1:
        print("FAILED at step: {}".format(failed))
    return code


if __name__ == "__main__":
    sys.exit(main())
