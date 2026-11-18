"""Config loading and the local Jira mirror journal.

Two responsibilities, both consumed by every Jira hook:

1. CONFIG. Custom field IDs, status IDs and transition IDs are per-site, so
   scripts/jira_bootstrap.py discovers them and writes .claude/jira-config.json.
   Nothing here may be hardcoded -- this repo is a public template and baked-in
   IDs from one site would fail silently on every other install.

2. MIRROR. A hook subprocess cannot reach Jira: the MCP's OAuth token lives
   inside Claude Code, not the environment. So the PostToolUse journaller
   records every successful mutation to ~/.claude/logs/jira-mirror/<KEY>.jsonl
   and the idle work-check folds that log into current per-issue state.

   Known blind spot: the mirror only sees mutations made through the MCP. Edits
   made by hand on the board are invisible until an agent next touches that
   issue. This is acceptable because the only consumer is an advisory,
   fail-open nudge.

Every function here is best-effort. Nothing raises; callers are hooks that must
fail open.
"""
import json
import os

# .claude/hooks/jira_mirror.py -> repo root
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_PATH = os.path.join(_REPO, ".claude", "jira-config.json")

# Fields a mirror event may carry. Absent keys must not clobber known values.
#
# ADDING A FIELD: it must be added to _DEFAULTS below in the same change. The
# journal is never versioned, migrated or rotated (fullstack-agent.md explicitly
# tells teardown to leave it in place), so live journals contain events written
# before the field existed. A field in _MERGEABLE but not in _DEFAULTS means
# every consumer doing issue["newfield"] raises KeyError on those old events --
# which the hooks' fail-open handler swallows, exiting 0 with the guardrail
# silently disabled and indistinguishable from "nothing to do".
_MERGEABLE = ("summary", "labels", "status", "files", "run", "blocked_by", "bypass_by")


def bypass_author(issue, label):
    """Who applied bypass `label` to this issue, or '' if nobody is on record.

    `bypass_by` maps each bypass label to its author. A value in any other
    shape (older journals stored one string for both labels) is treated as no
    record: sharing one author between two labels let a lead-applied
    skip-format-check vouch for a skip-verify nobody had attributed.
    """
    authors = (issue or {}).get("bypass_by")
    if not isinstance(authors, dict):
        return ""
    return str(authors.get(label) or "")

_DEFAULTS = {"summary": "", "labels": [], "status": "", "files": [],
             "run": "", "blocked_by": [], "bypass_by": {}}


def _fresh_defaults():
    """A defaults dict whose mutable values no other issue shares.

    dict(_DEFAULTS) is a SHALLOW copy: every issue in the folded state would
    hold the *same* labels list and the same files list, and one consumer doing
    issue["labels"].append() would rewrite every other issue AND poison the
    module-level default for the remaining life of the process -- handing every
    later-folded issue a phantom agent-* claim. The dict literal this replaced
    built fresh lists per call; keep that, without giving up _DEFAULTS as the
    single place a new field is declared (see the note above _MERGEABLE).
    """
    return {k: list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v
            for k, v in _DEFAULTS.items()}


def normalize_path(path):
    """Canonical form of a repo-relative path in the mirror's `files` list.

    Both sides of every comparison must agree or the path drops out of the two
    places declared paths are load-bearing -- jira_issue_format_check's overlap
    check and claim_gate -- silently, for that file only. claim_gate compares
    os.path.relpath() output, which is already normalised, so './src/a.py' or
    'src//a.py' in an issue's `Files:` matched nothing. Applied on READ as well
    as on write: the journal is never versioned, migrated or rotated, so live
    journals still hold entries recorded in their raw form.

    Best-effort like everything here: an unusable value normalises to '', which
    callers drop, rather than raising into a hook's fail-open handler.
    """
    try:
        s = str(path or "").strip()
        if not s:
            return ""
        return os.path.normpath(s)
    except Exception:
        return ""


_GLOB_CHARS = set("*?[")


def is_glob(entry):
    return bool(_GLOB_CHARS & set(str(entry)))


def _glob_regex(pattern):
    """Regex for a path glob: `**` crosses directories, `*` and `?` do not."""
    import re
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 2] == "**":
                i += 2
                if pattern[i:i + 1] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                out.append(pattern[i:j + 1])
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def path_declared_by(entry, rel_path):
    """Does a `Files:` entry cover this repo-relative path?

    An entry is an exact path, a directory (everything beneath it), or a glob.
    Entries used to be compared by string equality only, so `Files: src/` or
    `Files: tests/**` declared paths that matched nothing -- the claim gate and
    the overlap check both silently stopped applying to that issue.
    """
    entry = normalize_path(entry)
    rel_path = normalize_path(rel_path)
    if not entry or not rel_path:
        return False
    if _fold_case():
        # `SRC/login.py` is `src/login.py` on a case-insensitive disk; matched
        # as written, the other spelling was a file no issue owned.
        entry, rel_path = entry.casefold(), rel_path.casefold()
    if is_glob(entry):
        return bool(_glob_regex(entry).match(rel_path))
    return rel_path == entry or rel_path.startswith(entry.rstrip("/") + "/")


def _fold_case():
    try:
        import team_hook_common
        return team_hook_common.case_insensitive_fs()
    except Exception:
        return False


def _static_prefix(entry):
    """The leading part of a glob that contains no wildcard, as a directory."""
    parts = []
    for part in normalize_path(entry).split("/"):
        if is_glob(part):
            break
        parts.append(part)
    return "/".join(parts)


def entries_overlap(a, b):
    """Could two `Files:` entries cover a common path?

    Exact for path/path, path/directory and path/glob. For two globs it is
    conservative: they are treated as overlapping when one's wildcard-free
    prefix sits inside the other's, which may over-report but never misses a
    real collision -- the safe direction for a rule that prevents two agents
    writing the same file.
    """
    a, b = normalize_path(a), normalize_path(b)
    if not a or not b:
        return False
    if not is_glob(a) and not is_glob(b):
        return path_declared_by(a, b) or path_declared_by(b, a)
    if is_glob(a) and not is_glob(b):
        a, b = b, a
    if not is_glob(a):                      # a is a path or directory, b a glob
        if path_declared_by(b, a):
            return True
        if "." in os.path.basename(a):
            # Reads as a file, and the glob does not match it: no common path.
            # (An extension-less entry may be a directory, which the glob could
            # reach into, so that case stays conservative below.)
            return False
        prefix = _static_prefix(b)
        return (not prefix) or path_declared_by(a, prefix) or path_declared_by(prefix, a)
    pa, pb = _static_prefix(a), _static_prefix(b)
    return (not pa) or (not pb) or path_declared_by(pa, pb) or path_declared_by(pb, pa)


def unsafe_path_reason(raw):
    """Why a raw `Files:` entry must be rejected, or None if it is acceptable.

    Entries are repo-relative. An absolute path, a home-relative path or one
    that climbs out with `..` would let an issue claim files outside the
    repository, and no gate could then map it back.
    """
    s = str(raw or "").strip().strip("`")
    if not s:
        return "is empty"
    if s.startswith(("/", "~")) or (len(s) > 1 and s[1] == ":"):
        return "is absolute; use a path relative to the repository root"
    if ".." in s.replace("\\", "/").split("/"):
        return "climbs out of the repository with '..'"
    return None


def load_config(path=None):
    """Read the Jira config. Returns {} if missing or unparseable.

    Resolution order: explicit path, then $JIRA_CONFIG_PATH (used by tests and
    by installs that keep config outside the repo), then the repo default.
    """
    candidate = path or os.environ.get("JIRA_CONFIG_PATH") or DEFAULT_CONFIG_PATH
    candidates = [candidate]
    if candidate == DEFAULT_CONFIG_PATH:
        # The config is gitignored, so a linked worktree does not have one.
        # Fall back to the main checkout's, found through the common git dir.
        try:
            import team_hook_common
            common = team_hook_common.git_common_dir(_REPO)
            if common:
                candidates.append(os.path.join(os.path.dirname(common), ".claude",
                                               "jira-config.json"))
        except Exception:
            pass
    for candidate in candidates:
        try:
            with open(candidate) as fh:
                cfg = json.load(fh)
            if isinstance(cfg, dict):
                return cfg
        except Exception:
            continue
    return {}


def _log_dir():
    # The coordination-state root (per repository), not the audit-log directory.
    # Imported lazily: team_hook_common sits beside this file but jira_mirror is
    # also imported by scripts that only add this directory to sys.path.
    try:
        import team_hook_common
        return team_hook_common.state_root()
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".claude", "logs")


def _mirror_dir():
    return MIRROR_DIR if MIRROR_DIR is not None else os.path.join(_log_dir(), "jira-mirror")


# Tests monkeypatch this to redirect the journal; None means "derive from $HOME".
MIRROR_DIR = None


def mirror_path(project_key):
    safe = "".join(c for c in str(project_key) if c.isalnum() or c in "._-") or "_"
    return os.path.join(_mirror_dir(), safe + ".jsonl")


def append_event(project_key, event):
    """Append one observed mutation. Never raises -- journalling must not break a hook."""
    try:
        os.makedirs(_mirror_dir(), exist_ok=True)
        line = json.dumps(event) + "\n"
        try:
            import team_hook_common
        except Exception:
            team_hook_common = None
        if team_hook_common is not None:
            # Never through a symlink or into a file with a second name.
            team_hook_common.append_private(mirror_path(project_key), line)
        else:
            with open(mirror_path(project_key), "a") as fh:
                fh.write(line)
    except Exception:
        pass


MIRROR_MAX_BYTES = 256 << 20


def load_state(project_key):
    """Fold the journal into {issue_key: {summary, labels, status, files}}.

    Later events win per field; a field absent from an event leaves the prior
    value intact (a transition event carries no labels, and must not erase them).
    Every key in _DEFAULTS is always present, so an event written before a field
    existed yields the default rather than a KeyError. Corrupt lines are skipped
    rather than aborting the fold.
    """
    state = {}
    try:
        # Never blocks and never follows a link: a FIFO at this path used to
        # hang the hook that read it.
        import evidence
        data = evidence.read_regular_file(mirror_path(project_key), MIRROR_MAX_BYTES)
        if data is None:
            return state
        lines = data.decode("utf-8", "replace").splitlines()
    except Exception:
        return state

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
        except Exception:
            continue
        key = event.get("key")
        if not key:
            continue
        issue = state.setdefault(key, _fresh_defaults())
        for field in _MERGEABLE:
            if event.get(field) is not None:
                issue[field] = event[field]
    return state


def normalize_issue_key(issue_key):
    """Canonical form of an issue reference: 'ag-14' / ' AG-14 ' -> 'AG-14'.

    Jira accepts keys case-insensitively and accepts issue URLs, but every hook
    scopes itself by comparing the key's project prefix with the configured
    project. Compared raw, a lowercase key read as "another project" and walked
    past the verification gate. A numeric id is returned unchanged (it carries
    no project; callers must treat it as unresolved, not as foreign).
    """
    s = str(issue_key or "").strip()
    if "/browse/" in s:
        s = s.split("/browse/", 1)[1].split("?", 1)[0].split("/", 1)[0]
    return s.upper() if "-" in s else s


def is_numeric_issue_id(issue_key):
    return str(issue_key or "").strip().isdigit()


def issue_project(issue_key):
    """'AGENT-14' -> 'AGENT'. Returns None for a numeric id or malformed key."""
    s = normalize_issue_key(issue_key)
    if "-" not in s:
        return None
    prefix = s.rsplit("-", 1)[0]
    return prefix or None


def agent_label(labels):
    """The first agent-* label in the list, or None if unclaimed."""
    try:
        for label in labels or []:
            if str(label).startswith("agent-"):
                return label
    except Exception:
        pass
    return None
