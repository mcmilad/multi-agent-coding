import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location(
    "board_triage", os.path.join(ROOT, "scripts", "board_triage.py"))
bt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bt)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path), 1))
    logs = tmp_path / ".claude" / "logs"
    logs.mkdir(parents=True)
    return tmp_path


def _stamp(minutes_ago=0):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _claim(home, key, issue, owner, minutes_ago=0):
    d = home / ".claude" / "logs" / "claims" / key / issue
    d.mkdir(parents=True)
    (d / "owner").write_text(owner)
    (d / "heartbeat").write_text(_stamp(minutes_ago))


def test_a_fresh_home_produces_an_empty_report_and_exit_0(home):
    report = bt.collect("AGENT", stale_mins=60)
    assert report["claims"] == []
    assert report["peers"] == []
    assert bt.main(["--key", "AGENT"]) == 0


def test_a_live_claim_is_reported_as_live(home):
    _claim(home, "AGENT", "AGENT-91", "coding-c3-2", minutes_ago=4)
    claim = bt.collect("AGENT", 60)["claims"][0]
    assert claim["issue"] == "AGENT-91"
    assert claim["owner"] == "coding-c3-2"
    assert claim["stale"] is False


def test_a_quiet_claim_is_reported_as_stale(home):
    _claim(home, "AGENT", "AGENT-93", "coding-c3-1", minutes_ago=78)
    claim = bt.collect("AGENT", 60)["claims"][0]
    assert claim["stale"] is True
    assert claim["age_mins"] >= 77


def test_a_claim_without_a_heartbeat_is_stale_not_skipped(home):
    d = home / ".claude" / "logs" / "claims" / "AGENT" / "AGENT-94"
    d.mkdir(parents=True)
    (d / "owner").write_text("ghost")
    claim = bt.collect("AGENT", 60)["claims"][0]
    assert claim["stale"] is True, "a lock invisible to the sweep is the dangerous case"


def test_verified_and_inflight_sentinels_are_distinguished(home):
    d = home / ".claude" / "logs" / "verified" / "AGENT"
    d.mkdir(parents=True)
    (d / "AGENT-88.verified").write_text("pytest PASSED")
    (d / "AGENT-93.inflight").write_text("pytest PASSED")
    s = bt.collect("AGENT", 60)["sentinels"]
    assert {"issue": "AGENT-88", "state": "verified"} in s
    assert {"issue": "AGENT-93", "state": "inflight"} in s


def test_the_mirror_folds_forward_to_the_last_status(home):
    p = home / ".claude" / "logs" / "jira-mirror" / "AGENT.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("\n".join([
        json.dumps({"op": "create", "key": "AGENT-1"}),
        json.dumps({"op": "transition", "key": "AGENT-1", "status": "In Progress"}),
        json.dumps({"op": "transition", "key": "AGENT-1", "status": "Done"}),
    ]) + "\n")
    mirror = bt.collect("AGENT", 60)["mirror"]
    assert mirror["by_status"]["Done"] == 1
    assert "In Progress" not in mirror["by_status"]


def test_a_corrupt_mirror_line_is_skipped_not_fatal(home):
    p = home / ".claude" / "logs" / "jira-mirror" / "AGENT.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text('{"op":"transition","key":"AGENT-1","status":"Done"}\nnot json\n')
    assert bt.collect("AGENT", 60)["mirror"]["by_status"]["Done"] == 1


def test_a_non_string_mirror_key_is_skipped_and_counted_not_folded_in(home):
    p = home / ".claude" / "logs" / "jira-mirror" / "AGENT.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("\n".join([
        json.dumps({"op": "transition", "key": ["AGENT-1"], "status": "Done"}),
        json.dumps({"op": "transition", "key": "AGENT-2", "status": "Done"}),
    ]) + "\n")
    mirror = bt.collect("AGENT", 60)["mirror"]
    assert mirror["available"] is True
    assert mirror["by_status"]["Done"] == 1
    assert mirror["skipped"] == 1


def test_a_non_string_mirror_key_does_not_discard_earlier_valid_lines(home):
    # Regression: status[rec["key"]] used to sit inside the same try/except
    # that wraps the whole file, so a single bad key (unhashable, e.g. a
    # list) raised TypeError and the outer except discarded every op
    # already folded from the good lines before it -- reporting the whole
    # mirror as unavailable even though the file opened and mostly parsed.
    p = home / ".claude" / "logs" / "jira-mirror" / "AGENT.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("\n".join([
        json.dumps({"op": "transition", "key": "AGENT-1", "status": "Done"}),
        json.dumps({"op": "transition", "key": "AGENT-2", "status": "In Review"}),
        json.dumps({"op": "transition", "key": ["not", "a", "string"], "status": "Done"}),
    ]) + "\n")
    mirror = bt.collect("AGENT", 60)["mirror"]
    assert mirror["available"] is True, "one bad record must not sink the whole file"
    assert mirror["by_status"] == {"Done": 1, "In Review": 1}
    assert mirror["ops"] == 3
    assert mirror["skipped"] == 1


def test_a_non_string_mirror_status_is_skipped_and_counted(home):
    p = home / ".claude" / "logs" / "jira-mirror" / "AGENT.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"op": "transition", "key": "AGENT-1", "status": 200}) + "\n")
    mirror = bt.collect("AGENT", 60)["mirror"]
    assert mirror["by_status"] == {}
    assert mirror["issues"] == 0
    assert mirror["skipped"] == 1


def test_hook_blocks_are_counted_from_the_audit_log(home):
    p = home / ".claude" / "logs" / "team-hooks.jsonl"
    p.write_text("\n".join([
        json.dumps({"event": "claim_gate", "decision": "block"}),
        json.dumps({"event": "claim_gate", "decision": "allow"}),
        json.dumps({"event": "issue_format", "decision": "block"}),
    ]) + "\n")
    hooks = bt.collect("AGENT", 60)["hooks"]
    assert hooks["blocks"]["claim_gate"] == 1
    assert hooks["blocks"]["issue_format"] == 1


def test_register_adds_this_session_to_peers(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-a")
    assert bt.main(["--key", "AGENT", "--spec", "shorty", "--register"]) == 0
    assert len(bt.collect("AGENT", 60)["peers"]) == 1


def test_release_removes_this_session_from_peers(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-a")
    bt.main(["--key", "AGENT", "--register"])
    bt.main(["--key", "AGENT", "--release"])
    assert bt.collect("AGENT", 60)["peers"] == []


def test_register_and_release_together_is_a_usage_error(home):
    assert bt.main(["--key", "AGENT", "--register", "--release"]) == 2


def test_missing_key_is_a_usage_error(home):
    assert bt.main([]) == 2


def test_json_output_is_parseable(home, capsys):
    _claim(home, "AGENT", "AGENT-91", "coding-c3-2")
    bt.main(["--key", "AGENT", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["claims"][0]["issue"] == "AGENT-91"


def test_the_report_has_no_ready_or_suggest_section(home, capsys):
    bt.main(["--key", "AGENT"])
    out = capsys.readouterr().out
    assert "READY" not in out and "SUGGEST" not in out, \
        "neither is computable from disk; see D-007"


def test_an_unreadable_logs_tree_still_exits_0(home, monkeypatch):
    monkeypatch.setattr(os, "listdir", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert bt.main(["--key", "AGENT"]) == 0, "triage must never stop an executor"


def test_a_triage_invocation_appends_to_the_audit_stream(home):
    # spec.md -> Non-Functional -> Operability: "Every triage invocation
    # appends to the existing ~/.claude/logs/team-hooks.jsonl audit stream,
    # matching hook behaviour." Verified by review-1 against a temp $HOME:
    # after two invocations the file was 0 bytes. This must not regress.
    hooks_path = home / ".claude" / "logs" / "team-hooks.jsonl"
    assert not hooks_path.exists()
    bt.main(["--key", "AGENT"])
    assert hooks_path.exists() and hooks_path.stat().st_size > 0
    lines = [ln for ln in hooks_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "board_triage"


def test_two_triage_invocations_append_two_records(home):
    hooks_path = home / ".claude" / "logs" / "team-hooks.jsonl"
    bt.main(["--key", "AGENT"])
    bt.main(["--key", "AGENT"])
    lines = [ln for ln in hooks_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2


def test_an_audit_invocation_also_appends_to_the_stream(home, monkeypatch):
    import io
    hooks_path = home / ".claude" / "logs" / "team-hooks.jsonl"
    monkeypatch.setattr("sys.stdin", io.StringIO("[]"))
    bt.main(["--audit"])
    assert hooks_path.exists() and hooks_path.stat().st_size > 0


def test_help_exits_0_not_2(home):
    # argparse's own --help calls parser.exit(), which raises SystemExit(0) --
    # a bare `except SystemExit: return 2` swallows that success exit and
    # reports it as a usage error. Only a genuine parse failure is a 2.
    with pytest.raises(SystemExit) as exc_info:
        bt.main(["--help"])
    assert exc_info.value.code == 0


def test_collect_claims_key_is_sanitized_like_executor_registry(home):
    _claim(home, "AGENT", "AGENT-91", "coding-c3-2")
    # Same traversal-style key executor_registry's _safe() guards against.
    # os.path.basename() strips the leading directory components, so this
    # must resolve to the same claims list as the clean "AGENT" key rather
    # than reading (or failing to read) some other path entirely.
    assert bt.collect_claims("../../../AGENT", 60) == bt.collect_claims("AGENT", 60)


# --- AGENT-106 Critical: the token register() actually assigns, not the
# discarded preferred one, must be what's reported and what --release acts on ---

def test_the_reported_token_matches_the_entry_actually_created_after_a_collision(
        home, monkeypatch, capsys):
    # Force a genuine collision deterministically: pre-occupy THIS session's
    # own natural (unwidened) token under a foreign session id, so
    # register() must widen when this invocation registers for real.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-B")
    preferred = bt.reg.session_token()
    occupied = os.path.join(bt.reg.registry_dir("AGENT"), preferred)
    os.makedirs(occupied)
    with open(os.path.join(occupied, "session-id"), "w") as fh:
        fh.write("session-A-foreign")

    assert bt.main(["--key", "AGENT", "--register", "--json"]) == 0
    reported = json.loads(capsys.readouterr().out)["token"]

    mine = [p for p in bt.reg.peers("AGENT") if p["token"] != preferred]
    assert len(mine) == 1, "the collision must have produced exactly one new entry"
    assert reported == mine[0]["token"], \
        "the reported TOKEN must be the one register() actually assigned, " \
        "not the preferred value that collided"
    assert reported != preferred


def test_release_removes_only_the_callers_entry_after_a_collision(home, monkeypatch):
    # Reproduces review-1's exact cycle-2 scenario: two sessions collide on
    # the same preferred token; session B's release must not delete session
    # A's entry. session_token() is patched to return the SAME preferred
    # value for both sessions -- deterministically forcing the collision
    # register() would otherwise only hit on a genuine (rare) hash match --
    # while each session's REAL id (via CLAUDE_CODE_SESSION_ID) still drives
    # register()'s actual widening/ownership logic untouched.
    monkeypatch.setattr(bt.reg, "session_token", lambda *a, **k: "234")

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-A")
    assert bt.main(["--key", "AGENT", "--spec", "specA", "--register"]) == 0

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-B")
    assert bt.main(["--key", "AGENT", "--spec", "specB", "--register"]) == 0

    before = {p["token"]: p["spec"] for p in bt.reg.peers("AGENT")}
    assert before.get("234") == "specA", "session A must keep the uncontested preferred token"
    assert len(before) == 2, "the collision must have produced two distinct entries"

    # Session B (still the active env session) releases. Only B's own
    # (widened) entry may be removed.
    assert bt.main(["--key", "AGENT", "--release"]) == 0
    after = {p["token"]: p["spec"] for p in bt.reg.peers("AGENT")}
    assert after == {"234": "specA"}, \
        "session B's release must not delete session A's entry"


# --- D-013: the audit-append degradation must be visible, never silent ---

def test_a_broken_hooks_import_surfaces_on_stderr_but_still_exits_0(home, monkeypatch, capsys):
    monkeypatch.setattr(bt, "hooks_common", None)
    assert bt.main(["--key", "AGENT"]) == 0
    err = capsys.readouterr().err
    assert "team_hook_common" in err, \
        "a degraded audit-append must be visible, not indistinguishable from a healthy run"


def test_a_healthy_hooks_import_does_not_print_the_degradation_message(home, capsys):
    assert bt.hooks_common is not None, "sanity: the real import must succeed in this repo"
    bt.main(["--key", "AGENT"])
    assert "team_hook_common" not in capsys.readouterr().err
