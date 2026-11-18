#!/usr/bin/env python3
"""SessionStart hook -- tell the session, up front, what is wrong with the harness.

Runs the cheap subset of the preflight (no `claude` CLI calls, no hook probes)
and prints anything that is not healthy. SessionStart stdout is added to the
session's context, so the lead reads it before planning a run.

Never blocks: a session must always be able to start, if only to repair the
harness. `scripts/doctor.py` is the full check and the one that exits non-zero.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    import selfcheck
    problems = [r for r in selfcheck.fast_checks() if r.status != selfcheck.OK]
    if not problems:
        return
    lines = ["[harness self-test] {} issue(s) found at session start:".format(len(problems))]
    for r in problems:
        lines.append("  [{}] {}: {}".format(r.status.upper(), r.name, r.message))
    lines.append("Run `python3 scripts/doctor.py` for the full preflight before an "
                 "unattended run. Failures mean guardrails are not in force.")
    print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # never blocks a session from starting
        print("[harness self-test] could not run: {}".format(exc))
    sys.exit(0)
