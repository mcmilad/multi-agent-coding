import hashlib
import importlib.util
import os
from datetime import datetime, timedelta, timezone

import pytest

SPEC = importlib.util.spec_from_file_location(
    "executor_registry",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scripts", "executor_registry.py"),
)
reg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reg)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path), 1))
    return tmp_path


def _stamp(minutes_ago):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_token_is_stable_for_the_same_session_id():
    assert reg.session_token("abc-123") == reg.session_token("abc-123")


def test_token_differs_between_sessions():
    assert reg.session_token("abc-123") != reg.session_token("def-456")


def test_token_reads_the_correct_env_var(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-env")
    assert reg.session_token() == reg.session_token("from-env")


def test_token_is_none_when_no_session_id(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert reg.session_token() is None


def test_register_creates_the_entry_and_peers_reads_it_back(home):
    reg.register("AGENT", "c3", spec="shorty", lane="api", session_name="shorty-1")
    found = reg.peers("AGENT")
    assert len(found) == 1
    assert found[0]["token"] == "c3"
    assert found[0]["spec"] == "shorty"
    assert found[0]["lane"] == "api"
    assert found[0]["session_name"] == "shorty-1"
    assert found[0]["stale"] is False


def test_register_is_idempotent_and_refreshes_the_heartbeat(home):
    reg.register("AGENT", "c3")
    hb = os.path.join(reg.registry_dir("AGENT"), "c3", "heartbeat")
    with open(hb, "w") as fh:
        fh.write(_stamp(90))
    assert reg.peers("AGENT")[0]["stale"] is True
    reg.register("AGENT", "c3")
    assert reg.peers("AGENT")[0]["stale"] is False


def test_peers_marks_a_quiet_entry_stale_but_still_lists_it(home):
    reg.register("AGENT", "old")
    with open(os.path.join(reg.registry_dir("AGENT"), "old", "heartbeat"), "w") as fh:
        fh.write(_stamp(120))
    found = reg.peers("AGENT", stale_mins=60)
    assert len(found) == 1, "a stale peer must be reported, never reaped"
    assert found[0]["stale"] is True
    assert found[0]["age_mins"] >= 119


def test_peers_is_empty_and_does_not_raise_on_a_fresh_home(home):
    assert reg.peers("NEVER-USED") == []


def test_peers_tolerates_a_corrupt_entry(home):
    reg.register("AGENT", "good")
    os.makedirs(os.path.join(reg.registry_dir("AGENT"), "broken"))
    found = {p["token"] for p in reg.peers("AGENT")}
    assert found == {"good", "broken"}, "a heartbeat-less dir is listed, not skipped"


def test_release_removes_only_the_named_token(home):
    reg.register("AGENT", "c3")
    reg.register("AGENT", "a1")
    assert reg.release("AGENT", "c3") is True
    assert [p["token"] for p in reg.peers("AGENT")] == ["a1"]


def test_acquire_lock_is_exclusive(home):
    assert reg.acquire_lock("AGENT", "finalize", owner="c3") is True
    assert reg.acquire_lock("AGENT", "finalize", owner="a1") is False


def test_a_stale_finalize_lock_can_be_taken_over(home):
    reg.acquire_lock("AGENT", "finalize", owner="c3")
    hb = os.path.join(reg.locks_dir("AGENT"), "finalize", "heartbeat")
    with open(hb, "w") as fh:
        fh.write(_stamp(90))
    assert reg.acquire_lock("AGENT", "finalize", owner="a1", stale_mins=60) is True
    with open(os.path.join(reg.locks_dir("AGENT"), "finalize", "owner")) as fh:
        assert fh.read().strip() == "a1"


def test_a_live_lock_is_not_taken_over(home):
    reg.acquire_lock("AGENT", "finalize", owner="c3")
    assert reg.acquire_lock("AGENT", "finalize", owner="a1", stale_mins=60) is False


def test_takeover_is_opt_in(home):
    reg.acquire_lock("AGENT", "git-commit", owner="c3")
    hb = os.path.join(reg.locks_dir("AGENT"), "git-commit", "heartbeat")
    with open(hb, "w") as fh:
        fh.write(_stamp(999))
    assert reg.acquire_lock("AGENT", "git-commit", owner="a1") is False, \
        "without stale_mins a lock is never stealable"


def test_release_lock_allows_reacquisition(home):
    reg.acquire_lock("AGENT", "git-commit", owner="c3")
    reg.release_lock("AGENT", "git-commit")
    assert reg.acquire_lock("AGENT", "git-commit", owner="a1") is True


def test_a_token_cannot_escape_its_directory(home):
    # A walk of "../.." from .../executors/AGENT/ lands at
    # <home>/.claude/logs/escape, NOT <home>/escape -- asserting on the
    # latter made this test inert (review-1 proved it passes even with
    # _safe() removed from register() entirely, since neither path is the
    # one actually reached). Assert on the real landing spot, and confirm
    # the token was in fact sanitized to stay inside its own directory.
    reg.register("AGENT", "../../escape")
    assert not os.path.exists(os.path.join(str(home), ".claude", "logs", "escape"))
    assert os.path.isdir(os.path.join(reg.registry_dir("AGENT"), "escape"))


# --- D-010: token collision detection, without breaking idempotent refresh ---

def test_a_genuine_collision_between_two_sessions_widens_the_token(home):
    # Two different sessions both present the literal token "c3" (register()
    # trusts the caller's token; it does not re-derive it). "c3" is 2 chars,
    # so the first widen goes to 3. Before D-010, makedirs(exist_ok=True)
    # would have silently merged them into one entry and one instance-name
    # prefix -- exactly the failure the token exists to prevent. On
    # collision, the token widens using the COLLIDING session's OWN digest,
    # which is deterministic and computed independently here (not a
    # hoped-for real hash coincidence).
    reg.register("AGENT", "c3", session_id="session-A")
    b_digest = hashlib.sha256(b"session-B").hexdigest()
    entry_b = reg.register("AGENT", "c3", session_id="session-B")
    assert entry_b["token"] == b_digest[:3], \
        "collision must widen by exactly one hex char of the colliding session's digest"
    assert entry_b["token"] != "c3"
    tokens = {p["token"] for p in reg.peers("AGENT")}
    assert tokens == {"c3", entry_b["token"]}, "both sessions must have distinct entries"
    with open(os.path.join(reg.registry_dir("AGENT"), "c3", "session-id")) as fh:
        assert fh.read() == "session-A", "the original slot must still belong to session-A"


def test_a_same_session_reregister_with_an_explicit_session_id_does_not_widen(home):
    # The idempotent-refresh property must keep holding with collision
    # detection layered on top: two calls from the SAME session id must
    # land on the SAME token, not widen against themselves.
    first = reg.register("AGENT", "c3", session_id="session-A")
    second = reg.register("AGENT", "c3", session_id="session-A")
    assert first["token"] == second["token"] == "c3"
    assert len(reg.peers("AGENT")) == 1


def test_a_collision_widens_again_if_the_widened_token_also_collides(home):
    # A double pileup: session-A holds "c3" (2 chars); session-B's first
    # widen (to a 3-char prefix of ITS OWN digest) is pre-occupied by a
    # foreign session-C, forcing a second widen to 4 chars. Both slots
    # session-B's own digest will try are computed independently here, so
    # this does not depend on an engineered real hash collision.
    reg.register("AGENT", "c3", session_id="session-A")
    b_digest = hashlib.sha256(b"session-B").hexdigest()
    three_char = b_digest[:3]
    occupied = os.path.join(reg.registry_dir("AGENT"), three_char)
    os.makedirs(occupied)
    with open(os.path.join(occupied, "session-id"), "w") as fh:
        fh.write("session-C")
    entry_b = reg.register("AGENT", "c3", session_id="session-B")
    assert entry_b["token"] == b_digest[:4], "a second collision must widen a second time"
    assert entry_b["token"] not in ("c3", three_char)


def test_an_unattributed_entry_is_claimable_as_a_refresh(home, monkeypatch):
    # An entry with no recorded session id -- e.g. one created before D-010,
    # or by a caller with no session_id and no CLAUDE_CODE_SESSION_ID either
    # -- must be claimable as a refresh rather than read as a permanent
    # foreign collision. `session_id=None` alone is not enough to simulate
    # "no session id available" here: register() falls back to the real
    # CLAUDE_CODE_SESSION_ID in THIS environment, so the env var must be
    # cleared too.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    reg.register("AGENT", "c3", session_id=None)
    session_id_file = os.path.join(reg.registry_dir("AGENT"), "c3", "session-id")
    assert not os.path.exists(session_id_file)
    entry = reg.register("AGENT", "c3", session_id="a-real-session")
    assert entry["token"] == "c3", "an unattributed entry must not force a widen"
