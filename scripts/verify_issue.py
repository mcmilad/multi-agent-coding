#!/usr/bin/env python3
"""Run an issue's verification and record the evidence the transition gate needs.

    python3 scripts/verify_issue.py AG-8            # implementer, before In Review
    python3 scripts/verify_issue.py AG-8 --review   # reviewer, before Done

It reads the issue's `Run:` command and `Files:` from the local Jira mirror,
executes the command, then the project's own checks (`agentChecks` in
.claude/gates.json: lint on every run, the full test suite only with --review),
and -- only if every one exits 0 --
writes an evidence record bound to the current content of the issue's files. The gate refuses a record
that is missing, hand-written, for another command, non-zero, or stale.

The command comes from the Jira issue, so it is executed here, inside whatever
sandbox the calling shell has, and never by a hook.

Exit codes:
  0  verification passed and evidence was written
  1  verification ran and failed (no evidence written; any old record removed)
  2  usage error, no Jira project configured, or the issue is unknown to the mirror
  3  the issue declares no Run: command
  4  the verification timed out
  5  the state of the issue's files could not be hashed (not a git repository?)
  6  the project checks could not be loaded or started
"""
import argparse
import os
import subprocess
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, ".claude", "hooks"))
import evidence  # noqa: E402
import jira_mirror  # noqa: E402
import jira_transition_verify_gate as gate  # noqa: E402

DEFAULT_TIMEOUT_SECONDS = 1800
OUTPUT_TAIL_LINES = 40


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("issue", help="issue key, e.g. AG-8")
    parser.add_argument("--review", action="store_true",
                        help="record review evidence (needed to close an issue)")
    parser.add_argument("--cwd", default=None,
                        help="directory to verify in (default: current directory)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS,
                        help="seconds before the command is abandoned (default %(default)s)")
    args = parser.parse_args(argv)

    cwd = os.path.abspath(args.cwd or os.getcwd())
    project = jira_mirror.load_config().get("projectKey")
    if not project:
        print("no projectKey in the Jira config", file=sys.stderr)
        return 2
    key = jira_mirror.normalize_issue_key(args.issue)
    if jira_mirror.issue_project(key) != project:
        print("{} is not an issue of project {}".format(args.issue, project), file=sys.stderr)
        return 2
    issue = jira_mirror.load_state(project).get(key)
    if issue is None:
        print("{} is unknown to the local mirror. Read it first (getJiraIssue, or a JQL "
              "search that returns it); the read is recorded automatically.".format(key),
              file=sys.stderr)
        return 2
    command = issue.get("run") or ""
    if not command:
        print("{} declares no Run: command. Ask the lead to add one, or to apply the "
              "skip-verify label if it genuinely has none.".format(key), file=sys.stderr)
        return 3

    path = gate.sentinel_path(project, key)
    kind = "review" if args.review else "run"
    print("verify {} ({}) in {}\n$ {}".format(key, kind, cwd, command), flush=True)

    started = time.time()
    try:
        proc = subprocess.run(["bash", "-c", command], cwd=cwd, capture_output=True,
                              text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        _discard(path)
        print("TIMED OUT after {}s -- no evidence written".format(args.timeout), file=sys.stderr)
        return 4
    finished = time.time()

    output = ((proc.stdout or "") + (proc.stderr or "")).rstrip()
    tail = "\n".join(output.splitlines()[-OUTPUT_TAIL_LINES:])
    if tail:
        print(tail)

    if proc.returncode != 0:
        _discard(path)
        print("\nFAILED (exit {}) -- no evidence written".format(proc.returncode),
              file=sys.stderr)
        return 1

    # The project's own checks (`agentChecks` in .claude/gates.json -- the test
    # suite). They run here, in this sandboxed shell, because running them means
    # running the working tree's code, which a hook outside the sandbox must not
    # do. Each result goes into the evidence; the gate requires them all.
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or _main_checkout(cwd) or cwd
    checks, problem = evidence.load_agent_checks(project_dir)
    # A worker's run executes the `always` checks; the reviewer's (--review)
    # executes them all, the full suite included. See evidence.CHECK_WHEN.
    checks = evidence.checks_for(checks, kind)
    if problem:
        _discard(path)
        print("\nproject checks could not be loaded: {}".format(problem), file=sys.stderr)
        return 6
    results = {}
    for check in checks:
        argv = evidence.resolve_command(check["command"], cwd, project_dir)
        print("\nproject check '{}'\n$ {}".format(check["name"], " ".join(argv)), flush=True)
        try:
            done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                  timeout=args.timeout)
        except subprocess.TimeoutExpired:
            _discard(path)
            print("TIMED OUT after {}s -- no evidence written".format(args.timeout),
                  file=sys.stderr)
            return 4
        except OSError as exc:
            _discard(path)
            print("could not start: {} -- no evidence written".format(exc), file=sys.stderr)
            return 6
        shown = ((done.stdout or "") + (done.stderr or "")).rstrip().splitlines()
        if shown:
            print("\n".join(shown[-(8 if done.returncode == 0 else OUTPUT_TAIL_LINES):]))
        results[check["name"]] = done.returncode
        if done.returncode != 0:
            _discard(path)
            print("\nFAILED: project check '{}' exited {} -- no evidence written".format(
                check["name"], done.returncode), file=sys.stderr)
            return 1
    finished = time.time()

    # Hashed after the run: a command that rewrites files (a formatter) leaves
    # the tree in the state the evidence must describe.
    tree = evidence.tree_hash(cwd, issue.get("files") or [])
    if tree is None:
        _discard(path)
        print("\ncould not hash the issue's files in {} -- is this a git repository?".format(cwd),
              file=sys.stderr)
        return 5

    evidence.write_record(path, evidence.make_record(
        key, kind, command, 0, tree, started, finished, output_tail=tail, checks=results))
    print("\nPASSED in {:.1f}s -- {} evidence recorded for {}".format(
        finished - started, kind, key))
    return 0


def _main_checkout(cwd):
    """Root of the main checkout when `cwd` is a linked worktree, else None.

    The gate configuration is read from the main checkout so that a branch
    cannot quietly drop a project check for itself.
    """
    try:
        import team_hook_common
        common = team_hook_common.git_common_dir(cwd)
        return os.path.dirname(common) if common else None
    except Exception:
        return None


def _discard(path):
    try:
        os.remove(path)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
