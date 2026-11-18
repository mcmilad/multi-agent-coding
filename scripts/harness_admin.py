#!/usr/bin/env python3
"""Operator-only decisions for the agent harness. Run it in YOUR terminal.

    python3 scripts/harness_admin.py sandbox --account 123456789012 --regions ap-southeast-2
    python3 scripts/harness_admin.py approve 1a2b3c4d5e6f7a8b
    python3 scripts/harness_admin.py show
    python3 scripts/harness_admin.py revoke 1a2b3c4d5e6f7a8b

`sandbox` declares the AWS account and regions agents may change without
asking. `approve` lets one exact command through that the AWS guard blocked;
the guard prints the id to approve. Both are recorded under
~/.claude/harness/<repo-id>/, outside the repository, where an agent's sandboxed
shell cannot write and the guardrail guard refuses the file-editing tools.

This script refuses to run inside a Claude Code session: these are the
decisions the harness exists to leave with a person.

Exit codes: 0 done; 1 refused (run from an agent session); 2 usage error.
"""
import argparse
import json
import os
import re
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, ".claude", "hooks"))
os.environ.setdefault("CLAUDE_PROJECT_DIR", _REPO)
import team_hook_common as common  # noqa: E402

AGENT_SESSION_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CHILD_SESSION")


_CLAUDE_PROCESS = re.compile(r"(^|/)claude(-code)?(/|\s|$)|@anthropic-ai/claude-code", re.I)


# Absolute: `ps` looked up on PATH is whatever the caller put first on PATH.
_PS_PATHS = ("/bin/ps", "/usr/bin/ps")


def _ancestors(pid=None, limit=40):
    """Command lines of this process's ancestors, nearest first, up to init.

    Returns None unless the WHOLE chain was read: when `ps` is missing or
    refused, when any lookup on the way up fails, or when the chain is deeper
    than `limit`. An operator's terminal can always run `ps` to the top; the
    sandbox an agent's shell runs in cannot, so "unreadable" is itself evidence
    and the caller refuses on it.
    """
    import subprocess
    ps = next((p for p in _PS_PATHS if os.path.exists(p)), None)
    if ps is None:
        return None
    commands = []
    pid = os.getppid() if pid is None else pid
    for _ in range(limit):
        if not pid or pid <= 1:
            return commands              # walked all the way up: the list is complete
        try:
            done = subprocess.run([ps, "-o", "ppid=,command=", "-p", str(pid)],
                                  capture_output=True, text=True, timeout=5)
        except Exception:
            return None
        out = done.stdout.strip()
        if done.returncode != 0 or not out:
            # A chain that stops short of the top is not a chain with no Claude
            # in it. A sandbox that shows a process its own shell but not the
            # shell's parent fails here, on the second lookup.
            return None
        parent, _, command = out.partition(" ")
        commands.append(command.strip())
        try:
            pid = int(parent)
        except ValueError:
            return None
    return None                          # deeper than `limit`: not seen to the top


def in_agent_session(environ=None, ancestors=None):
    """Is this being run from inside a Claude Code session?

    Two independent signs, because the first is one an agent can remove:

      - the environment variables Claude Code sets for the shells it starts.
        `env -u CLAUDECODE python3 scripts/harness_admin.py ...` strips them;
      - an ancestor process that is Claude Code itself. A process table that
        cannot be read all the way up counts as a yes, and so does having no
        ancestors at all.

    This is a second line, not the control. A process that orphans an
    intermediate shell (`(sh -c '...' &)`) has a short, complete chain with no
    Claude in it, and that cannot be told from a terminal started by launchd or
    tmux. What stops an agent recording an operator decision is that its shell
    cannot write ~/.claude/harness (the sandbox) and its file tools cannot
    either (the guardrail guard).

    `ancestors` is for tests: a list of command lines, or None for "unreadable".
    Omit it to read the real process table.
    """
    environ = os.environ if environ is None else environ
    if any(environ.get(name) for name in AGENT_SESSION_MARKERS):
        return True
    commands = _ancestors() if ancestors is None else ancestors
    if not commands:
        # Unreadable, or no ancestor at all: a process reparented to init
        # (setsid, a double fork) has shed its ancestry on purpose or by
        # accident, and either way there is no terminal to vouch for it.
        return True
    return any(_CLAUDE_PROCESS.search(str(command)) for command in commands)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sandbox", help="declare the AWS sandbox account and regions")
    s.add_argument("--account", required=True, help="12-digit AWS account id")
    s.add_argument("--regions", required=True, help="comma-separated regions")
    a = sub.add_parser("approve", help="approve one blocked AWS command by its id")
    a.add_argument("digest")
    r = sub.add_parser("revoke", help="withdraw an approval")
    r.add_argument("digest")
    sub.add_parser("show", help="show the sandbox declaration and standing approvals")
    args = parser.parse_args(argv)

    if in_agent_session():
        print("refused: this is an operator decision and must be made outside a Claude Code "
              "session. Open your own terminal and run it there.", file=sys.stderr)
        return 1

    root = common.human_dir()
    approvals = os.path.join(root, "approvals")
    sandbox = os.path.join(root, "aws-sandbox.json")

    if args.command == "sandbox":
        regions = [x.strip() for x in args.regions.split(",") if x.strip()]
        if not re.fullmatch(r"\d{12}", args.account) or not regions or not all(
                re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", x) for x in regions):
            print("need a 12-digit --account and --regions like ap-southeast-2,us-east-1",
                  file=sys.stderr)
            return 2
        os.makedirs(root, mode=0o700, exist_ok=True)
        with open(sandbox, "w") as fh:
            json.dump({"accountId": args.account, "regions": regions}, fh, indent=2)
            fh.write("\n")
        print("sandbox declared: account {} in {}\n  {}".format(
            args.account, ", ".join(regions), sandbox))
        return 0

    if args.command in ("approve", "revoke"):
        if not re.fullmatch(r"[0-9a-f]{16}", args.digest):
            print("the id is the 16 hex characters the AWS guard printed", file=sys.stderr)
            return 2
        marker = os.path.join(approvals, args.digest + ".approved")
        if args.command == "approve":
            os.makedirs(approvals, mode=0o700, exist_ok=True)
            with open(marker, "w") as fh:
                fh.write(time.strftime("approved %Y-%m-%dT%H:%M:%SZ\n", time.gmtime()))
            print("approved {} (stands until revoked)".format(args.digest))
        else:
            try:
                os.remove(marker)
                print("revoked {}".format(args.digest))
            except FileNotFoundError:
                print("no such approval: {}".format(args.digest))
        return 0

    print("state directory: {}".format(root))
    try:
        print("sandbox: {}".format(json.dumps(json.load(open(sandbox)))))
    except Exception:
        print("sandbox: none declared -- every AWS change needs an approval")
    try:
        names = sorted(n[:-len(".approved")] for n in os.listdir(approvals)
                       if n.endswith(".approved"))
    except FileNotFoundError:
        names = []
    print("approvals: {}".format(", ".join(names) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
