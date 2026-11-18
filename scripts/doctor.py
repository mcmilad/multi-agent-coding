#!/usr/bin/env python3
"""Preflight for the agent harness: is it safe and able to run?

    python3 scripts/doctor.py                         # core profile, full checks
    python3 scripts/doctor.py --profile serverless,frontend
    python3 scripts/doctor.py --fast                  # no `claude` CLI calls, no hook probes
    python3 scripts/doctor.py --json

Checks, in order: settings (hooks exist, no credential in `env`, deny rules,
sandbox), the session environment (no token-shaped values), the Jira config
(something is actually gated), the project checks, the state directory and
mirror, recent hook errors in the audit log, every hook entry point, the AWS
sandbox declaration, and the plugins, MCP servers and CLIs the selected
profiles need (see .claude/capabilities.json).

Every guardrail in this repository fails open, so a broken one is silent. Run
this before an unattended run.

Exit codes:
  0  no failures (warnings may be present)
  1  at least one failure: do not start a run
  2  usage error
"""
import argparse
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, ".claude", "hooks"))
os.environ.setdefault("CLAUDE_PROJECT_DIR", _REPO)
import selfcheck  # noqa: E402

_MARK = {selfcheck.OK: "ok  ", selfcheck.WARN: "WARN", selfcheck.FAIL: "FAIL"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", default="core",
                        help="comma-separated capability profiles (default: core)")
    parser.add_argument("--fast", action="store_true",
                        help="only the cheap checks run at session start")
    parser.add_argument("--no-live", action="store_true",
                        help="skip the `claude` CLI queries for plugins and MCP servers")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    profiles = tuple(p.strip() for p in args.profile.split(",") if p.strip())
    if "core" not in profiles:
        profiles = ("core",) + profiles
    results = (selfcheck.fast_checks() if args.fast
               else selfcheck.full_checks(profiles, live=not args.no_live))
    verdict = selfcheck.worst(results)

    if args.json:
        print(json.dumps({"verdict": verdict, "profiles": list(profiles),
                          "results": [r._asdict() for r in results]}, indent=2))
    else:
        for r in results:
            print("[{}] {:<24} {}".format(_MARK[r.status], r.name, r.message))
        counts = {s: sum(1 for r in results if r.status == s)
                  for s in (selfcheck.FAIL, selfcheck.WARN, selfcheck.OK)}
        print("\n{} failure(s), {} warning(s), {} ok".format(
            counts[selfcheck.FAIL], counts[selfcheck.WARN], counts[selfcheck.OK]))
    return 1 if verdict == selfcheck.FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
