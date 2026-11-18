"""Coordination state for concurrent /ship-it executor sessions.

Owns two directories under ~/.claude/logs/:

  executors/<KEY>/<token>/   one entry per live executor session
  locks/<KEY>/<name>/        named mutexes: git-commit, finalize

Both use `mkdir` as a POSIX atomic test-and-set, the same primitive the claim
protocol in rules/agent-team-protocol.md relies on, and for the same reason: it
is the only cross-session atomic operation available on a shared $HOME. This
is also what makes token collisions detectable (D-010): `register()`'s token
directory is created with `os.mkdir`, never `os.makedirs(..., exist_ok=True)`,
so an already-existing directory is an observable event, not silently
absorbed -- see `register()`'s docstring for how it tells "my own refresh"
apart from "a different session already holds this exact token".

Design rule, inherited from .claude/hooks/: FAIL OPEN. A registry that cannot be
read yields an empty peer list, never an exception -- a coordination-metadata
problem must not stop an executor from doing its work.

Standard library only. Never touches Jira and never reads a credential.
"""
import hashlib
import os
import sys
import re
from datetime import datetime, timezone

STALE_MINS_DEFAULT = 60
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _home():
    return os.path.expanduser("~")


def log_dir():
    """Root for the executor registry and run locks.

    Delegates to the hooks' per-repository state root so the registry, the
    commit mutex and the finalize lock cannot be shared with another checkout
    that happens to use the same Jira project key. Falls back to the legacy
    location only if the hooks directory cannot be imported.
    """
    try:
        hooks = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             ".claude", "hooks")
        if hooks not in sys.path:
            sys.path.insert(0, hooks)
        import team_hook_common
        return team_hook_common.state_root()
    except Exception:
        return os.path.join(_home(), ".claude", "logs")


def _safe(value, default="_"):
    """Sanitize one untrusted path component. Mirrors team_hook_common.safe_path_component.

    Tokens and project keys reach us from the environment and from argv, and get
    joined into paths we mkdir and remove. '../../x' must not traverse out.
    """
    s = "" if value is None else str(value)
    s = os.path.basename(s)
    s = _UNSAFE.sub("_", s)
    return default if s in ("", ".", "..") else s


def registry_dir(key):
    return os.path.join(log_dir(), "executors", _safe(key))


def locks_dir(key):
    return os.path.join(log_dir(), "locks", _safe(key))


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def session_token(session_id=None, width=3):
    """Short, stable id for this session. None when no session id is available.

    Derived from CLAUDE_CODE_SESSION_ID -- verified as the exported name; there
    is no CLAUDE_SESSION_ID. Stability matters because a session re-invoking
    /ship-it must land on the same token rather than registering a phantom peer.
    """
    sid = session_id if session_id is not None else os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return None
    return hashlib.sha256(str(sid).encode("utf-8")).hexdigest()[:width]


def _write(path, text):
    try:
        with open(path, "w") as fh:
            fh.write(str(text))
    except Exception:
        pass


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except Exception:
        return None


def _age_mins(stamp):
    """Whole minutes since an ISO-8601 UTC stamp, or None if unparseable."""
    if not stamp:
        return None
    try:
        then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return int((datetime.now(timezone.utc) - then).total_seconds() // 60)


def register(key, token, spec=None, lane=None, session_name=None, session_id=None):
    """Create or refresh this session's registry entry. Returns the entry dict.

    D-010 -- token collision detection, without weakening idempotent refresh.
    `token` is the PREFERRED token (typically `session_token()`'s short
    digest prefix). The collision check spec.md requires is the `os.mkdir`
    that creates the token directory, exactly as `acquire_lock()` below does
    for named locks -- `mkdir` is the only atomic test-and-set available on
    a shared $HOME, so `makedirs(..., exist_ok=True)` (which never raises on
    an existing directory) cannot distinguish "this is my own re-register"
    from "someone else already claimed this exact token".

    The two cases look identical from `mkdir`'s FileExistsError alone, so
    the entry also records which session id created it:
      - directory doesn't exist yet: create it, this token is ours.
      - directory exists, stored session id matches ours (or none is
        recorded, e.g. an entry from before this fix): a refresh, not a
        collision -- keep the same token.
      - directory exists, stored session id is a DIFFERENT session: a
        genuine collision. Widen the token by one hex character of the
        caller's session-id digest and retry, up to the full 64-char sha256
        digest, which makes a collision self-healing rather than fatal.
    """
    sid = session_id if session_id is not None else os.environ.get("CLAUDE_CODE_SESSION_ID")
    sid_str = None if sid is None else str(sid)
    full_digest = (hashlib.sha256(sid_str.encode("utf-8")).hexdigest()
                   if sid_str is not None else None)

    root = registry_dir(key)
    try:
        os.makedirs(root, exist_ok=True)
    except Exception:
        return {}

    candidate = _safe(token)
    width = len(candidate)
    entry = os.path.join(root, candidate)
    while True:
        try:
            os.mkdir(entry)
            break   # fresh slot: this token is ours, uncontested
        except FileExistsError:
            stored_sid = _read(os.path.join(entry, "session-id"))
            if stored_sid is None or stored_sid == sid_str:
                break   # our own entry (refresh), or an unattributed one
            if full_digest is None or width >= len(full_digest):
                # No session id to widen with, or already at full
                # resolution -- nothing left to try. Fail open and share
                # the slot rather than loop forever; register()'s only
                # real caller always supplies a digest-derived token plus
                # a live CLAUDE_CODE_SESSION_ID, so this is unreachable in
                # practice.
                break
            width += 1
            candidate = full_digest[:width]
            entry = os.path.join(root, candidate)
        except Exception:
            return {}

    _write(os.path.join(entry, "heartbeat"), utc_now())
    _write(os.path.join(entry, "pid"), os.getpid())
    if sid_str is not None:
        _write(os.path.join(entry, "session-id"), sid_str)
    if spec is not None:
        _write(os.path.join(entry, "spec"), spec)
    if lane is not None:
        _write(os.path.join(entry, "lane"), lane)
    if session_name is not None:
        _write(os.path.join(entry, "session-name"), session_name)
    return {"token": candidate, "spec": spec, "lane": lane,
            "session_name": session_name}


def release(key, token):
    """Remove only this token's entry. Never touches a peer's."""
    entry = os.path.join(registry_dir(key), _safe(token))
    try:
        for name in os.listdir(entry):
            os.remove(os.path.join(entry, name))
        os.rmdir(entry)
        return True
    except Exception:
        return False


def peers(key, stale_mins=STALE_MINS_DEFAULT):
    """Every registered executor, stale ones flagged but never removed.

    Reporting rather than reaping is the same discipline the stale-claim sweep
    uses: a session in a long verification pass is indistinguishable from a dead
    one, and under lazy pool growth a phantom peer costs nothing anyway.
    """
    root = registry_dir(key)
    try:
        tokens = sorted(os.listdir(root))
    except Exception:
        return []
    out = []
    for token in tokens:
        entry = os.path.join(root, token)
        if not os.path.isdir(entry):
            continue
        heartbeat = _read(os.path.join(entry, "heartbeat"))
        age = _age_mins(heartbeat)
        out.append({
            "token": token,
            "heartbeat": heartbeat,
            "age_mins": age,
            "stale": True if age is None else age >= stale_mins,
            "spec": _read(os.path.join(entry, "spec")),
            "lane": _read(os.path.join(entry, "lane")),
            "session_name": _read(os.path.join(entry, "session-name")),
            "pid": _read(os.path.join(entry, "pid")),
        })
    return out


def acquire_lock(key, name, owner, stale_mins=None):
    """Take a named lock. True if it is now ours.

    stale_mins opts into takeover of an abandoned lock. It is deliberately
    opt-in and off by default: `finalize` is takeover-able because a holder
    dying mid-Phase-4 would otherwise strand documentation forever and a
    repeated documentation pass is merely wasteful, while `git-commit` is not,
    because stealing it produces two concurrent commits -- the exact contention
    the lock exists to prevent.
    """
    lock = os.path.join(locks_dir(key), _safe(name))
    try:
        os.makedirs(locks_dir(key), exist_ok=True)
    except Exception:
        return False
    try:
        os.mkdir(lock)
    except FileExistsError:
        if stale_mins is None:
            return False
        age = _age_mins(_read(os.path.join(lock, "heartbeat")))
        if age is None or age < stale_mins:
            return False
        # Stale: adopt it in place. Rewriting owner is the takeover record.
    except Exception:
        return False
    _write(os.path.join(lock, "owner"), owner)
    _write(os.path.join(lock, "heartbeat"), utc_now())
    return True


def refresh_lock(key, name):
    """Keep a held lock alive during long work, so a peer does not adopt it."""
    lock = os.path.join(locks_dir(key), _safe(name))
    if os.path.isdir(lock):
        _write(os.path.join(lock, "heartbeat"), utc_now())


def release_lock(key, name):
    lock = os.path.join(locks_dir(key), _safe(name))
    try:
        for fname in os.listdir(lock):
            os.remove(os.path.join(lock, fname))
        os.rmdir(lock)
    except Exception:
        pass
