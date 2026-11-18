"""Shared helpers for the agent-team enforcement hooks.

Imported by jira_issue_format_check.py, jira_transition_verify_gate.py,
jira_mirror_journal.py and teammate_idle_workcheck.py. Responsibilities:
  - parse the stdin JSON payload the harness delivers to a hook,
  - append an audit record to ~/.claude/logs/team-hooks.jsonl for every decision,
  - implement the documented exit-code contract: 0 = proceed, 2 = block + the
    stderr text is fed back as the reason.

Design rule for ALL hooks: FAIL OPEN. Any unexpected condition must resolve to
allow() -- a hook bug must never block a valid action or trap a teammate.
Enforcement is a guardrail, not a tripwire.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone


def _home():
    return os.path.expanduser("~")


_DATA_VOLUME = "/System/Volumes/Data"


def unfirm(path):
    """`path` without the macOS data-volume prefix, when that is the same place.

    `/System/Volumes/Data/Users/x` and `/Users/x` are one directory (a firmlink),
    and realpath leaves both spellings alone. Every path guard compares
    spellings, so there has to be exactly one: the short one. The long spelling
    is given up only when the filesystem confirms the two are the same file,
    judged at the deepest ancestor that exists.
    """
    p = str(path)
    if not p.startswith(_DATA_VOLUME + os.sep):
        return path
    short = p[len(_DATA_VOLUME):]
    a, b = p, short
    while True:
        try:
            return short if os.path.samefile(a, b) else path
        except (OSError, ValueError):
            pass
        na, nb = os.path.dirname(a), os.path.dirname(b)
        if nb == b or len(na) <= len(_DATA_VOLUME):
            return path
        a, b = na, nb


def real_path(path):
    """realpath, in the one spelling the guards compare (see unfirm)."""
    return unfirm(os.path.realpath(str(path)))


def write_private(path, text, within=None):
    """Replace the file at `path` with `text`. True if it was written.

    For state a hook writes where a worker's shell can also write. A hook runs
    outside the sandbox, so a plain `open(path, "w")` followed a symlink the
    worker left at `path` and overwrote whatever it pointed at -- a hook file
    (review cycle 5). Written to a fresh temporary file and renamed into place:
    a rename replaces the directory entry, link or not, and never the target.
    With `within`, the directory actually written must lie inside it, so a
    symlinked parent cannot redirect the write either.
    """
    try:
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        if within is not None and not path_is_under(folder, within):
            return False
        tmp = "{}.tmp.{}".format(path, os.getpid())
        try:
            os.unlink(tmp)
        except OSError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600)
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def append_private(path, text):
    """Append `text` to the regular file at `path`. True if it was written.

    Never through a symlink, never into a file with a second name (a hard link
    to something else), never into a pipe or device.
    """
    import stat
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
    except Exception:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            return False
        os.write(fd, text.encode("utf-8"))
        return True
    except Exception:
        return False
    finally:
        os.close(fd)


def read_small(path, max_bytes=4096):
    """Text of a small regular file, or None. Never blocks, never follows a
    final symlink: what a worker left at a path a hook reads is not to be
    opened on trust, and never echoed if it is a link to something else."""
    import stat
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
    except Exception:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            return None
        return os.read(fd, max_bytes).decode("utf-8", "replace")
    except Exception:
        return None
    finally:
        os.close(fd)


def log_dir():
    return os.path.join(_home(), ".claude", "logs")


def log_path():
    return os.path.join(log_dir(), "team-hooks.jsonl")


def _project_start_dir():
    """Where to look for the repository this hook is serving.

    $CLAUDE_PROJECT_DIR is the project root the session started in; it is set
    for hooks and is the same for every worktree-less teammate. Falling back to
    the process cwd covers scripts run by hand and tests.
    """
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def git_common_dir(start=None):
    """Absolute path of the repository's COMMON git directory, or None.

    Resolved by reading the filesystem, not by spawning git: hooks run on every
    tool call and must stay cheap. For an ordinary checkout this is `<root>/.git`.
    For a linked worktree `<root>/.git` is a FILE (`gitdir: <main>/.git/worktrees/x`)
    and that directory's `commondir` file points back at the main `.git` -- so
    every worktree of one repository resolves to the same directory, which is
    exactly the property shared coordination state needs.
    """
    try:
        d = real_path(start or _project_start_dir())
        while True:
            dot_git = os.path.join(d, ".git")
            if os.path.isdir(dot_git):
                return real_path(dot_git)
            if os.path.isfile(dot_git):
                first = (read_small(real_path(dot_git)) or "").split("\n", 1)[0].strip()
                if not first.startswith("gitdir:"):
                    return None
                gitdir = first[len("gitdir:"):].strip()
                if not os.path.isabs(gitdir):
                    gitdir = os.path.join(d, gitdir)
                gitdir = real_path(gitdir)
                commondir_file = os.path.join(gitdir, "commondir")
                if os.path.isfile(commondir_file):
                    common = (read_small(real_path(commondir_file)) or "").split("\n", 1)[0].strip()
                    if not common:
                        return None
                    if not os.path.isabs(common):
                        common = os.path.join(gitdir, common)
                    return real_path(common)
                return gitdir
            parent = os.path.dirname(d)
            if parent == d:
                return None
            d = parent
    except Exception:
        return None


STATE_DIR_ENV = "CLAUDE_HARNESS_STATE_DIR"
STATE_SUBDIR = "claude-harness"


def state_root():
    """Root of the coordination state: mirror, claim locks, evidence, run locks.

    This state used to live under ~/.claude/logs keyed only by the Jira project
    key, so two checkouts using the same key (a second clone, a sibling copy)
    silently shared claim locks, verification sentinels and the mirror, and a
    stale sentinel from one satisfied the gate in the other. It now lives in the
    repository's common git directory: per repository by construction, shared by
    every worktree of that repository, never committed, and inside the project
    tree so sandboxed shells can write it.

    Resolution order:
      1. $CLAUDE_HARNESS_STATE_DIR (`~` is expanded) -- explicit override, used
         by the test suite and by installs that want the state elsewhere.
      2. <git common dir>/claude-harness.
      3. ~/.claude/logs -- only when no repository can be found at all.
    """
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return os.path.expanduser(override)
    common = git_common_dir()
    if common:
        return os.path.join(common, STATE_SUBDIR)
    return log_dir()


def verified_dir():
    """Completion sentinels: <state root>/verified/<scope>/"""
    return os.path.join(state_root(), "verified")


def claims_dir():
    """Claim locks: <state root>/claims/<projectKey>/<ISSUE-KEY>/"""
    return os.path.join(state_root(), "claims")


def locks_dir():
    """Run-level locks (commit mutex, finalize): <state root>/locks/<projectKey>/"""
    return os.path.join(state_root(), "locks")


def nudge_dir():
    """Idle loop-guard state: <state root>/idle-nudges/"""
    return os.path.join(state_root(), "idle-nudges")


_MCP_TOOL = re.compile(r"^mcp__(.+?)__([A-Za-z0-9_]+)$")

# Operation names that changed between Atlassian MCP versions, old -> canonical.
_JIRA_OP_ALIASES = {"addCommentToJiraIssue": "addOrEditJiraIssueComment"}


def jira_op(tool_name):
    """Canonical Jira operation for an Atlassian MCP tool name, else None.

    Hooks used to compare against one hardcoded server prefix
    (`mcp__plugin_atlassian_atlassian__`). The same Atlassian server is also
    reachable under other names (a second plugin exposes
    `mcp__plugin_engineering_atlassian__*`; a user-added server is
    `mcp__atlassian__*`), and through any of those every gate silently did not
    apply. Any server whose name contains "atlassian" is recognised, and renamed
    operations are folded to one canonical name.
    """
    m = _MCP_TOOL.match(str(tool_name or ""))
    if not m or "atlassian" not in m.group(1).lower():
        return None
    return _JIRA_OP_ALIASES.get(m.group(2), m.group(2))


def read_payload():
    """Read and parse the hook stdin payload. Returns {} on empty/invalid."""
    raw = sys.stdin.read()
    try:
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


_UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]")


def safe_path_component(value, default="_"):
    """Sanitize a single untrusted path component (team name, task id) so it
    cannot escape its intended parent directory.

    Payload fields like team_name / task_id are attacker-influenceable (e.g. an
    agent steered by prompt injection could create a team/task with crafted
    identifiers). They get joined into filesystem paths the hooks then write to
    or os.remove(), so a value like '../../x' or '/etc/x' must not be allowed to
    traverse out. Strategy: drop any directory portion with basename(), then
    allowlist to [A-Za-z0-9._-]. Never returns '', '.', or '..', and the result
    never contains a path separator. No-op for legitimate slugs / numeric ids.
    """
    s = "" if value is None else str(value)
    s = os.path.basename(s)              # '../../../tmp' -> 'tmp', '/etc/passwd' -> 'passwd'
    s = _UNSAFE_COMPONENT.sub("_", s)    # replace any surviving separators / odd chars
    if s in ("", ".", ".."):             # pure-dot results would still resolve to a dir
        return default
    return s


def as_dict(value):
    """Coalesce a value to a dict before .get(), guarding on *type* rather than
    truthiness.

    `value or {}` only guards falsy values (None, {}, [], ""). A truthy
    non-dict passes through unchanged and raises AttributeError on the next
    .get() -- which the outer fail-open handler then swallows, so the hook
    exits 0 and the guardrail silently does nothing. The concrete case: a model
    emitting `additional_fields` as a JSON *string* instead of an object, a
    well-documented failure mode. Every value a hook reads out of a tool
    payload is model-authored, so its type is never guaranteed.

    (scripts/jira_bootstrap.py carries its own copy as `_as_dict`. It must stay
    standard-library-only and must not import from the hooks directory, so a
    five-line duplicate beats a cross-tree import.)
    """
    return value if isinstance(value, dict) else {}


def tool_input_of(payload):
    """(tool_input_dict, unreadable_cause_or_None) for a hook payload.

    A tool_input arriving as a JSON *string* is DECODED here, unlike as_dict()
    which deliberately discards one. The two directions are opposite on purpose:

      - a nested field (additional_fields) carries bypass authority, so
        refusing to decode it is the safe direction -- an unreadable
        skip-format-check label must not be a bypass;
      - tool_input itself carries the SCOPE. Every gating hook decides whether
        an action is its business by reading projectKey / issueIdOrKey out of
        it. Discarding it makes the scope unknown, and an unknown scope reads
        to those hooks as "some other project" -- so they allow, and the audit
        log records a reason that is simply false. Decoding can only ever turn
        an allow into a block, never the reverse.

    Anything still not a dict is genuinely unreadable and the caller must fail
    open on it -- the scope cannot be established, and these hooks must never
    police a project that is not the configured one -- but must say why. An
    absent tool_input is not unreadable: no arguments legitimately reads as {}.
    """
    raw = payload.get("tool_input") if isinstance(payload, dict) else None
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except Exception:
            decoded = None
        if isinstance(decoded, dict):
            return decoded, None
    if raw is None:
        return {}, None
    return {}, "tool_input arrived as a {}, not a JSON object".format(
        type(raw).__name__)


def create_labels(tool_input):
    """(labels_list, unreadable) for a createJiraIssue input, across MCP versions.

    The Atlassian MCP has shipped two shapes: labels nested under
    `additional_fields` (older) and a top-level `labels` array (current; the
    current server REJECTS the nested form). Reading only one makes every label
    invisible on the other -- which blocked all issue creation and hid the
    bypass label. Both are read and unioned. `unreadable` is True when either
    carrier is present but not the expected type, so a caller can say so rather
    than report "no labels".
    """
    tool_input = as_dict(tool_input)
    out, unreadable = [], False
    top = tool_input.get("labels")
    if isinstance(top, list):
        out.extend(str(x) for x in top)
    elif top is not None:
        unreadable = True
    raw_af = tool_input.get("additional_fields")
    if raw_af is not None and not isinstance(raw_af, dict):
        unreadable = True
    nested = as_dict(raw_af).get("labels")
    if isinstance(nested, list):
        out.extend(str(x) for x in nested if str(x) not in out)
    elif nested is not None:
        unreadable = True
    return out, unreadable


def edit_labels(tool_input):
    """Labels an editJiraIssue call sets, or None if it sets none.

    `fields.labels` in both MCP versions; `additional_fields.labels` is accepted
    too since the current schema offers both carriers.
    """
    tool_input = as_dict(tool_input)
    for carrier in ("fields", "additional_fields"):
        labels = as_dict(tool_input.get(carrier)).get("labels")
        if isinstance(labels, list):
            return [str(x) for x in labels]
    return None


def issue_type_name(tool_input):
    """Issue type of a create call: `issueType` (current) or `issueTypeName` (older)."""
    tool_input = as_dict(tool_input)
    for k in ("issueType", "issueTypeName"):
        v = tool_input.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


_DONE_LIKE = re.compile(
    r"^(done|closed?|resolved?|complete[d]?|cancel+ed|won'?t ?(do|fix)|rejected|released|"
    r"shipped|finished)$", re.I)


def is_done_status(cfg, status):
    """Is `status` one that finishes an issue?

    Jira's category for the status decides when bootstrap recorded it
    (`statusCategories`). Without that, the name does: a status called Closed,
    Resolved or Complete finishes an issue as surely as one called Done, and
    treating only the literal name as final left every other one ungated.
    """
    if not status:
        return False
    category = as_dict(as_dict(cfg).get("statusCategories")).get(status)
    if category:
        return str(category).lower() == "done"
    return bool(_DONE_LIKE.match(str(status).strip()))


_CASE_INSENSITIVE = None


def case_insensitive_fs():
    """Does this machine's filesystem treat `Hooks` and `hooks` as one name?

    macOS and Windows do by default. Decided once, by asking the filesystem
    about this very file under a different spelling.
    """
    global _CASE_INSENSITIVE
    if _CASE_INSENSITIVE is None:
        here = os.path.abspath(__file__)
        other = here.swapcase()
        try:
            _CASE_INSENSITIVE = other != here and os.path.exists(other) and os.path.samefile(
                here, other)
        except OSError:
            _CASE_INSENSITIVE = False
    return _CASE_INSENSITIVE


def canonical_path(path, fold=None):
    """One spelling for a path, for comparing it with a path that is protected.

    Two things made string comparison of paths a hole in every path guard:

      - a SYMLINK as the final component was not followed (only the directory
        part was resolved), so a link to a protected file was not the file;
      - LETTER CASE: on a case-insensitive filesystem `.claude/HOOKS/x.py` is
        `.claude/hooks/x.py`, and a guard matching the lower-case spelling let
        the other through.

    `realpath` resolves the whole path, final component included, and tolerates
    a file that does not exist yet. Case is folded when the filesystem folds it
    (or always, with fold=True).
    """
    try:
        resolved = real_path(os.path.expanduser(str(path)))
    except Exception:
        resolved = str(path)
    if fold is None:
        fold = case_insensitive_fs()
    return resolved.casefold() if fold else resolved


def path_is_under(path, root, fold=None):
    """Is `path` the directory `root` or something inside it? Canonical on both sides."""
    p, r = canonical_path(path, fold), canonical_path(root, fold).rstrip(os.sep)
    return p == r or p.startswith(r + os.sep)


def respell_under(path, root):
    """`path` rewritten to sit under `root` when it does so by identity only.

    realpath gives one spelling per symlink, but not per FIRMLINK: on macOS
    `/System/Volumes/Data/Users/x` and `/Users/x` are the same directory and
    realpath leaves both alone, so a guard comparing strings saw a protected
    file under the long spelling as a stranger (review cycle 4). Identity is the
    inode: each existing ancestor of `path` is compared with `root`, and on a
    match the remainder is re-attached to `root`'s own spelling.

    Returns `path` unchanged when it is already under `root` as written, or is
    not under it at all.
    """
    try:
        resolved = real_path(os.path.expanduser(str(path)))
        root = real_path(os.path.expanduser(str(root)))
        if path_is_under(resolved, root, fold=True):
            # Resolved, not as given: the long spelling of a path already
            # under the root must not come back out of here.
            return resolved if unfirm(str(path)) != str(path) else path
        wanted = os.stat(root)
    except Exception:
        return path
    tail, current = [], resolved
    while True:
        try:
            st = os.stat(current)
            if (st.st_dev, st.st_ino) == (wanted.st_dev, wanted.st_ino):
                return os.path.join(root, *reversed(tail)) if tail else root
        except OSError:
            pass
        parent = os.path.dirname(current)
        if parent == current:
            return path
        tail.append(os.path.basename(current))
        current = parent


def repo_id():
    """Short stable id for this repository (all of its worktrees share it)."""
    import hashlib
    common = git_common_dir() or real_path(_project_start_dir())
    return hashlib.sha256(common.encode("utf-8", "replace")).hexdigest()[:12]


HUMAN_DIR_ENV = "CLAUDE_HARNESS_HUMAN_DIR"


def human_dir():
    """Where decisions only a human may record live: ~/.claude/harness/<repo-id>/.

    AWS approval markers and the sandbox-account declaration used to sit in the
    project tree, where the agent they constrain could create them with one
    `touch`. They are kept outside the repository, under ~/.claude, which a
    sandboxed shell cannot write and which the guardrail guard refuses to the
    file-editing tools. Overridable for tests.
    """
    override = os.environ.get(HUMAN_DIR_ENV)
    if override:
        return os.path.expanduser(override)
    return os.path.join(_home(), ".claude", "harness", repo_id())


def transition_id(tool_input):
    """Transition id of a call: `transitionId` (current) or `transition.id` (older).

    Total on junk: every field is model-authored.
    """
    tool_input = as_dict(tool_input)
    tid = tool_input.get("transitionId")
    if isinstance(tid, bool) or not isinstance(tid, (str, int)) or not str(tid).strip():
        tid = as_dict(tool_input.get("transition")).get("id", "")
    tid = str(tid if tid is not None else "").strip()
    # "031" and "31" are the same transition to Jira; compared as text the
    # padded form missed the discovered map and read as an unknown transition.
    return str(int(tid)) if tid.isdigit() else tid


def transition_name(tool_input):
    """`transitionName` of a call (current MCP only), or ''."""
    v = as_dict(tool_input).get("transitionName")
    return v.strip() if isinstance(v, str) else ""


def transition_target(cfg, tool_input):
    """Status a transitionJiraIssue call lands in, or None if it cannot be told.

    By id through the discovered `transitions` map; failing that, by
    `transitionName` when it equals a configured status name (the usual case on
    a team-managed board, where each global transition is named for its
    target). One resolver for the gate, the finalizer and the journaller, so
    they cannot disagree.
    """
    cfg = as_dict(cfg)
    tid = transition_id(tool_input)
    if tid:
        return as_dict(cfg.get("transitions")).get(tid) or None
    name = transition_name(tool_input).lower()
    if name:
        known = list(as_dict(cfg.get("statuses"))) + [
            str(v) for v in as_dict(cfg.get("transitions")).values()]
        for status in known:
            if str(status).lower() == name:
                return str(status)
    return None


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# What may be written to the audit log about a tool call. Everything else in a
# payload is model- or user-authored content: file bodies, shell commands with
# inline credentials, Jira responses. The log is a plain file in ~/.claude/logs
# that is never rotated, so it records WHAT was decided about WHICH call and by
# WHOM, plus a digest that lets two records be matched, and nothing it would be
# a problem to leave lying around.
_AUDIT_TOP = ("hook_event_name", "tool_name", "tool_use_id", "session_id", "agent_id",
              "agent_type", "teammate_name", "team_name", "cwd", "permission_mode",
              "file_path", "issue", "patterns", "prompt_len")
_AUDIT_INPUT = ("issueIdOrKey", "projectKey", "transitionId", "transitionName", "issueType",
                "file_path", "notebook_path", "name")
_AUDIT_MAX = 300


def _short(value):
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_short(v) for v in list(value)[:20]]
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= _AUDIT_MAX else text[:_AUDIT_MAX] + "...[truncated]"


def audit_view(payload):
    """The part of a hook payload that is safe and useful to keep."""
    import hashlib
    if not isinstance(payload, dict):
        return {}
    view = {k: _short(payload[k]) for k in _AUDIT_TOP if k in payload}
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except Exception:
            tool_input = {"_raw": tool_input}
    if isinstance(tool_input, dict):
        summary = {k: _short(tool_input[k]) for k in _AUDIT_INPUT if k in tool_input}
        summary["keys"] = sorted(str(k) for k in tool_input)
        try:
            summary["sha256"] = hashlib.sha256(
                json.dumps(tool_input, sort_keys=True, default=str).encode()).hexdigest()[:16]
        except Exception:
            pass
        view["tool_input"] = summary
    if "tool_response" in payload:
        response = payload["tool_response"]
        view["tool_response"] = {"type": type(response).__name__}
    return view


def audit(event, payload, decision, reason=None, extra=None):
    rec = {"captured_at": _now(), "event": event, "decision": decision}
    if reason:
        rec["reason"] = reason
    if extra:
        rec.update(extra)
    rec["payload"] = audit_view(payload)
    try:
        os.makedirs(log_dir(), mode=0o700, exist_ok=True)
        append_private(log_path(), json.dumps(rec) + "\n")
    except Exception:
        pass  # logging must never break a hook


def allow(event=None, payload=None, reason=None, extra=None):
    """Permit the action (exit 0). Audits if an event is given."""
    if event is not None:
        audit(event, payload, "allow", reason, extra)
    sys.exit(0)


def block(event, payload, reason, extra=None):
    """Block the action (exit 2). stderr is fed back as the reason."""
    audit(event, payload, "block", reason, extra)
    print(reason, file=sys.stderr)
    sys.exit(2)


def caller_identity(payload):
    """Who issued the tool call a hook is looking at.

    Returns {"is_lead": bool, "agent_id": str|None, "name": str|None, "role": str|None}.

    Captured from the live harness (see the spec's decisions.md, "Hook payload
    capture"): a call from the main session carries neither `agent_id` nor
    `agent_type`; a call from inside a named background subagent or teammate
    carries both, and `agent_type` is the SPAWN NAME the lead assigned (for
    example `review-1`), which the worker cannot change. So:

      - no `agent_id`           -> the lead (or a solo session);
      - `agent_id` present      -> a worker, whose role comes from its name.

    Nothing here reads text the model supplies in tool arguments. `teammate_name`
    is used when present (TeammateIdle payloads carry it), then `agent_type`.
    """
    payload = payload if isinstance(payload, dict) else {}
    agent_id = payload.get("agent_id")
    agent_id = agent_id if isinstance(agent_id, str) and agent_id.strip() else None
    name = None
    for key in ("teammate_name", "agent_type"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    return {
        "is_lead": agent_id is None,
        "agent_id": agent_id,
        "name": name if agent_id else None,
        "role": role_of_teammate(name) if agent_id else None,
    }


# Every role a worker can hold. The one list: the issue format check, the spawn
# guard and role_of_teammate all read it. Adding a role is this line, an agent
# file named <role>-agent.md, and a row in the lead's pool table.
ROLES = ("coding", "frontend", "backend", "devops", "sa", "review")


def role_of_teammate(name):
    """Map a teammate name (e.g. 'coding-agent', 'coding-2') to its role, or None.

    Only an exact match or a `<role>-` instance suffix counts. A bare prefix
    match would over-match: 'sample-1'.startswith('sa') is true, which would
    hand a teammate named 'sample-1' the sa pool's unclaimed work.
    """
    if not name:
        return None
    n = str(name).lower()
    for role in ROLES:
        if n == role or n.startswith(role + "-"):
            return role
    return None
