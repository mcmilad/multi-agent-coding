"""harness_admin.py records operator decisions and refuses to run for an agent."""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "harness_admin.py")
HOOKS = os.path.join(REPO, ".claude", "hooks")
AGENT_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CHILD_SESSION")
OPERATOR_TERMINAL = ["-zsh", "/usr/bin/login -fp operator", "/System/Applications/Terminal"]


def load():
    spec = importlib.util.spec_from_file_location("harness_admin_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def admin(tmp_path, monkeypatch):
    """The script as an operator's own terminal would run it.

    The suite itself usually runs inside an agent session, which the script
    detects from the process tree, so the operator's side is exercised
    in-process with the ancestry stated. There is deliberately no environment
    switch for this: one would be a switch an agent could flip too.
    """
    for name in AGENT_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_HARNESS_HUMAN_DIR", str(tmp_path / "human"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", REPO)
    module = load()
    monkeypatch.setattr(module, "_ancestors", lambda *a, **k: list(OPERATOR_TERMINAL))
    return module


def run(tmp_path, *args, strip=AGENT_VARS, extra=None):
    """The real script in a real subprocess, as an agent's shell would start it."""
    env = {k: v for k, v in os.environ.items() if k not in strip}
    env.update(HOME=str(tmp_path / "home"), CLAUDE_HARNESS_HUMAN_DIR=str(tmp_path / "human"),
               CLAUDE_PROJECT_DIR=REPO)
    env.update(extra or {})
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True,
                          env=env)


def test_it_refuses_every_command_inside_an_agent_session(tmp_path):
    for args in (("approve", "0123456789abcdef"), ("show",),
                 ("sandbox", "--account", "111122223333", "--regions", "ap-southeast-2")):
        proc = run(tmp_path, *args, extra={"CLAUDECODE": "1"})
        assert proc.returncode == 1, args
        assert "outside a Claude Code session" in proc.stderr
    assert not (tmp_path / "human").exists(), "a refused call must record nothing"


# --- an agent can unset the variables; it cannot change its ancestry (AG-18, N1) ---

@pytest.mark.parametrize("ancestors", [
    ["/bin/zsh -c python3 scripts/harness_admin.py show", "claude"],
    ["/bin/zsh", "/Users/x/.local/bin/claude --continue"],
    ["zsh", "/Users/x/.local/share/claude/versions/2.1.0"],
    ["sh", "node /opt/lib/node_modules/@anthropic-ai/claude-code/cli.js"],
    ["sh", "/usr/local/bin/claude-code"],
    ["bash", "env", "bash", "Claude"],
])
def test_a_claude_ancestor_is_an_agent_session_with_a_clean_environment(admin, ancestors):
    assert admin.in_agent_session(environ={}, ancestors=ancestors) is True


@pytest.mark.parametrize("ancestors", [
    OPERATOR_TERMINAL,
    ["-bash", "sshd: operator@pts/0", "/usr/sbin/sshd -D"],
    ["zsh", "tmux", "/Applications/iTerm.app/Contents/MacOS/iTerm2"],
    ["zsh", "vim notes-about-claude.md"],
])
def test_an_ordinary_terminal_is_not_an_agent_session(admin, ancestors):
    assert admin.in_agent_session(environ={}, ancestors=ancestors) is False


def test_a_process_with_no_ancestors_counts_as_an_agent_session(admin):
    """Reparented to init (setsid, a double fork) with the variables stripped:
    review cycle 3 found this branch answered "not an agent"."""
    assert admin.in_agent_session(environ={}, ancestors=[]) is True


def test_an_unreadable_process_table_counts_as_an_agent_session(admin, monkeypatch):
    monkeypatch.setattr(admin, "_ancestors", lambda *a, **k: None)
    assert admin.in_agent_session(environ={}) is True


def test_the_environment_markers_still_refuse_whatever_the_ancestry(admin):
    for name in AGENT_VARS:
        assert admin.in_agent_session(environ={name: "1"}, ancestors=OPERATOR_TERMINAL) is True


def test_ancestry_is_reported_unreadable_when_ps_cannot_run(admin, monkeypatch):
    module = load()

    def denied(*a, **k):
        raise PermissionError("operation not permitted: ps")
    monkeypatch.setattr(subprocess, "run", denied)
    assert module._ancestors(pid=500) is None


def test_ancestry_is_reported_unreadable_when_ps_fails_for_our_own_parent(monkeypatch):
    module = load()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 1, stdout="", stderr="ps: operation not permitted"))
    assert module._ancestors(pid=500) is None


def test_ancestry_walks_parent_links_to_the_top(monkeypatch):
    module = load()
    table = {"500": "400 /bin/zsh -c x", "400": "300 /Users/x/.local/bin/claude", "300": "1 -zsh"}

    def fake(argv, **k):
        out = table.get(argv[-1], "")
        return subprocess.CompletedProcess(argv, 0 if out else 1, stdout=out + "\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake)
    assert module._ancestors(pid=500) == ["/bin/zsh -c x", "/Users/x/.local/bin/claude", "-zsh"]
    assert module.in_agent_session(environ={}, ancestors=module._ancestors(pid=500)) is True


def _fake_ps(monkeypatch, table):
    seen = []

    def fake(argv, **k):
        seen.append(argv[0])
        out = table.get(argv[-1], "")
        return subprocess.CompletedProcess(argv, 0 if out else 1, stdout=out + "\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake)
    return seen


def test_a_chain_that_stops_short_of_the_top_is_unreadable(monkeypatch):
    """A sandbox that shows a process its own shell but not the shell's parent
    fails on the SECOND lookup. That used to return the one shell it had seen,
    which reads as an ordinary terminal (review cycle 4)."""
    module = load()
    _fake_ps(monkeypatch, {"500": "400 /bin/zsh -c x"})        # 400 cannot be read
    assert module._ancestors(pid=500) is None
    assert module.in_agent_session(environ={}, ancestors=module._ancestors(pid=500)) is True


def test_a_chain_deeper_than_the_limit_is_unreadable(monkeypatch):
    module = load()
    table = {str(n): "{} sh -c next".format(n - 1) for n in range(2, 200)}
    _fake_ps(monkeypatch, table)
    assert module._ancestors(pid=199, limit=40) is None
    assert module._ancestors(pid=30, limit=40) is not None


def test_an_unparsable_parent_id_is_unreadable(monkeypatch):
    module = load()
    _fake_ps(monkeypatch, {"500": "what -zsh"})
    assert module._ancestors(pid=500) is None


def test_ps_is_run_by_absolute_path_never_found_on_path(monkeypatch, tmp_path):
    """`PATH=/tmp/fake:$PATH` with a `ps` that prints an innocent parent."""
    module = load()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "ps").write_text("#!/bin/sh\necho '1 -zsh'\n")
    os.chmod(fake_bin / "ps", 0o755)
    monkeypatch.setenv("PATH", "{}{}{}".format(fake_bin, os.pathsep, os.environ["PATH"]))
    seen = _fake_ps(monkeypatch, {"500": "1 -zsh"})
    module._ancestors(pid=500)
    assert seen and all(os.path.isabs(p) and not p.startswith(str(fake_bin)) for p in seen)


def test_a_missing_ps_is_unreadable(monkeypatch):
    module = load()
    monkeypatch.setattr(module, "_PS_PATHS", ("/nonexistent/ps",))
    assert module._ancestors(pid=500) is None


def test_stripping_the_variables_does_not_get_an_agent_past_the_refusal(tmp_path):
    """End to end, with a process tree built here rather than borrowed.

    The script is started by a launcher whose command line is `.../claude ...`,
    with every marker variable stripped. The expected answer does not come from
    the code under test: there is a Claude ancestor, so it must refuse. (Inside
    a sandbox that denies `ps` it refuses for that reason instead; either way,
    take the ancestry check out and this goes green-to-red.)
    """
    launcher = tmp_path / "bin" / "claude"
    launcher.parent.mkdir()
    launcher.write_text('#!/bin/sh\n"$@"\nrc=$?\nexit $rc\n')
    os.chmod(launcher, 0o755)
    env = {k: v for k, v in os.environ.items() if k not in AGENT_VARS}
    env.update(HOME=str(tmp_path / "home"), CLAUDE_HARNESS_HUMAN_DIR=str(tmp_path / "human"),
               CLAUDE_PROJECT_DIR=REPO)
    proc = subprocess.run([str(launcher), sys.executable, SCRIPT, "approve", "0123456789abcdef"],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "outside a Claude Code session" in proc.stderr
    assert not (tmp_path / "human").exists(), "a refused call must record nothing"


# --- the operator's side ----------------------------------------------------

def test_the_operator_can_declare_a_sandbox_the_guard_then_reads(admin, tmp_path, monkeypatch):
    assert admin.main(["sandbox", "--account", "111122223333",
                       "--regions", "ap-southeast-2, us-east-1"]) == 0
    declared = json.load(open(tmp_path / "human" / "aws-sandbox.json"))
    assert declared == {"accountId": "111122223333", "regions": ["ap-southeast-2", "us-east-1"]}
    sys.path.insert(0, HOOKS)
    import aws_guard
    monkeypatch.delenv("CLAUDE_HARNESS_AWS_SANDBOX_PATH", raising=False)
    assert aws_guard.load_sandbox() == declared


def test_malformed_sandbox_arguments_are_rejected(admin, tmp_path):
    for account, regions in (("123", "ap-southeast-2"), ("111122223333", ""),
                             ("111122223333", "sydney")):
        assert admin.main(["sandbox", "--account", account, "--regions", regions]) == 2
    assert not (tmp_path / "human" / "aws-sandbox.json").exists()


def test_approve_show_and_revoke(admin, tmp_path, capsys):
    digest = "0123456789abcdef"
    assert admin.main(["approve", digest]) == 0
    assert (tmp_path / "human" / "approvals" / (digest + ".approved")).exists()
    capsys.readouterr()
    assert admin.main(["show"]) == 0
    assert digest in capsys.readouterr().out
    assert admin.main(["revoke", digest]) == 0
    assert not (tmp_path / "human" / "approvals" / (digest + ".approved")).exists()
    assert admin.main(["show"]) == 0
    assert "approvals: none" in capsys.readouterr().out


def test_an_id_that_is_not_a_guard_digest_is_rejected(admin):
    for bad in ("../../etc/passwd", "0123", "ZZZZZZZZZZZZZZZZ", "0123456789abcdef.approved"):
        assert admin.main(["approve", bad]) == 2, bad


def test_the_default_location_is_outside_the_repository(monkeypatch):
    sys.path.insert(0, HOOKS)
    import team_hook_common
    monkeypatch.delenv("CLAUDE_HARNESS_HUMAN_DIR", raising=False)
    path = os.path.realpath(team_hook_common.human_dir())
    assert not path.startswith(os.path.realpath(REPO) + os.sep)
    assert os.sep + os.path.join(".claude", "harness") + os.sep in path
