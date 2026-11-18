#!/usr/bin/env python3
"""PreToolUse hook on the Agent tool -- a worker cannot spawn itself a role.

The other hooks decide what a caller may do from its identity: a reviewer may
close an issue, devops may change AWS, the owner of a claim lock may write its
files. That identity is the name the agent was spawned with (see
team_hook_common.caller_identity). So whoever chooses spawn names chooses
roles -- and that must be the lead and nobody else. Without this hook a coding
worker could spawn `review-9` and have it close the worker's own issue, or
spawn `devops-9` to deploy.

Rule: only the lead (a call with no agent identity) may spawn an agent whose
name or type carries a role. A worker may still spawn helpers -- an explorer, a
simplifier, a specialised reviewer subagent -- under names that mean nothing to
the gates; those helpers get no privileges.

FAIL CLOSED once the caller is known to be a worker; FAIL OPEN on an
unparseable payload.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import (  # noqa: E402
    read_payload, allow, block, audit, as_dict, tool_input_of, caller_identity,
    role_of_teammate, ROLES,
)

EVENT = "PreToolUse"
WATCHED = ("Agent", "Task")
# Agent definitions that are roles in this harness, whatever instance name is used.
ROLE_AGENT_TYPES = ("fullstack-agent",) + tuple(role + "-agent" for role in ROLES)


def carries_role(value):
    """Does a spawn name or agent type confer a role the gates act on?"""
    if not isinstance(value, str) or not value.strip():
        return False
    value = value.strip()
    return (value in ROLE_AGENT_TYPES or role_of_teammate(value) is not None
            or value.lower() in ("lead", "team-lead"))


def main():
    p = read_payload()
    if p.get("tool_name") not in WATCHED:
        allow()
    tool_input, unreadable = tool_input_of(p)
    if unreadable:
        allow()
    who = caller_identity(p)
    if who["is_lead"]:
        allow()

    try:
        tool_input = as_dict(tool_input)
        for field in ("name", "subagent_type"):
            value = tool_input.get(field)
            if carries_role(value):
                block(EVENT, p, (
                    "[spawn-guard] Blocked: a worker cannot spawn an agent with {} '{}'. "
                    "Names and agent types that carry a role (coding, frontend, backend, devops, review, sa, "
                    "the lead) decide what the gates allow, so only the lead assigns them. "
                    "You are '{}'. If you need that role's work done, comment on the issue "
                    "and message the lead; a helper under a neutral name is fine."
                ).format("name" if field == "name" else "type", value,
                         who["name"] or "an unnamed worker"))
        allow(EVENT, p, reason="worker spawned a helper with no role")
    except SystemExit:
        raise
    except Exception as e:
        audit(EVENT, {}, "block", reason="spawn-guard error (fail-closed): {}".format(e))
        sys.stderr.write("[spawn-guard] Internal error; blocked rather than allow a worker "
                         "spawn unchecked: {}\n".format(e))
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # nothing was established about the call
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
