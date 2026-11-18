"""Deterministic disk-state report for a /ship-it executor session.

Answers the questions an executor cannot answer reliably by hand: who holds
which claim and for how long, which attestations are earned versus in flight,
what the mirror last saw, and which peers are live.

Reads and writes only ~/.claude/logs/** (--register/--release delegate to
executor_registry, which mkdirs and writes heartbeat/pid/spec/lane/session-name
files under executors/). It never calls Jira and never reads a credential --
that isolation is the point. The executor supplies board state from JQL and
owns every decision derived from it; this script owns disk facts and nothing
else. See D-007 for why there is no READY or SUGGEST section.

Fails open in the manner of .claude/hooks/: a partial report and exit 0 beat an
exception. Most of that comes from the per-section guards in each collect_*
function; the outer try/except in main() is a last-resort backstop that, on a
genuine crash, emits one stderr line and still exits 0 rather than a partial
report body, because a coordination-metadata problem must not stop real work.

Standard library only.

Usage:
  python3 scripts/board_triage.py --audit                      # no --key needed
  python3 scripts/board_triage.py --key AGENT [--spec shorty] [--lane api]
      [--session-name shorty-1] [--register | --release] [--json] [--stale-mins 60]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backlog_audit          # noqa: E402
import executor_registry as reg   # noqa: E402

# Cross-tree import, by design (see design.md -> Components): team_hook_common
# owns log-path derivation and the audit-append helper, so a log-layout change
# moves one file instead of three. jira_bootstrap.py deliberately keeps its own
# _as_dict rather than doing this; board_triage.py does the import because
# design.md requires it explicitly for this script. Guarded because a missing
# or moved hooks directory must degrade this script to doing nothing extra,
# never crash it -- fail open, like every other guardrail here.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".claude", "hooks"))
try:
    import team_hook_common as hooks_common   # noqa: E402
except Exception:
    hooks_common = None

HOOK_TAIL_BYTES = 512 * 1024   # bounded: team-hooks.jsonl reaches many MB


def _log_dir():
    """Audit-log root (~/.claude/logs). Delegates to team_hook_common when
    importable, so this and every hook agree on one definition."""
    if hooks_common is not None:
        return hooks_common.log_dir()
    return os.path.join(os.path.expanduser("~"), ".claude", "logs")


def _state_dir():
    """Coordination-state root (claims, sentinels, mirror): per repository.
    See team_hook_common.state_root."""
    if hooks_common is not None:
        return hooks_common.state_root()
    return os.path.join(os.path.expanduser("~"), ".claude", "logs")


def _safe_key(key):
    """Sanitize a --key value the same way executor_registry sanitizes a
    token, so the two modules agree on what a safe path component is instead
    of one of them trusting the raw value."""
    return reg._safe(key)


def _record_invocation(args):
    """Append one record per triage invocation to the shared audit stream.

    Required by spec.md -> Non-Functional -> Operability: "so the executor's
    view of the world at spawn time is reconstructable after the fact." Uses
    team_hook_common.audit(), which already fails open internally (any write
    error is swallowed there).

    D-013: the cross-tree import stays guarded -- a moved or broken
    team_hook_common must degrade this script, never crash it -- but the
    degradation must be VISIBLE. Before this, a broken import and a healthy
    run were byte-identical: exit 0, nothing on stderr, the stream silently
    at 0 bytes. The guard is not the defect; the silence was.
    """
    if hooks_common is None:
        print("board_triage: team_hook_common unavailable -- audit-append "
              "skipped for this invocation", file=sys.stderr)
        return
    hooks_common.audit(
        "board_triage",
        {"key": args.key, "spec": args.spec, "lane": args.lane,
         "register": bool(args.register), "release": bool(args.release),
         "audit": bool(args.audit)},
        "report",
    )


# Aliased rather than re-derived: executor_registry already defines both
# (board_triage already imports it as `reg`), and duplicating the same
# datetime-parsing logic a third time is exactly the "three edits for one
# log-layout change" outcome design.md's team_hook_common reuse note exists
# to prevent.
_age_mins = reg._age_mins
_read = reg._read


def _resolve_own_token(key, preferred, sid):
    """The token this session's own registry entry is actually stored
    under -- which may differ from `preferred`.

    D-012/AGENT-106 Critical: `session_token()` is a pure function of the
    session id, so it always returns the same PREFERRED value, even after
    `register()` widened it to resolve a genuine collision (D-010). A later
    invocation -- typically --release -- must not blindly reuse that
    preferred value: if it collided with a peer at register time, the
    PEER, not this session, owns the directory at the preferred token, and
    acting on it would delete the peer's live entry rather than this
    session's own. That is exactly what review-1 reproduced: session B's
    `--release` deleted session A's entry because both computed the same
    preferred token and only `register()` -- never `main()` -- knew B's had
    widened.

    Resolves ownership the same way `register()` itself does: by the
    stored session-id, not by assuming the preferred token is ours. Falls
    back to `preferred` when nothing on disk is attributable to `sid`
    (never registered, or no session id available at all) -- the same
    fail-open posture as every other lookup in this file.
    """
    if sid is None:
        return preferred
    root = reg.registry_dir(key)
    try:
        tokens = os.listdir(root)
    except Exception:
        return preferred
    for tok in tokens:
        if _read(os.path.join(root, tok, "session-id")) == sid:
            return tok
    return preferred


def collect_claims(key, stale_mins):
    root = os.path.join(_state_dir(), "claims", _safe_key(key))
    try:
        issues = sorted(os.listdir(root))
    except Exception:
        return []
    out = []
    for issue in issues:
        entry = os.path.join(root, issue)
        if not os.path.isdir(entry):
            continue
        heartbeat = _read(os.path.join(entry, "heartbeat"))
        age = _age_mins(heartbeat)
        out.append({
            "issue": issue,
            "owner": _read(os.path.join(entry, "owner")),
            "heartbeat": heartbeat,
            "age_mins": age,
            # An absent or unparseable heartbeat reads as stale on purpose: the
            # lead's sweep matches on the heartbeat FILE, so a lock without one
            # is invisible to the only thing that could ever recover the issue.
            "stale": True if age is None else age >= stale_mins,
        })
    return out


def collect_sentinels(key):
    root = os.path.join(_state_dir(), "verified", _safe_key(key))
    try:
        names = sorted(os.listdir(root))
    except Exception:
        return []
    out = []
    for name in names:
        for suffix, state in ((".verified", "verified"), (".inflight", "inflight")):
            if name.endswith(suffix):
                out.append({"issue": name[: -len(suffix)], "state": state})
                break
    return out


def collect_mirror(key):
    """Fold the mirror journal forward to a last-known status per issue.

    Best-effort by construction -- the journal only sees MCP mutations, so a
    card moved by hand on the board never appears. The executor reconciles
    against JQL; this is a hint about recent activity, not a source of truth.

    Every record is model-authored JSON, so `key` and `status` are
    type-checked before use, not just truthiness-checked. `rec.get("key")`
    passing a truthiness check does not make it a valid dict key for the
    fold: a record like {"op": "transition", "key": ["A-1"], ...} has a
    truthy, non-string key that would raise TypeError on
    `status[rec["key"]] = ...` -- and because that assignment used to sit
    INSIDE the same try/except that wraps the whole file read, one such
    record would discard every op already folded from earlier, GOOD lines
    in the file and report the mirror as fully "unavailable". A malformed
    record must be skipped and counted, never allowed to take down the
    lines that parsed fine before it.
    """
    path = os.path.join(_state_dir(), "jira-mirror", "%s.jsonl" % _safe_key(key))
    status = {}
    ops = 0
    skipped = 0
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue      # a torn write must not lose the whole file
                if not isinstance(rec, dict):
                    continue
                ops += 1
                if rec.get("op") != "transition":
                    continue
                rec_key = rec.get("key")
                rec_status = rec.get("status")
                if not isinstance(rec_key, str) or not rec_key or (
                        rec_status is not None and not isinstance(rec_status, str)):
                    skipped += 1
                    continue
                status[rec_key] = rec_status
    except Exception:
        return {"ops": 0, "by_status": {}, "issues": 0, "available": False, "skipped": 0}

    by_status = {}
    for value in status.values():
        if value:
            by_status[value] = by_status.get(value, 0) + 1
    return {"ops": ops, "by_status": by_status, "issues": len(status),
            "available": True, "skipped": skipped}


def collect_hooks():
    """Recent block decisions, from a bounded tail of the shared audit log."""
    path = os.path.join(_log_dir(), "team-hooks.jsonl")
    blocks = {}
    try:
        size = os.path.getsize(path)
        with open(path) as fh:
            if size > HOOK_TAIL_BYTES:
                fh.seek(size - HOOK_TAIL_BYTES)
                fh.readline()      # discard the partial first line
            for line in fh:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict) and rec.get("decision") == "block":
                    event = rec.get("event") or "unknown"
                    blocks[event] = blocks.get(event, 0) + 1
    except Exception:
        pass
    return {"blocks": blocks}


def collect(key, stale_mins):
    return {
        "peers": reg.peers(key, stale_mins),
        "claims": collect_claims(key, stale_mins),
        "sentinels": collect_sentinels(key),
        "mirror": collect_mirror(key),
        "hooks": collect_hooks(),
    }


def render(report):
    lines = []
    peers = report["peers"]
    if not peers:
        lines.append("PEERS       none registered -- you are the only executor")
    for p in peers:
        lines.append("PEERS       %-6s %-14s %s%s" % (
            p["token"], p.get("session_name") or "(unnamed)",
            "heartbeat %sm ago" % p["age_mins"] if p["age_mins"] is not None else "no heartbeat",
            "  STALE" if p["stale"] else ""))
    for c in report["claims"]:
        lines.append("CLAIMS      %-12s owner=%-16s %s%s" % (
            c["issue"], c.get("owner") or "(unknown)",
            "heartbeat %sm ago" % c["age_mins"] if c["age_mins"] is not None else "no heartbeat",
            "  STALE" if c["stale"] else "  LIVE"))
    if not report["claims"]:
        lines.append("CLAIMS      none held")
    for s in report["sentinels"]:
        note = "  (transition in air)" if s["state"] == "inflight" else ""
        lines.append("SENTINELS   %-12s %s%s" % (s["issue"], s["state"], note))
    if not report["sentinels"]:
        lines.append("SENTINELS   none")
    m = report["mirror"]
    if m.get("available"):
        counts = "  ".join("%d %s" % (n, s) for s, n in sorted(m["by_status"].items()))
        skipped_note = "  (%d malformed record(s) skipped)" % m["skipped"] if m.get("skipped") else ""
        lines.append("MIRROR      %d ops, %d issues | %s  (best-effort)%s" % (
            m["ops"], m["issues"], counts or "no transitions", skipped_note))
    else:
        lines.append("MIRROR      unavailable  (best-effort; reconcile against JQL)")
    blocks = report["hooks"]["blocks"]
    lines.append("HOOKS       %s" % (
        "  ".join("%d %s" % (n, e) for e, n in sorted(blocks.items())) or "no recent blocks"))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Disk-state report for a /ship-it executor session")
    parser.add_argument("--key")
    parser.add_argument("--spec")
    parser.add_argument("--lane")
    parser.add_argument("--session-name")
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--stale-mins", type=int, default=reg.STALE_MINS_DEFAULT)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse's own --help/--version call parser.exit(), which raises
        # SystemExit(0) -- a genuine success, not a usage error. Only a
        # nonzero code (an actual parse failure) is this script's "2".
        if exc.code == 0:
            raise
        return 2

    _record_invocation(args)

    if args.audit:
        return backlog_audit.main([])

    if not args.key:
        print("board_triage: --key is required", file=sys.stderr)
        return 2
    if args.register and args.release:
        print("board_triage: --register and --release are mutually exclusive",
              file=sys.stderr)
        return 2

    token = reg.session_token()
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if args.register:
        if token is None:
            print("board_triage: no CLAUDE_CODE_SESSION_ID; cannot register",
                  file=sys.stderr)
        else:
            assigned = reg.register(args.key, token, spec=args.spec, lane=args.lane,
                                     session_name=args.session_name)
            # AGENT-106 Critical: register() widens the token on a genuine
            # collision (D-010) and returns the token actually assigned.
            # Reporting the discarded preferred `token` instead is exactly
            # what broke D-010's guarantee -- ship-it.md's instance naming
            # and this session's own later --release would both keep using
            # a value nothing on disk is stored under. register() returns
            # {} on failure, hence the `or token` fallback.
            token = assigned.get("token") or token
    elif args.release and token is not None:
        # Same Critical, release side: session_token() always recomputes
        # the PREFERRED token, never the widened one. Resolving by stored
        # session-id (not by assuming the preferred value is ours) is what
        # stops this session's release from deleting a colliding peer's
        # live entry -- the exact failure review-1 reproduced.
        token = _resolve_own_token(args.key, token, sid)
        reg.release(args.key, token)

    try:
        report = collect(args.key, args.stale_mins)
    except Exception as exc:
        # Belt and braces over the per-section guards: an executor blocked by a
        # triage crash is strictly worse than one running on no report at all.
        print("board_triage: partial report (%s)" % exc, file=sys.stderr)
        return 0

    report["token"] = token
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        if token:
            print("TOKEN       %s" % token)
        print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
