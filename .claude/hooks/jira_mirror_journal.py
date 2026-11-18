#!/usr/bin/env python3
"""PostToolUse hook -- journal successful Jira mutations to the local mirror.

Runs AFTER the MCP call so it records what actually succeeded. The mirror is
the only way a hook subprocess can know anything about Jira state: it holds no
OAuth token and cannot call the API.

Scope: only the configured agent project. Issues in any other project (the
operator's real work) are ignored entirely.

Always exits 0. This hook observes; it never gates.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import (  # noqa: E402
    read_payload, allow, audit, as_dict, create_labels, edit_labels,
    tool_input_of, caller_identity,
    transition_id, transition_target as jira_transition_target, jira_op,
)
import jira_mirror  # noqa: E402

EVENT = "PostToolUse"
PREFIX = "mcp__plugin_atlassian_atlassian__"
CREATE = PREFIX + "createJiraIssue"
TRANSITION = PREFIX + "transitionJiraIssue"
EDIT = PREFIX + "editJiraIssue"
COMMENT = PREFIX + "addCommentToJiraIssue"
# The current Atlassian MCP renamed the comment tool; both names are watched.
COMMENT_V2 = PREFIX + "addOrEditJiraIssueComment"
WATCHED = (CREATE, TRANSITION, EDIT, COMMENT, COMMENT_V2)
BYPASS_LABELS = ("skip-verify", "skip-format-check")
SYNC = "sync"   # reads: the response shows Jira's own current state
_OPS = {
    "createJiraIssue": CREATE,
    "transitionJiraIssue": TRANSITION,
    "editJiraIssue": EDIT,
    "addOrEditJiraIssueComment": COMMENT,
    "searchJiraIssuesUsingJql": SYNC,
    "getJiraIssue": SYNC,
}

_ERROR_KEYS = ("error", "errors", "errorMessages")

# Verified live against a real Jira site: the harness
# delivers tool_response as a list of content blocks --
# [{"type": "text", "text": "<issue JSON as a string>"}] -- not the bare MCP
# result object. That is the confirmed, primary shape for createJiraIssue
# responses. The `isinstance(response, dict)` branches in _succeeded/_created_key
# below are a fallback for a bare-object shape this repo has not observed live,
# kept in case the harness ever changes what it hands back.


def _parse_content_block_object(response):
    """Extract the first JSON object embedded in a content-block list.

    The harness does not always hand back the bare MCP result. Live capture
    from a real createJiraIssue call shows it instead wraps the result as a
    list of content blocks:
    `[{"type": "text", "text": "<issue JSON as a string>"}, ...]`.
    Walk the entries and decode the first dict entry whose `text` is a
    string that parses to a JSON object.

    Returns None if `response` is not a list, or no block's `text` decodes
    to an object. None means "could not determine" -- it is NOT proof of
    failure. Only an explicit error marker inside a successfully decoded
    object is treated as a failure signal; an undecodable block is a
    shape-recognition problem for the caller to diagnose, not evidence the
    call itself failed.
    """
    if not isinstance(response, list):
        return None
    for block in response:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _unwrap(obj):
    """Peel the current Atlassian MCP envelope: {"data": {...}} -> {...}.

    Verified live 2026-11-15: createJiraIssue returns {"data": {"id", "key"}}
    and a rejected call returns {"data": {"error": ...}}. Read unwrapped, a
    successful create yielded no key (nothing was journalled, so every issue
    was invisible to the claim gate and the idle check) and a rejected one
    carried no top-level error key (so it read as success).
    """
    if isinstance(obj, dict) and isinstance(obj.get("data"), dict) and not (
            "key" in obj or "id" in obj):
        return obj["data"]
    return obj


# Words that mark a prose response as a failure. A tool that fails can hand back
# a bare string ("Error: 401 Unauthorized") instead of a structured error.
_ERROR_TEXT = re.compile(
    r"\b(error|errors|failed|failure|unauthori[sz]ed|forbidden|denied|rejected|"
    r"invalid|not found|exception|timed out|timeout)\b", re.I)


def _object_failed(obj):
    """True if a decoded response object carries any failure marker."""
    obj = as_dict(obj)
    inner = _unwrap(obj)
    for candidate in (obj, inner):
        if any(candidate.get(k) for k in _ERROR_KEYS):
            return True
        if candidate.get("isError") is True:
            return True
        status = candidate.get("status")
        if isinstance(status, int) and status >= 400:
            return True
    return False


def _succeeded(response):
    """Whether a tool response is evidence the call landed. Strict on purpose.

    The mirror is what every other hook reads, so a failure journalled as a
    success is worse than a success that goes unjournalled: a failed transition
    recorded as 'In Review' made the claim gate treat the issue's files as
    covered and let a second issue declare the same paths.

    `tool_response` arrives as a bare object, as a list of content blocks whose
    text is JSON (the confirmed live shape), or as a plain string:
      - an object, or a block that decodes to one, fails on any error marker --
        an error key, isError, or an HTTP status >= 400 -- at the top level or
        inside the `data` envelope;
      - an empty list or empty value is no evidence and fails;
      - a string that decodes to an object is judged as that object; any other
        string fails if it reads like an error, and otherwise passes;
      - a non-empty list that decodes to nothing is not called a failure here:
        whether a key can be found in it is _created_key's job, which logs its
        own distinguishable reason.
    """
    if isinstance(response, dict):
        return not _object_failed(response)
    if isinstance(response, list):
        if not response:
            return False
        parsed = _parse_content_block_object(response)
        if parsed is not None:
            return not _object_failed(parsed)
        texts = [b.get("text") for b in response
                 if isinstance(b, dict) and isinstance(b.get("text"), str)]
        return not any(_ERROR_TEXT.search(t) for t in texts)
    if isinstance(response, str):
        text = response.strip()
        if not text:
            return False
        try:
            decoded = json.loads(text)
        except Exception:
            decoded = None
        if isinstance(decoded, dict):
            return not _object_failed(decoded)
        return not _ERROR_TEXT.search(text)
    return bool(response)


def parse_files(description):
    """Extract the `Files:` list from an issue description, or None.

    fullstack-agent.md calls sprint-wide Files: disjointness "the sole guarantee
    against conflicts under the shared-tree pool model", but nothing could check
    it: the mirror recorded summary/labels/status and never the description, so
    no hook could see the declared paths. Journalling the parsed list is what
    makes jira_issue_format_check's overlap check possible at all.

    Deliberately forgiving -- a path this fails to parse is simply not enforced,
    which is the status quo, whereas over-parsing would block a valid create.

    Every parsed path is normalised (jira_mirror.normalize_path) so it is
    directly comparable with the os.path.relpath() output claim_gate matches
    against. Unnormalised, a single './'-prefixed or doubled-slash entry
    journalled a string nothing would ever equal, taking that file out of both
    the overlap check and the claim gate with no signal at all.
    """
    if not isinstance(description, str):
        return None
    match = re.search(r"^\s*Files:\s*(.+)$", description, re.M)
    if not match:
        return None
    paths = []
    for raw in match.group(1).split(","):
        path = jira_mirror.normalize_path(raw.strip().strip("`").rstrip(".").strip())
        if path:
            paths.append(path)
    return paths or None


def _files_from_description(description):
    return parse_files(description)


def parse_run(description):
    """The issue's `Run:` verification command, or None.

    Jira stores descriptions as markdown, which can eat backslashes and wrap the
    command in backticks; a fenced or backticked command is unwrapped. The
    mirrored value is what the verification gate compares evidence against.
    """
    if not isinstance(description, str):
        return None
    match = re.search(r"^\s*Run:\s*(.+?)\s*$", description, re.M)
    if not match:
        return None
    command = match.group(1).strip()
    if len(command) >= 2 and command[0] == command[-1] == "`":
        command = command[1:-1].strip()
    return command or None


def _blocked_by(issuelinks):
    """Keys of issues that block this one, from a Jira `issuelinks` array."""
    out = []
    if not isinstance(issuelinks, list):
        return None
    for link in issuelinks:
        link = as_dict(link)
        inward = str(as_dict(link.get("type")).get("inward") or "").lower()
        blocker = as_dict(link.get("inwardIssue")).get("key")
        if blocker and "blocked by" in inward:
            key = jira_mirror.normalize_issue_key(blocker)
            if key not in out:
                out.append(key)
    return out


def snapshot_of(issue):
    """Mirror event fields for one issue object as Jira returns it, or None.

    Used for every response that shows an issue's current state -- a search, a
    single read, the echo of an edit. Only fields actually present are emitted,
    so a compact search row (no description) updates status and labels without
    erasing the mirrored Files: list.
    """
    issue = as_dict(issue)
    key = issue.get("key")
    if not key or "-" not in str(key):
        return None
    fields = as_dict(issue.get("fields"))
    event = {"key": jira_mirror.normalize_issue_key(key)}
    if isinstance(fields.get("summary"), str):
        event["summary"] = fields["summary"]
    status = as_dict(fields.get("status")).get("name")
    if isinstance(status, str) and status:
        event["status"] = status
    if isinstance(fields.get("labels"), list):
        event["labels"] = [str(x) for x in fields["labels"]]
    description = fields.get("description")
    if isinstance(description, str):
        event["files"] = parse_files(description) or []
        event["run"] = parse_run(description) or ""
    links = _blocked_by(fields.get("issuelinks"))
    if links is not None:
        event["blocked_by"] = links
    return event


def snapshots_in(response):
    """Every issue snapshot found in a tool response (search rows or one issue)."""
    if isinstance(response, list) and any(
            isinstance(x, dict) and "key" in x for x in response):
        # A bare list of issue objects (a saved export), not content blocks.
        return [snap for snap in (snapshot_of(x) for x in response) if snap]
    obj = response if isinstance(response, dict) else _parse_content_block_object(response)
    if obj is None and isinstance(response, str):
        try:
            obj = json.loads(response)
        except Exception:
            obj = None
    if isinstance(obj, list):
        candidates = obj
    else:
        obj = _unwrap(as_dict(obj))
        candidates = obj.get("issues") if isinstance(obj.get("issues"), list) else [obj]
    out = []
    for issue in candidates:
        snap = snapshot_of(issue)
        if snap:
            out.append(snap)
    return out


def bypass_authors(prior, labels, who):
    """{bypass label: author} for an issue whose labels are now `labels`.

    Per label, because the two bypasses are separate grants: a lead-applied
    skip-format-check says nothing about who added skip-verify.

      - a label the issue already carried keeps whatever author is on record,
        INCLUDING NONE -- editJiraIssue replaces the whole array, so every edit
        re-sends every label, and re-sending is not applying. (Keeping only a
        known author let the lead adopt an unattributed skip-verify just by
        releasing a claim or adding a group label.) To apply a bypass to an
        issue that carries an unattributed one, the lead removes the label and
        adds it again;
      - a label that is new in a WRITE through the MCP belongs to the caller;
      - a label that is new in a READ (`who` is None) has no author: it arrived
        by a route no hook saw -- the board, another client -- and cannot be
        shown to be the lead's.
    """
    prior = prior or {}
    had = set(prior.get("labels") or [])
    authors = {}
    for label in labels or []:
        if label not in BYPASS_LABELS:
            continue
        known = jira_mirror.bypass_author(prior, label)
        if label in had:
            authors[label] = known
        elif who is None:
            authors[label] = ""
        else:
            authors[label] = "lead" if who["is_lead"] else (who["name"] or "worker")
    return authors


def _labels_from_create(tool_input):
    # as_dict, not `or {}`: additional_fields commonly arrives as a JSON string,
    # on which .get() raises. The outer handler would then fail open and journal
    # NOTHING -- and since only a create event ever writes status 'To Do', that
    # issue would stay invisible to the idle work-check forever, even after
    # later edits restore its labels. Unreadable labels lose the labels, not the
    # create event.
    # Both carriers are read (top-level `labels` on the current MCP,
    # additional_fields.labels on the older one).
    labels, _ = create_labels(tool_input)
    return labels or None


def _created_key(response):
    """Extract the new issue key from a create response.

    The exact shape is not contractual: the harness may hand back the raw MCP
    result, a wrapper around it, or -- confirmed via live capture from a real
    createJiraIssue call -- a list of content blocks whose `text` is the
    issue JSON serialized as a string (see _parse_content_block_object).
    Guessing wrong is silent -- _succeeded still says True, the key is None,
    nothing is journalled, and the idle check nudges nobody while the hook
    looks correctly installed. So accept the shapes we know about and make
    the miss loud in the audit log.

    Bare-dict handling comes first and is untouched: if `response` is
    already a non-empty dict, the content-block path is never consulted.

    The `id` arm is a last resort and only accepted when it is key-shaped
    (contains a '-', e.g. "AGENT-14"). A bare numeric id (the Jira REST
    internal id, e.g. "10042") is NOT an issue key: every later event for the
    issue is addressed by tool_input.issueIdOrKey using the real key, so
    journalling under the numeric id would fork off a second mirror entry
    that stays 'To Do' forever and is unfetchable by any agent -- a phantom
    claimable issue that survives the idle-check's loop guard because it
    never changes state. Reject it the same way as no key at all. This guard
    applies identically whether the id arrived in a bare dict or was decoded
    out of a content block.
    """
    r = as_dict(response)
    if not r:
        block_obj = _parse_content_block_object(response)
        if block_obj is not None:
            r = block_obj
    r = _unwrap(r)
    for candidate in (r.get("key"), as_dict(r.get("issue")).get("key")):
        if candidate:
            return str(candidate)
    raw_id = r.get("id")
    if raw_id and "-" in str(raw_id):
        return str(raw_id)
    return None


def main():
    p = read_payload()
    # Any Atlassian server name, and either name of the comment tool, folds to
    # one canonical operation; from here on `tool` is the legacy constant for it.
    op = jira_op(p.get("tool_name", ""))
    tool = _OPS.get(op)
    if tool is None and op != "executeRead":
        allow()

    # Decoded, not `or {}`: a tool_input that arrives as a JSON string raised on
    # the first .get() and the hook failed open having journalled nothing.
    tool_input, unreadable = tool_input_of(p)
    if unreadable:
        allow(EVENT, p, reason="not journalled: {}".format(unreadable))
    if op == "executeRead":
        # The generic read runner can carry the same reads as the primary tools
        # (`executeRead({name: "getJiraIssue", ...})`). Left out, an issue read
        # that way never reached the mirror, and the gate then called it unknown.
        tool = _OPS.get(str(tool_input.get("name") or ""))
        if tool != SYNC:
            allow()
    if not _succeeded(p.get("tool_response")):
        allow(EVENT, p, reason="tool call did not succeed -- not journalled")

    cfg = jira_mirror.load_config()
    project = cfg.get("projectKey")
    if not project:
        allow(EVENT, p, reason="no projectKey configured -- nothing to journal")

    event = {"op": None, "key": None}

    if tool == SYNC:
        # A read is Jira telling us the truth, including changes made by hand on
        # the board or before this mirror existed. Nothing is inferred: only the
        # fields the response actually carries are recorded.
        snaps = [s for s in snapshots_in(p.get("tool_response"))
                 if jira_mirror.issue_project(s["key"]) == project]
        for snap in snaps:
            if isinstance(snap.get("labels"), list):
                # A read shows labels but not who set them: a bypass label first
                # seen here is recorded as unattributed, never inherited.
                snap["bypass_by"] = bypass_authors(
                    jira_mirror.load_state(project).get(snap["key"]), snap["labels"], None)
            jira_mirror.append_event(project, dict(snap, op="sync"))
        allow(EVENT, p, reason="synced {} issue(s) from a read".format(len(snaps)))

    if tool == CREATE:
        if (tool_input.get("projectKey") or "") != project:
            allow(EVENT, p, reason="create targets another project -- ignored")
        key = _created_key(p.get("tool_response"))
        if not key:
            # Distinguishable on purpose: this is the diagnosable signature of a
            # journaller that is installed but blind, and it is only ever
            # visible in ~/.claude/logs/team-hooks.jsonl.
            allow(EVENT, p, reason=(
                "create succeeded but no issue key could be extracted -- "
                "unrecognized create response shape (keys: {})".format(
                    ",".join(sorted(as_dict(p.get("tool_response")))) or "none")))
        # createJiraIssue exposes a top-level `transition` (verified against the
        # live schema), so an issue can be created straight into In Progress.
        # Hardcoding 'To Do' mirrored such an issue as unclaimed work and -- since
        # a create carries no agent-* label -- advertised it to every teammate of
        # that role. An absent or unrecognized id still falls back to To Do.
        created_id = transition_id(tool_input)
        created_status = (cfg.get("transitions") or {}).get(created_id) or "To Do"
        event.update({
            "op": "create",
            "key": key,
            "summary": tool_input.get("summary"),
            "labels": _labels_from_create(tool_input),
            "status": created_status,
            "files": _files_from_description(tool_input.get("description")),
            "run": parse_run(tool_input.get("description")),
        })
    else:
        key = jira_mirror.normalize_issue_key(tool_input.get("issueIdOrKey"))
        if jira_mirror.issue_project(key) != project:
            allow(EVENT, p, reason="issue belongs to another project -- ignored")
        if tool == TRANSITION:
            # Resolved exactly as the gate resolves it, so the mirror and the
            # gate cannot disagree about where a transition lands. An unknown
            # transition journals no status rather than guessing.
            status = jira_transition_target(cfg, tool_input)
            event.update({"op": "transition", "key": key, "status": status})
        elif tool == EDIT:
            fields = as_dict(tool_input.get("fields"))
            event.update({
                "op": "edit",
                "key": key,
                "labels": edit_labels(tool_input),
                "summary": fields.get("summary"),
            })
            # An edit that rewrites the description changes the declared paths
            # and the verification command. Leaving the mirror on the old ones
            # kept the claim gate guarding files the issue no longer owns.
            description = fields.get("description")
            if isinstance(description, str):
                event["files"] = parse_files(description) or []
                event["run"] = parse_run(description) or ""
            # The edit's response echoes the issue as Jira now holds it; where it
            # does, that is the better source than what was asked for.
            for snap in snapshots_in(p.get("tool_response")):
                if snap["key"] == key:
                    for field in ("summary", "status", "labels", "files", "run"):
                        if field in snap and (field != "labels" or event.get("labels") is None):
                            if field in ("files", "run") and "files" in event:
                                continue
                            event[field] = snap[field]
        else:
            event.update({"op": "comment", "key": key})

    # Who set each bypass label. The gates honour one only if the lead applied
    # it, and Jira records no author a hook can read, so it is captured here.
    if isinstance(event.get("labels"), list):
        event["bypass_by"] = bypass_authors(
            jira_mirror.load_state(project).get(event.get("key")),
            event["labels"], caller_identity(p))

    jira_mirror.append_event(project, {k: v for k, v in event.items() if v is not None})
    allow(EVENT, p, reason="journalled {} for {}".format(event["op"], event["key"]))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL OPEN
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
