#!/usr/bin/env python3
"""PreToolUse hook on the Atlassian MCP's generic runners -- no side door past the gates.

The Atlassian MCP exposes `executeWrite` / `executeDestructive`, which run any
operation by name. The gates in this directory are wired to the primary tools
(`createJiraIssue`, `editJiraIssue`, `transitionJiraIssue`): a transition sent
as `executeWrite({name: "transitionJiraIssue", ...})` would reach Jira without
meeting the verification gate, the format check or the mirror.

So an operation the gates police must be issued through its primary tool, where
they apply. Through a generic runner it is blocked, for the configured project
and for any call whose project cannot be told. Destructive operations on issues
are the lead's alone. Everything else passes untouched.

FAIL OPEN on an unparseable payload.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import (  # noqa: E402
    read_payload, allow, block, audit, as_dict, tool_input_of, caller_identity, jira_op,
)
import jira_mirror  # noqa: E402

EVENT = "PreToolUse"
RUNNERS = ("executeWrite", "executeDestructive")
PRIMARY = ("createJiraIssue", "editJiraIssue", "transitionJiraIssue")
# Operations that change an issue's status, shape or existence under another name.
_ISSUE_MUTATION = re.compile(
    r"(transition|bulk|convert|move|clone|delete|archive).*(issue|workitem)|"
    r"(issue|workitem).*(transition|bulk|convert|move|clone|delete|archive)", re.I)


def main():
    p = read_payload()
    runner = jira_op(p.get("tool_name"))
    if runner not in RUNNERS:
        allow()
    tool_input, unreadable = tool_input_of(p)
    if unreadable:
        allow()
    name = str(tool_input.get("name") or "")
    inputs = as_dict(tool_input.get("inputs"))
    project = jira_mirror.load_config().get("projectKey")
    if not project or not name:
        allow()

    policed = name in PRIMARY or bool(_ISSUE_MUTATION.search(name))
    if not policed:
        allow()

    # Scope: the configured project, or unknown. Another project is not ours.
    key = inputs.get("issueIdOrKey") or inputs.get("issueKey") or ""
    target = inputs.get("projectKey") or jira_mirror.issue_project(key)
    if target and str(target).upper() != str(project).upper():
        allow(EVENT, p, reason="generic runner targets another project -- not policed")

    if name in PRIMARY:
        block(EVENT, p, (
            "[jira-execute-guard] `{name}` must be called through its own tool "
            "(`…__{name}`), not through `{runner}`. The verification gate, the format "
            "check and the local mirror are attached to the primary tool; sent this way "
            "the call would skip all three."
        ).format(name=name, runner=runner))

    who = caller_identity(p)
    if who["is_lead"]:
        allow(EVENT, p, reason="lead issued {} through {}".format(name, runner))
    block(EVENT, p, (
        "[jira-execute-guard] `{}` changes issues in bulk or irreversibly and is the "
        "lead's to run (you are '{}'). Comment what you need on the issue and message "
        "the lead."
    ).format(name, who["name"] or "an unnamed worker"))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL OPEN
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
