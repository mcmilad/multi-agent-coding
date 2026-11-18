#!/usr/bin/env python3
"""PreToolUse hook on Write/Edit -- enforce "claim before you edit".

This is the enforcement layer the claim protocol previously lacked. Every other
guardrail in this repo is a hook; the claim protocol was the one load-bearing
rule backed only by prose, and the measured result was that 8 of 11 worked
issues never entered In Progress at all. Documentation alone did not move that
number, so the invariant is now checked mechanically.

WHAT IT CAN AND CANNOT KNOW. A Write payload carries no teammate identity --
team_name/teammate_name reach only the TeammateIdle hook -- so this hook cannot
ask "are YOU the owner?". It asks a question it can actually answer, using the
`Files:` lists the mirror journals at create time:

    the file being written is declared by issue X; is X claimed at all?

That is attributable without identity, and it catches the exact failure: an
agent editing the files of an issue that is still sitting unclaimed at To Do,
or one that reached In Progress without ever taking the lock that decides
ownership.

Two violations, both blocked:
  1. The declaring issue is still `To Do` -- nobody has claimed this work.
  2. The declaring issue is `In Progress` but no claim lock exists -- it was
     claimed label-only, which provides no mutual exclusion (editJiraIssue has
     no compare-and-swap and transitions are global, so neither the label nor
     the status can arbitrate a race).

STOP ONCE PER ISSUE, NOT ONCE PER SESSION. A guardrail that traps a session is
worse than none, so every stop is one-shot -- but the budget is keyed on the
ISSUE, because `session_id` is shared by an entire team: 13 teammates in a real
run all reported the same one. Keyed on the session, "block once" would mean one
stop for the whole fleet, which is indistinguishable from no gate at all. Keyed
on the issue, each unclaimed issue earns exactly one actionable stop, and
MAX_BLOCKS bounds the total so a pathological mirror can still never trap a run.
The mirror is best-effort (it only sees MCP mutations, so a card moved by hand is
invisible), which is why every stop is survivable by simply retrying.

Never counts: anything under .claude/, the spec directory, dependency dirs, or
files no issue declares. Editing the rules and hooks themselves IS the meta-work
and must never be gated by them.

FAIL OPEN. Any unexpected condition allows. Bypass with CLAUDE_CLAIM_GATE=off.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import (  # noqa: E402
    read_payload, allow, as_dict, audit, log_dir, safe_path_component,
    claims_dir, git_common_dir, caller_identity, case_insensitive_fs, path_is_under,
    respell_under, real_path, read_small, write_private,
)
import time  # noqa: E402
import jira_mirror  # noqa: E402

EVENT = "PreToolUse"
WATCHED = ("Write", "Edit", "NotebookEdit")

# Total stops allowed per session, across all issues. A backstop against a
# pathological mirror, set far above any realistic run.
MAX_BLOCKS = 20

# Same exclusions as spec_gate, and for the same reason: repairing the
# guardrail must never be trapped by the guardrail.
_EXCLUDED_SEGMENTS = (
    "/.claude/", "/node_modules/", "/.venv/", "/venv/", "/.aws-sam/",
    "/__pycache__/", "/.superpowers/", "/.git/", "/dist/", "/build/",
    "/.pytest_cache/",
)


def is_excluded(path, root):
    """Match the exclusions against the REPO-RELATIVE path, never the absolute one.

    `seg in path` on the absolute path let the checkout LOCATION disable the
    guardrail: a repo cloned under any directory named build/dist/venv/... made
    every file in it look like build output, so every write was allowed -- and
    since that allow() carries no event, not even an audit record was left.
    Callers check `path` sits under `root` first, so slicing the root off leaves
    the leading '/' the segment patterns anchor on.
    """
    rel = path[len(root.rstrip("/")):]
    return any(seg in rel for seg in _EXCLUDED_SEGMENTS)


def state_path(session_id):
    return os.path.join(
        log_dir(), "claim-gate", safe_path_component(session_id, "nosession") + ".json"
    )


def load_state(session_id):
    try:
        with open(state_path(session_id)) as fh:
            state = json.load(fh)
        if isinstance(state, dict):
            state.setdefault("blocked_issues", [])
            return state
    except Exception:
        pass
    return {"blocked_issues": []}


def save_state(session_id, state):
    """True only if the state actually reached disk.

    The caller must not block on a state it failed to persist: load_state would
    then keep returning an empty set and the gate would re-arm on every single
    write, trapping the session while its own message promises one stop.
    """
    try:
        return write_private(state_path(session_id), json.dumps(state))
    except Exception:
        return False


def project_dir(payload):
    return (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or payload.get("cwd")
        or os.getcwd()
    )


def claim_dir(project, issue_key):
    return os.path.join(
        claims_dir(),
        safe_path_component(project, default="_noproject"),
        safe_path_component(issue_key, default="_noissue"),
    )


def declaring_issues(project, rel_path):
    """Issues whose `Files:` list names this path, as (key, issue) pairs.

    Uses .get() throughout: the mirror journal is never versioned or migrated,
    so events written before `files` existed must yield the default rather than
    raising into the fail-open handler and silently disabling this hook.

    Both sides go through jira_mirror.normalize_path, and for the same reason:
    the journal is never migrated, so it still holds paths recorded before the
    journaller normalised them. './src/a.py' must gate src/a.py -- comparing raw
    strings took exactly that file out of this hook, silently.
    """
    found = []
    try:
        state = jira_mirror.load_state(project)
    except Exception:
        return found
    for key, issue in sorted(state.items()):
        # By coverage: an entry may be an exact path, a directory or a glob.
        if any(jira_mirror.path_declared_by(f, rel_path) for f in (issue.get("files") or [])):
            found.append((key, issue))
    return found


STALE_CLAIM_SECONDS_DEFAULT = 60 * 60


def lock_owner(project, issue_key):
    """Instance name recorded in the claim lock, or None.

    The lock directory is the worker's to write, and this hook runs outside
    the sandbox and prints the owner in its block message. So the file is read
    only if it is a small regular file (a symlink here read another file back
    to the worker; a FIFO hung the hook), and only something shaped like an
    instance name is ever returned.
    """
    import re
    text = read_small(os.path.join(claim_dir(project, issue_key), "owner"), 256)
    name = (text or "").strip()
    return name if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) else None


def lock_age_seconds(project, issue_key):
    """Seconds since the lock's heartbeat was last refreshed, or None if unknown."""
    try:
        return time.time() - os.lstat(
            os.path.join(claim_dir(project, issue_key), "heartbeat")).st_mtime
    except Exception:
        return None


def refresh_heartbeat(project, issue_key):
    """The owner writing one of its files is the best liveness signal there is.

    Written by rename, inside the claims directory only: a symlink left at this
    path used to be followed, and the timestamp overwrote the file it pointed
    at -- a hook (review cycle 5).
    """
    path = os.path.join(claim_dir(project, issue_key), "heartbeat")
    write_private(path, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n",
                  within=claims_dir())


def enclosing_checkout(path):
    """(root, common_git_dir) of the checkout or worktree containing `path`.

    Walks up from the file to the nearest directory holding `.git`. For a linked
    worktree that is the worktree's own root, wherever it lives -- beside the
    repository or under `.claude/worktrees/` -- and its common git dir is the
    main repository's. Comparing common dirs is what tells "a worktree of this
    repository" from "some other repository".
    """
    d = os.path.dirname(path)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d, git_common_dir(d)
        parent = os.path.dirname(d)
        if parent == d:
            return None, None
        d = parent


def real(path):
    """The WHOLE path resolved, final component included.

    Resolving only the directory part left a symlink in the last position
    unfollowed, so a link named anything at all could stand in for a file an
    issue owns. realpath tolerates a file that does not exist yet.
    """
    try:
        return real_path(path)
    except Exception:
        return path


def _same(a, b):
    """Path equality as this filesystem sees it (case folded where it folds)."""
    if a is None or b is None:
        return False
    return (a.casefold() == b.casefold()) if case_insensitive_fs() else a == b


def violation(project, issues, who=None, stale_after=STALE_CLAIM_SECONDS_DEFAULT):
    """(issue_key, reason, certain) for the first declarer the caller may not write, or None.

    THE LOCK IS AUTHORITATIVE, THE STATUS IS ITS MIRROR. Every branch consults
    the claim dir first: an agent that holds the lock is the owner even when the
    mirror still shows To Do (the mirror only sees MCP mutations and lags).

    The lock records WHO holds it, and a hook payload says who is calling (see
    team_hook_common.caller_identity), so "is anyone working this?" became "is
    it you?":
      - caller is a named worker and owns the lock      -> allowed, heartbeat refreshed
      - caller is a named worker, someone else owns it  -> blocked, and this is
        CERTAIN: it is read off the lock and the payload, not inferred from the
        mirror, so it is not subject to the stop-once rule
      - caller has no agent identity (the lead, or a solo session): ownership
        cannot be checked, so a live lock is accepted and the audit log says so;
        a lock whose heartbeat has gone stale is not accepted

    `continue`, never `return None`, on a satisfied declarer: a landed sibling
    must not silently exempt a DIFFERENT unclaimed issue that declares the same
    path.
    """
    who = who or {"is_lead": True, "name": None}
    problems = []
    for key, issue in issues:
        status = str(issue.get("status") or "")
        if os.path.isdir(claim_dir(project, key)):
            owner = lock_owner(project, key)
            if not who["is_lead"] and who.get("name") and owner:
                if owner == who["name"]:
                    refresh_heartbeat(project, key)
                    continue
                problems.append((key, (
                    "{} is claimed by '{}', and you are '{}'. Its files are not yours to "
                    "edit: pick another issue. If '{}' has gone, the lead releases the "
                    "claim -- a worker never takes one over."
                ).format(key, owner, who["name"], owner), True))
                continue
            age = lock_age_seconds(project, key)
            if age is not None and age > stale_after:
                problems.append((key, (
                    "{} has a claim lock held by '{}', but its heartbeat is {} minutes "
                    "old and this session has no agent identity to show the lock is its "
                    "own. If it is yours, refresh it; if its owner has gone, the lead "
                    "must release it first:\n  {}"
                ).format(key, owner or "unknown", int(age // 60),
                         os.path.join(claim_dir(project, key), "heartbeat")), False))
                continue
            continue  # lock held and live -- owned, whatever the board currently says
        if status in ("In Review", "Done"):
            continue  # work already landed; edits here are follow-ups
        if status == "In Progress":
            problems.append((key, (
                "{} is In Progress but holds no claim lock at\n  {}\n"
                "It was claimed label-only, which provides NO mutual exclusion: "
                "editJiraIssue has no compare-and-swap (a peer's write erases your "
                "label) and transitions are global (both racers' move to In Progress "
                "succeeds). Take the lock."
            ).format(key, claim_dir(project, key)), False))
        else:
            problems.append((key, (
                "{} declares this file and is still '{}' -- no lock is held for it. "
                "Claim it before editing, so the lead's board does not report "
                "in-flight work as unstarted."
            ).format(key, status or "unknown"), False))
    # A certain violation outranks a mirror-derived one.
    problems.sort(key=lambda item: not item[2])
    return problems[0] if problems else None


def main():
    if os.environ.get("CLAUDE_CLAIM_GATE", "").lower() in ("off", "0", "false"):
        allow()

    p = read_payload()
    if p.get("tool_name") not in WATCHED:
        allow()

    # NotebookEdit names its target notebook_path, not file_path. Matching only
    # file_path made the hook inert for notebooks while settings.json advertised
    # them as gated -- a guardrail that silently covers less than it claims.
    tool_input = as_dict(p.get("tool_input"))
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not isinstance(path, str) or not path:
        allow()

    session_id = p.get("session_id") or "nosession"
    state = load_state(session_id)
    if len(state.get("blocked_issues") or []) >= MAX_BLOCKS:
        allow(EVENT, p, reason="block budget spent this session -- staying out of the way")

    # The file is judged by the checkout it sits in, not by the directory the
    # session started in. A teammate working in a linked worktree -- including
    # one under .claude/worktrees/, which the exclusion list used to wave
    # through wholesale -- is writing this repository's files, at the same
    # repo-relative paths the issues declare.
    project_root = real_path(project_dir(p))
    # A second spelling of the project root (a macOS firmlink) is the same root.
    path = real(respell_under(real(path), project_root))
    root, common = enclosing_checkout(path)
    if root is None or common is None or not _same(common, git_common_dir(project_root)):
        if not path_is_under(path, project_root):
            allow()                      # not this repository at all
        root = project_root
    if is_excluded(path, root):
        allow()

    cfg = jira_mirror.load_config()
    project = cfg.get("projectKey")
    if not project:
        allow(EVENT, p, reason="no projectKey configured -- fail-open")

    # Sliced, not os.path.relpath: on a case-insensitive disk the root can be
    # spelled differently in the path (`/x/PROJECT/src/a.py` under `/x/project`),
    # and relpath then answers `../PROJECT/src/a.py`, which no issue declares.
    stem = root.rstrip(os.sep)
    if path_is_under(path, root) and len(path) > len(stem):
        rel = path[len(stem):].lstrip(os.sep)
    else:
        rel = os.path.relpath(path, root)
    issues = declaring_issues(project, rel)
    if not issues:
        allow()  # no issue declares this file -- not board work

    who = caller_identity(p)
    stale_after = cfg.get("staleClaimMinutes")
    stale_after = (stale_after * 60 if isinstance(stale_after, (int, float))
                   and not isinstance(stale_after, bool) and stale_after > 0
                   else STALE_CLAIM_SECONDS_DEFAULT)
    found = violation(project, issues, who=who, stale_after=stale_after)
    if not found:
        allow(EVENT, p, reason="{} is covered by a claimed issue ({})".format(
            rel, "owner verified" if not who["is_lead"] and who["name"]
            else "caller has no agent identity -- ownership not checked"))

    issue_key, reason, certain = found
    if certain:
        # Read off the lock and the payload, not inferred from a lagging mirror:
        # stopping once and then stepping aside would hand a second agent the
        # files on its retry.
        message = "[claim-gate] Blocked writing {}\n\n{}".format(rel, reason)
        audit(EVENT, {"file_path": path, "issue": issue_key}, "block", message)
        print(message, file=sys.stderr)
        sys.exit(2)
    if issue_key in (state.get("blocked_issues") or []):
        allow(EVENT, p, reason="already stopped once for {} -- not repeating".format(
            issue_key))

    state.setdefault("blocked_issues", []).append(issue_key)
    if not save_state(session_id, state):
        # Could not record the stop, so a block here would repeat on every write.
        allow(EVENT, p, reason="claim-gate state not persistable -- fail-open")

    message = (
        "[claim-gate] Blocked writing {}\n\n{}\n\n"
        "Claim it -- the mkdir lock is what decides ownership, and it must SUCCEED:\n"
        "  CLAIMS=\"$(git rev-parse --path-format=absolute --git-common-dir)/claude-harness/claims/{p}\"\n"
        "  mkdir -p \"$CLAIMS\"\n"
        "  if mkdir \"$CLAIMS/{k}\" 2>/dev/null; then\n"
        "    echo \"<your-instance>\" > \"$CLAIMS/{k}/owner\"\n"
        "    date -u +%Y-%m-%dT%H:%M:%SZ > \"$CLAIMS/{k}/heartbeat\"\n"
        "  else\n"
        "    echo \"LOST -- owned by $(cat \"$CLAIMS/{k}/owner\" 2>/dev/null)\"; fi\n\n"
        "Never write owner/heartbeat unconditionally: on a lost race that overwrites "
        "the winner's own record. If you LOST, pick another issue and touch nothing.\n"
        "Once the lock is yours: add your agent-* label, transition {k} to In Progress, "
        "and comment 'Claimed by <instance>.' -- full protocol in `jira-workflow`.\n\n"
        "This gate stops you at most ONCE for {k} and will not repeat for it. If the "
        "mirror is stale or this file genuinely is not {k}'s work, just retry. To "
        "disable the gate for this session entirely: CLAUDE_CLAIM_GATE=off."
    ).format(rel, reason, p=project, k=issue_key)
    audit(EVENT, {"file_path": path, "issue": issue_key}, "block", message)
    print(message, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL OPEN
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
