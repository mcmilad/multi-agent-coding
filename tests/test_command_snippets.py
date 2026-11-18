"""Executes the commit-mutex shell embedded in commands/ship-it.md against a
temp $HOME, so a regression in it is caught by a test run rather than by a
reviewer running it by hand.

Each property below is checked against a single-variable buggy variant of the
REAL current snippet (never a hand-typed independent string), so a guard that
has gone inert fails here: property 1 must go red against an unguarded trap,
property 2 against `rmdir` on a lock directory that holds owner/heartbeat.

stdlib + pytest only. Extracted shell runs with a stubbed `sleep` (so the
60-iteration retry loop completes in well under a second instead of up to
120s) and a stubbed `git` (so the snippet's literal `git add` / `git commit`
calls never touch a real repository -- the properties under test are about
the LOCKS directory, not about what gets committed). Every subprocess runs
with HOME and CWD pointed at a pytest tmp_path; nothing is left under the
real $HOME.
"""
import os
import re
import stat
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIP = os.path.join(ROOT, "commands", "ship-it.md")

_TRAP_LINE = 'trap \'[ "$HELD" = 1 ] && rm -rf "$LOCKS/git-commit"\' EXIT'


def _read(path):
    with open(path) as fh:
        return fh.read()


def _bash_blocks(text):
    return re.findall(r"```bash\n(.*?)```", text, re.S)


def _commit_mutex_block(text):
    """The one fenced bash block that is the commit mutex, identified by
    content rather than position -- ship-it.md has several bash blocks."""
    candidates = [b for b in _bash_blocks(text) if "git-commit" in b and "trap" in b]
    assert len(candidates) == 1, \
        "expected exactly one commit-mutex bash block, found %d" % len(candidates)
    return candidates[0]


def _unguarded_trap_variant(snippet):
    """The real snippet with ONLY the HELD guard removed from the trap --
    isolates the cycle-1 Critical (an unconditional trap releasing a lock
    this session never acquired) from the separate rmdir/rm-rf bug."""
    unguarded = snippet.replace(
        _TRAP_LINE, 'trap \'rm -rf "$LOCKS/git-commit"\' EXIT')
    assert unguarded != snippet, "trap line not found -- snippet shape changed"
    return unguarded


def _rmdir_release_variant(snippet):
    """The real snippet with ONLY rm -rf swapped for rmdir in the
    (still HELD-guarded) trap -- isolates the rmdir-cannot-remove-a-
    non-empty-directory bug from the trap-guard bug."""
    swapped = snippet.replace(
        _TRAP_LINE,
        'trap \'[ "$HELD" = 1 ] && rmdir "$LOCKS/git-commit" 2>/dev/null\' EXIT')
    assert swapped != snippet, "trap line not found -- snippet shape changed"
    return swapped


def _stub_bin(tmp_path):
    """A directory placed ahead of the real PATH, with no-op `sleep` and
    `git` stand-ins. `sleep` turns the retry loop's up-to-120s worst case
    into well under a second; `git` guarantees the snippet's literal
    `git add` / `git commit` calls never touch a real repository, regardless
    of what CWD happens to be."""
    import shutil
    real_git = shutil.which("git")
    bindir = tmp_path / "stubbin"
    bindir.mkdir(exist_ok=True)
    scripts = {
        "sleep": "#!/bin/sh\nexit 0\n",
        # Only `git rev-parse` reaches the real git; everything else is a no-op.
        "git": ("#!/bin/sh\nif [ \"$1\" = rev-parse ]; then exec '{}' \"$@\"; fi\n"
                "exit 0\n").format(real_git),
    }
    for name, body in scripts.items():
        stub = bindir / name
        stub.write_text(body)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(bindir)


def _run_snippet(snippet, home, key="AGENT", instance="test-instance"):
    """Execute a commit-mutex snippet with its documented placeholders
    substituted the way an operator/agent copying it would, against a temp
    $HOME. Returns the completed subprocess (stdout/stderr captured, never
    propagated to the real terminal)."""
    script = (snippet.replace("<PROJECT>", key).replace("<KEY>", key)
                      .replace("<instance>", instance)
                      .replace("<only this issue's Files:>", "."))
    env = dict(os.environ)
    env["HOME"] = str(home)
    # The snippet resolves its state directory with `git rev-parse`, so it needs
    # a real repository and the real git for that one call; `git add`/`commit`
    # stay stubbed by a wrapper that passes only rev-parse through.
    if not (home / ".git" / "HEAD").exists():
        subprocess.run(["git", "init", "-q"], cwd=str(home), check=True, capture_output=True)
    env["PATH"] = _stub_bin(home) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        ["bash", "-c", script], cwd=str(home), env=env,
        capture_output=True, text=True, timeout=30,
    )


def _lock_dir(home, key="AGENT"):
    return home / ".git" / "claude-harness" / "locks" / key / "git-commit"


# --- Property 1: a non-acquirer's exit must not touch a peer's lock ---

def test_a_non_acquirer_leaves_a_peers_lock_and_owner_intact(tmp_path):
    lock = _lock_dir(tmp_path)
    lock.mkdir(parents=True)
    (lock / "owner").write_text("peer-owner")
    (lock / "heartbeat").write_text("2026-01-01T00:00:00Z")

    real_snippet = _commit_mutex_block(_read(SHIP))
    _run_snippet(real_snippet, tmp_path)

    assert lock.exists(), "a session that never acquired the lock must not remove it"
    assert (lock / "owner").read_text() == "peer-owner", \
        "the peer's owner file must survive a non-acquirer's exit"


def test_the_unguarded_trap_variant_destroys_a_peers_lock(tmp_path):
    """Proves property 1's guard is not inert: the same scenario, but with
    only the HELD check removed from the trap, must fail this way -- this is
    the cycle-1 Critical, reproduced against a single-variable variant of
    the real snippet rather than asserted from memory."""
    lock = _lock_dir(tmp_path)
    lock.mkdir(parents=True)
    (lock / "owner").write_text("peer-owner")
    (lock / "heartbeat").write_text("2026-01-01T00:00:00Z")

    buggy = _unguarded_trap_variant(_commit_mutex_block(_read(SHIP)))
    _run_snippet(buggy, tmp_path)

    assert not lock.exists(), \
        "an unconditional trap must destroy a peer's lock here -- if this " \
        "assertion fails, the buggy variant no longer reproduces the Critical"


# --- Property 2: the holder must release a lock dir it populated itself ---

def test_the_holder_releases_a_nonempty_lock_dir_cleanly(tmp_path):
    real_snippet = _commit_mutex_block(_read(SHIP))
    lock = _lock_dir(tmp_path)

    _run_snippet(real_snippet, tmp_path)

    assert not lock.exists(), \
        "the holder must remove its own lock dir on exit, even though " \
        "acquiring it made the dir non-empty (owner + heartbeat)"


def test_the_rmdir_variant_leaves_a_nonempty_lock_dir_behind(tmp_path):
    """Proves property 2's guard is not inert: swapping only rm -rf for
    rmdir in the (still correctly HELD-guarded) trap must leave the lock dir
    behind, because rmdir cannot remove a directory holding owner/heartbeat."""
    buggy = _rmdir_release_variant(_commit_mutex_block(_read(SHIP)))
    lock = _lock_dir(tmp_path)

    _run_snippet(buggy, tmp_path)

    assert lock.exists(), \
        "rmdir must fail silently on the non-empty lock dir here -- if " \
        "this assertion fails, the buggy variant no longer reproduces the bug"
