#!/usr/bin/env python3
"""Rebuild or refresh the local Jira mirror from a saved Jira response.

The mirror is what the hooks read (a hook subprocess holds no Jira
credentials). It is normally kept current by the journal hook, which records
every write made through the Atlassian MCP and every issue it sees in a read.
This script is the recovery path for when that is not enough: the mirror file
was lost, the hooks were not installed when the backlog was authored, or the
board was edited by hand.

Input is JSON, from a file or stdin, in any shape Jira hands back:
  - a search result:   {"data": {"issues": [...]}}  or  {"issues": [...]}
  - a single issue:    {"data": {"key": ..., "fields": {...}}}
  - a bare list of issues
Each issue needs `key`; `fields.summary`, `fields.status.name`, `fields.labels`,
`fields.description` and `fields.issuelinks` are recorded when present.

Usage:
  python3 scripts/mirror_resync.py saved.json            # merge into the mirror
  python3 scripts/mirror_resync.py --replace saved.json  # start from this alone
  ... | python3 scripts/mirror_resync.py -

This is an operator's tool, run from your own terminal. The mirror is what the
verification gate trusts for an issue's `Run:` command and its bypass labels,
so an agent's sandboxed shell is denied write access to it; run from there this
script says so and exits 4.

Exit codes: 0 synced at least one issue; 1 usage or unreadable input;
2 no Jira project configured; 3 input held no issue of the configured project;
4 the mirror cannot be written from here.
"""
import argparse
import json
import os
import shutil
import sys

_HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      ".claude", "hooks")
sys.path.insert(0, _HOOKS)
import jira_mirror  # noqa: E402
import jira_mirror_journal  # noqa: E402


def resync(payload, project, replace=False):
    """Write sync events for every issue of `project` in `payload`. Returns the keys."""
    snaps = [s for s in jira_mirror_journal.snapshots_in(payload)
             if jira_mirror.issue_project(s["key"]) == project]
    if not snaps:
        return []
    path = jira_mirror.mirror_path(project)
    if replace and os.path.exists(path):
        shutil.copyfile(path, path + ".bak")
        os.remove(path)
    for snap in snaps:
        jira_mirror.append_event(project, dict(snap, op="sync"))
    return [s["key"] for s in snaps]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", help="JSON file, or - for stdin")
    parser.add_argument("--replace", action="store_true",
                        help="discard the existing mirror (kept as .bak) before writing")
    args = parser.parse_args(argv)

    project = jira_mirror.load_config().get("projectKey")
    if not project:
        print("no projectKey in the Jira config -- run scripts/jira_bootstrap.py discover",
              file=sys.stderr)
        return 2
    try:
        raw = sys.stdin.read() if args.source == "-" else open(args.source).read()
        payload = json.loads(raw)
    except Exception as exc:
        print("cannot read JSON from {}: {}".format(args.source, exc), file=sys.stderr)
        return 1

    # append_event never raises (a hook must not break on a journal write), so
    # find out here whether a write can land at all.
    path = jira_mirror.mirror_path(project)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a"):
            pass
    except OSError as exc:
        print("cannot write the mirror at {} ({}). It is written only by the hooks and by "
              "the operator: run this from your own terminal, not an agent's shell.".format(
                  path, exc.strerror or exc), file=sys.stderr)
        return 4

    keys = resync(payload, project, replace=args.replace)
    if not keys:
        print("no issue of project {} found in the input".format(project), file=sys.stderr)
        return 3
    print("synced {} issue(s) into {}: {}".format(
        len(keys), jira_mirror.mirror_path(project), ", ".join(keys)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
