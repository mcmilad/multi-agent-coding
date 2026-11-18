#!/usr/bin/env python3
"""PreToolUse hook on transitionJiraIssue -- the verification gate.

A transition into a gated status (In Review, Done) is blocked unless an
evidence record exists at
    <state root>/verified/<PROJECT>/<ISSUE-KEY>.verified
written by scripts/verify_issue.py: the issue's `Run:` command and every
project check exited 0 against the issue's files as they are now. One record
authorizes at most one SUCCESSFUL transition. ("Sentinel" below is the older
name for that record.)

This hook catches the transition nobody verified. It is not a control against
forgery -- the record is the agent's own -- and it runs no project tool: CI on
the pull request is the independent check.

TWO-PHASE CONSUME. This hook does not delete the sentinel -- it atomically
renames it to <ISSUE-KEY>.inflight. The rename is the consume: it takes effect
immediately, so a second concurrent transition finds no .verified and is
blocked. jira_transition_sentinel_finalize.py (PostToolUse) then deletes the
.inflight on affirmative success, or restores it to .verified otherwise.

Why not delete here: the MCP call had not happened yet. A 401, 429, 5xx or
timeout destroyed the attestation and permanently blocked the retry -- observed
live on AGENT-11 (2026-08-05T05:47:48Z), where the gate allowed and consumed but
no transition ever landed, so the issue could never close. Why not move the
whole consume to PostToolUse: that would leave the gate check-only for the
duration of the round trip, letting one sentinel authorize two gated
transitions.

If the tool is never invoked at all (denied, cancelled), no PostToolUse fires
and the .inflight lingers; it becomes reclaimable after INFLIGHT_STALE_SECONDS
so a legitimate retry is never permanently wedged.

Why a sentinel rather than reading the transcript: the hook payload's
transcript belongs to whichever session triggered it, and a hook cannot observe
a teammate actually running tests. The sentinel is the teammate's attestation.

transitionJiraIssue takes a TRANSITION ID, not a target status name, so the id
is resolved through the transitions map that bootstrap discovers. An id that is
not in the map is blocked: it cannot be shown to be ungated.

Bypass: the skip-verify label, honoured only when the lead applied it.
FAIL OPEN before the call is known to be a gated transition (unparseable
payload, missing config, another project). FAIL CLOSED after that: an internal
error, or no verdict within the watchdog's time, blocks.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import (  # noqa: E402
    read_payload, allow, block, audit, as_dict, tool_input_of,
    safe_path_component, verified_dir,
    transition_target, transition_name,
    transition_id as _transition_id, jira_op, caller_identity, is_done_status,
)
import evidence  # noqa: E402
import jira_mirror  # noqa: E402

EVENT = "PreToolUse"
TRANSITION = "mcp__plugin_atlassian_atlassian__transitionJiraIssue"
DEFAULT_GATED = ("In Review", "Done")

# How long an .inflight may sit before a retry may reclaim it. Covers a normal
# MCP round trip with wide margin; only reached when PostToolUse never fired,
# i.e. the tool call was denied or cancelled rather than executed.
INFLIGHT_STALE_SECONDS = 300


# --- gated-ness resolution -------------------------------------------------
# Shared with jira_transition_sentinel_finalize.py, which imports these rather
# than re-deriving them. The finalizer must act on exactly the transitions this
# gate consumes a sentinel for and no others: a second copy of this rule lets
# the two phases of the consume drift, and an ungated PostToolUse then spends
# or resurrects an .inflight that belongs to a gated transition.

def target_status(cfg, tool_input):
    """The status this transitionJiraIssue call lands the issue in, or None.

    transitionJiraIssue takes a TRANSITION ID, not a target status name, so the
    id is resolved through the transitions map that bootstrap discovers. An id
    that is absent or not in the map resolves to unknown -- and unknown is never
    gated, so it never blocks here and is never finalized there.

    as_dict on `transition` too, not `or {}`: every field here is model-authored,
    and a truthy non-dict (transition arriving as the bare id STRING) sails past
    `or {}` and raises on the next .get(). Both hooks would then resolve this
    call through their outer fail-open handler instead of through this function
    -- exiting 0 with an empty audit payload, indistinguishable from "not
    gated". This must be total so the two phases agree even on junk.
    """
    # Delegates to the shared resolver, which reads both MCP input shapes
    # (`transitionId` / `transition.id`) and a status-named `transitionName`.
    # Reading only `transition.id` resolved every call from the current MCP to
    # "unknown", which is never gated -- the whole gate was silently off.
    return transition_target(cfg, tool_input)


_GATED_PATH = False   # set once main() knows the transition is gated


def review_only_statuses(cfg):
    """Statuses only a reviewer may move an issue into. Default: Done.

    Configurable (`reviewOnlyStatuses`) because boards differ; an explicit list
    is honoured as given, including an empty one.
    """
    configured = as_dict(cfg).get("reviewOnlyStatuses")
    return configured if isinstance(configured, list) else ["Done"]


def is_review_only(cfg, target):
    """Closing is the reviewer's, under any name the board gives it.

    An explicit `reviewOnlyStatuses` list is the whole answer. Otherwise every
    status that finishes an issue counts, not only the one named Done.
    """
    if isinstance(as_dict(cfg).get("reviewOnlyStatuses"), list):
        return target in cfg["reviewOnlyStatuses"]
    return target in review_only_statuses(cfg) or is_done_status(cfg, target)


def project_dir(payload):
    """Root of the main checkout: where the tracked gate configuration lives."""
    return os.environ.get("CLAUDE_PROJECT_DIR") or as_dict(payload).get("cwd") or os.getcwd()


def work_dir(payload):
    """Where the caller is working: its worktree if it has one, else the project."""
    cwd = as_dict(payload).get("cwd")
    return cwd if isinstance(cwd, str) and os.path.isdir(cwd) else project_dir(payload)


def evidence_instructions(issue_key, required_kind):
    flag = " --review" if required_kind == "review" else ""
    return (
        "Produce evidence, then retry:\n"
        "  python3 scripts/verify_issue.py {}{}        (from the repository root)\n"
        "It runs this issue's Run: command and records the result against the "
        "current content of the issue's files. Do not write the file by hand: a "
        "record that does not match is refused.{}\n"
        "If this issue genuinely has no runnable verification, ask the lead to "
        "apply the skip-verify label."
    ).format(
        issue_key, flag,
        "\nClosing needs review evidence: run it with --review after the PASS verdict."
        if required_kind == "review" else "")


def is_gated(cfg, target):
    """Whether landing in `target` requires a consumed sentinel.

    isinstance, not truthiness: `x or DEFAULT` cannot tell "discovery found no
    gated status on this board" ([]) from "nobody configured this" (absent).
    Falling back on [] gates two status names the board does not have, so
    nothing is really gated while the audit log claims a gate is in force. An
    explicit [] is honoured as-is; bootstrap fails at setup (exit 5) rather
    than shipping an empty gate set.
    """
    configured = cfg.get("gatedStatuses")
    if isinstance(configured, list) and not configured:
        return False                       # an explicit empty list is honoured as-is
    gated = configured if isinstance(configured, list) else list(DEFAULT_GATED)
    # A status that finishes an issue is gated whatever it is called: a board
    # with both Done and Closed otherwise has an ungated way to finish.
    return target in gated or is_done_status(cfg, target)


def _sentinel_base(project, issue_key):
    # Both components come from the payload and are attacker-influenceable; this
    # path is passed to os.rename/os.remove, so each is reduced to a single safe
    # path component that cannot escape the verified directory.
    return os.path.join(
        verified_dir(),
        safe_path_component(project, default="_noproject"),
        safe_path_component(issue_key, default="_noissue"),
    )


def sentinel_path(project, issue_key):
    return _sentinel_base(project, issue_key) + ".verified"


def inflight_path(project, issue_key):
    return _sentinel_base(project, issue_key) + ".inflight"


def main():
    p = read_payload()
    if jira_op(p.get("tool_name")) != "transitionJiraIssue":
        allow()

    # tool_input_of, not as_dict: a tool_input arriving as a JSON STRING is a
    # documented model failure mode, and as_dict() turns it into {} -- whereupon
    # issueIdOrKey is empty, the project-scope check below decides the issue is
    # not ours, and the gate exits 0. An unverified transition sails through AND
    # the audit log blames "another project", which is false and sends whoever
    # reads it looking in the wrong place. tool_input_of decodes the string
    # instead, so the gate judges the real issue key.
    tool_input, unreadable = tool_input_of(p)
    if unreadable:
        # Genuinely unreadable: the issue key is unknowable, so we cannot tell
        # an issue in our project from the operator's real client work. Fail
        # open per the house rule -- but name the actual cause. (Nothing is
        # smuggled past the gate this way: a tool_input the hook cannot parse
        # as JSON is not one the MCP server can act on either.)
        allow(EVENT, p, reason=(
            "verification gate did not run: {} -- issue key and project "
            "unknowable, fail-open".format(unreadable)))
    cfg = jira_mirror.load_config()
    project = cfg.get("projectKey")
    if not project:
        allow(EVENT, p, reason="no projectKey configured -- fail-open")

    # Sanitize before the project check (not just before the path join): a
    # crafted key like 'AGENT-../../../../etc/passwd' still has the real
    # project as its literal prefix, which would pass an unsanitized project
    # check and only get caught at path-build time -- by then we've already
    # decided this issue is "ours" and gone down the blocking path. Reducing
    # to a safe component first means a traversal payload is judged by what it
    # resolves to (an unrecognized bare key, project-less), so it falls out at
    # the project-scope check instead, before any path is built or blocked on.
    raw_issue = tool_input.get("issueIdOrKey")
    if jira_mirror.is_numeric_issue_id(raw_issue):
        # A numeric id names no project, so scope cannot be established from it.
        # Treating it as "another project" let any agent walk past this gate by
        # passing the id instead of the key. Only refuse when the transition is
        # one this gate would actually police; everything else stays allowed.
        numeric_target = target_status(cfg, tool_input)
        if numeric_target and is_gated(cfg, numeric_target):
            block(EVENT, p, (
                "Transition to '{}' was addressed by numeric issue id '{}'. The "
                "verification gate is scoped by issue key and cannot evaluate an "
                "id. Retry with the issue key (e.g. {}-123).".format(
                    numeric_target, raw_issue, project)))
        allow(EVENT, p, reason="numeric issue id on an ungated transition -- not gated")
    # Normalised first: Jira accepts 'ag-14' and issue URLs, and compared raw
    # those read as a foreign project.
    issue_key = safe_path_component(
        jira_mirror.normalize_issue_key(raw_issue), default="_noissue"
    )
    if jira_mirror.issue_project(issue_key) != project:
        allow(EVENT, p, reason="issue belongs to another project -- not gated")

    # as_dict here too: this line is only message material, but it runs BEFORE
    # target_status and would otherwise raise past it, undoing its totality.
    transition_id = _transition_id(tool_input)
    target = target_status(cfg, tool_input)
    if not target:
        name = transition_name(tool_input)
        if name and not transition_id:
            # A named transition that does not resolve to a known status cannot
            # be shown to be ungated. Failing open here would make
            # `transitionName` a one-argument bypass of the whole gate.
            block(EVENT, p, (
                "Cannot tell where transition name '{}' lands, so the verification "
                "gate cannot evaluate it. Retry with transitionId (from "
                ".claude/jira-config.json `transitions`).".format(name)))
        if transition_id:
            # An id the discovered map does not know cannot be shown to be
            # ungated. It used to be allowed, so a transition added to the board
            # after bootstrap was an ungated way into any status.
            block(EVENT, p, (
                "Transition id '{}' is not in the discovered transition map, so the "
                "verification gate cannot tell where it lands. If the board changed, the "
                "operator re-runs `scripts/jira_bootstrap.py discover`; otherwise use an "
                "id from .claude/jira-config.json `transitions`.".format(transition_id)))
        allow(EVENT, p, reason="no transition requested (sprint or backlog move only)")

    if not is_gated(cfg, target):
        allow(EVENT, p, reason="target status {} is not gated".format(target))

    # From here on the transition is one this gate polices. An internal error
    # past this point must not read as permission (see the handler at the end).
    global _GATED_PATH
    _GATED_PATH = True
    arm_watchdog()

    who = caller_identity(p)

    # Closing is the reviewer's. A worker of any other role moving an issue to a
    # review-only status is the self-close this whole gate exists to prevent;
    # it used to be a convention nothing enforced. The lead (no agent identity:
    # the main session, or a solo run) is allowed and the audit log says so.
    if is_review_only(cfg, target) and not who["is_lead"] and who["role"] != "review":
        block(EVENT, p, (
            "Transition of {} to '{}' blocked: only a reviewer may close an issue. "
            "You are '{}'. Move it to 'In Review' and stop there; the review "
            "synthesizer closes it after a PASS verdict."
        ).format(issue_key, target, who["name"] or "an unnamed worker"))

    state = jira_mirror.load_state(project)
    issue = state.get(issue_key)

    if issue and "skip-verify" in (issue.get("labels") or []):
        applied_by = jira_mirror.bypass_author(issue, "skip-verify")
        if applied_by == "lead":
            allow(EVENT, p, reason="skip-verify label applied by the lead")
        block(EVENT, p, (
            "Transition of {} to '{}' blocked: the skip-verify label is only honoured "
            "when the lead applied it, and this one was applied by {}. Ask the lead to "
            "re-apply it, or run the verification."
        ).format(issue_key, target, applied_by or "an unrecorded author"))

    if issue is None:
        block(EVENT, p, (
            "Transition of {} to '{}' blocked: the gate has no record of this issue, so "
            "it does not know its Run: command or its files. Read the issue first "
            "(getJiraIssue with the default view, or a JQL search that returns it) -- "
            "the read is recorded automatically -- then retry."
        ).format(issue_key, target))

    path = sentinel_path(project, issue_key)
    inflight = inflight_path(project, issue_key)
    cwd = work_dir(p)
    required_kind = "review" if is_review_only(cfg, target) else None
    how_to = evidence_instructions(issue_key, required_kind)

    if not os.path.exists(path):
        if os.path.exists(inflight):
            try:
                stale = (time.time() - os.path.getmtime(inflight)) > INFLIGHT_STALE_SECONDS
            except OSError:
                stale = False
            if stale:
                # PostToolUse never fired, so the previous call never executed.
                # Put the evidence back so this attempt is validated like any
                # other, instead of waving the retry through unexamined.
                try:
                    os.rename(inflight, path)
                except OSError:
                    pass
            else:
                block(EVENT, p, (
                    "Transition of {} to '{}' blocked: a transition using this evidence "
                    "is already in flight. If that call failed, the evidence is restored "
                    "automatically -- retry in a moment."
                ).format(issue_key, target))
        if not os.path.exists(path):
            block(EVENT, p, (
                "Transition of {} to '{}' blocked by the verification gate: no evidence "
                "record at\n  {}\n\n{}"
            ).format(issue_key, target, path, how_to))

    # The project's checks (lint, tests) run in the agent's sandboxed shell and
    # are recorded in the evidence; the gate requires each to be there and
    # passing. This hook runs no project tool at all: it is outside the sandbox,
    # and a tool run here over a tree the worker writes acts for the worker.
    agent_checks, checks_problem = evidence.load_agent_checks(project_dir(p))
    if checks_problem:
        block(EVENT, p, (
            "Transition of {} to '{}' blocked: the project checks could not be loaded "
            "({}). This is a harness configuration fault -- report it to the lead."
        ).format(issue_key, target, checks_problem))

    record = evidence.load_record(path)
    current_tree = evidence.tree_hash(cwd, issue.get("files") or [])
    ok, why = evidence.validate(record, issue_key, issue.get("run") or "", current_tree,
                                required_kind=required_kind,
                                required_checks=[c["name"] for c in evidence.checks_for(
                                    agent_checks, required_kind or "run")])
    if not ok:
        block(EVENT, p, (
            "Transition of {} to '{}' blocked by the verification gate: {}.\n\n{}"
        ).format(issue_key, target, why, how_to))

    try:
        # Atomic consume. A peer that renamed first makes this raise, and that
        # peer now owns the one transition this evidence authorizes.
        os.rename(path, inflight)
        # A rename keeps the file's mtime, and the staleness check above reads
        # mtime. Evidence older than the stale window was therefore "stale" the
        # instant it was consumed, and a second transition arriving while the
        # first was in flight put it back and used it again. Stamp the moment
        # of consumption.
        try:
            os.utime(inflight, None)
        except OSError:
            pass
    except OSError:
        block(EVENT, p, (
            "Transition of {} to '{}' blocked: its evidence was consumed by another "
            "transition that is still in flight. One record authorizes one "
            "transition -- wait for that one to land, or verify again."
        ).format(issue_key, target))
    allow(EVENT, p, reason="verified via {} evidence for {} -> {} (by {})".format(
        record.get("kind"), issue_key, target, who["name"] or "lead"))


# Claude Code cancels a hook that runs past its timeout and lets the tool call
# proceed. For this hook that is the one outcome that must not happen: a stall
# would be a transition nobody verified. So once the gate knows it is policing
# the call it sets its own alarm, shorter than the `timeout` on its entry in
# settings.json, and when the alarm fires it blocks.
WATCHDOG_ENV = "CLAUDE_HARNESS_GATE_WATCHDOG_SECONDS"
WATCHDOG_DEFAULT_SECONDS = 240


def watchdog_seconds():
    """The default, or a SHORTER value from the environment. Never longer: the
    settings timeout is fixed, and a watchdog that outlasts it guards nothing."""
    try:
        asked = int(os.environ.get(WATCHDOG_ENV, ""))
    except ValueError:
        return WATCHDOG_DEFAULT_SECONDS
    return asked if 0 < asked < WATCHDOG_DEFAULT_SECONDS else WATCHDOG_DEFAULT_SECONDS


def _watchdog_fired(_signum, _frame):
    try:
        audit(EVENT, {}, "block", reason="gate watchdog: no verdict in {}s (fail-closed)".format(
            watchdog_seconds()))
        sys.stderr.write(
            "Verification gate did not reach a verdict in {}s and blocked the transition "
            "rather than let it through unverified. Something it reads is stalling it -- "
            "check the issue's files for a link to a device or pipe. Report this to the "
            "lead.\n".format(watchdog_seconds()))
        sys.stderr.flush()
    finally:
        os._exit(2)


def arm_watchdog():
    import signal
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _watchdog_fired)
        signal.alarm(watchdog_seconds())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        if _GATED_PATH:
            # FAIL CLOSED here, and only here. Every other hook fails open, but
            # this one protects a transition: an exception read as permission
            # would turn any bug in the gate into a silent bypass of it.
            audit(EVENT, {}, "block", reason="gate error (fail-closed): {}".format(e))
            sys.stderr.write(
                "Verification gate hit an internal error and blocked the transition "
                "rather than allow it unverified: {}\nReport this to the lead.\n".format(e))
            sys.exit(2)
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
