"""scripts/ci_local.py runs the CI steps on a fresh clone of a commit."""
import importlib.util
import io
import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "ci_local.py")


def load():
    spec = importlib.util.spec_from_file_location("ci_local_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci_local = load()


def git(repo, *args):
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "check.py").write_text("import sys\nsys.exit(0)\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def run(repo, tmp_path, steps, rev="HEAD"):
    out = io.StringIO()
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    code, failed = ci_local.run(str(repo), rev, steps, sys.executable, str(work), out)
    return code, failed, out.getvalue()


CHECK = [("check", ["{python}", "check.py"])]


def test_every_step_passing_exits_zero(repo, tmp_path):
    code, failed, out = run(repo, tmp_path, CHECK)
    assert (code, failed) == (0, None) and "[ok  ] check" in out


def test_it_tests_the_commit_not_the_working_tree(repo, tmp_path):
    """The reason this exists: the working tree says pass, the commit says fail."""
    (repo / "check.py").write_text("import sys\nsys.exit(3)\n")
    git(repo, "commit", "-q", "-am", "break it")
    (repo / "check.py").write_text("import sys\nsys.exit(0)\n")     # fixed, not committed
    assert subprocess.run([sys.executable, "check.py"], cwd=repo).returncode == 0
    code, failed, out = run(repo, tmp_path, CHECK)
    assert (code, failed) == (1, "check")
    assert "uncommitted change" in out and "check.py" in out


def test_a_file_that_was_never_committed_is_not_there(repo, tmp_path):
    (repo / "local_only.py").write_text("x = 1\n")
    steps = [("needs it", ["{python}", "-c", "import local_only"])]
    assert subprocess.run([sys.executable, "-c", "import local_only"], cwd=repo).returncode == 0
    assert run(repo, tmp_path, steps)[:2] == (1, "needs it")


def test_it_stops_at_the_first_failing_step(repo, tmp_path):
    marker = tmp_path / "second-ran"
    steps = [("first", ["{python}", "-c", "import sys; sys.exit(1)"]),
             ("second", ["{python}", "-c", "open({!r}, 'w').close()".format(str(marker))])]
    assert run(repo, tmp_path, steps)[:2] == (1, "first")
    assert not marker.exists()


def test_an_earlier_commit_can_be_named(repo, tmp_path):
    (repo / "check.py").write_text("import sys\nsys.exit(3)\n")
    git(repo, "commit", "-q", "-am", "break it")
    assert run(repo, tmp_path, CHECK)[0] == 1
    assert run(repo, tmp_path, CHECK, rev="HEAD~1")[0] == 0


def test_an_unknown_commit_is_a_usage_error_not_a_pass(repo, tmp_path):
    code, failed, out = run(repo, tmp_path, CHECK, rev="no-such-rev")
    assert code == 2 and "cannot resolve" in out


def test_the_clone_is_removed_and_the_repository_is_left_alone(repo, tmp_path):
    run(repo, tmp_path, CHECK)
    assert not (tmp_path / "work" / "src").exists()
    assert not (repo / ".git" / "worktrees").exists(), "it must not register a worktree"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True,
                            text=True).stdout
    assert status == ""


def test_the_clone_does_not_inherit_this_checkouts_identity(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/somewhere/else")
    steps = [("env", ["{python}", "-c",
                      "import os, sys; sys.exit(1 if 'CLAUDE_PROJECT_DIR' in os.environ else 0)"])]
    assert run(repo, tmp_path, steps)[0] == 0


def test_the_steps_are_the_ones_the_workflow_runs():
    """Add a check to ci.yml and not here (or the reverse) and this fails."""
    workflow = open(os.path.join(REPO, ".github", "workflows", "ci.yml")).read()
    in_ci = [line.strip() for line in re.findall(r"^\s*run:\s*(.+)$", workflow, re.M)]
    in_ci = [line for line in in_ci if "pip install" not in line]
    here = [" ".join("python" if a == "{python}" else a for a in argv)
            for _name, argv in ci_local.STEPS]
    assert here == in_ci
    names = re.findall(r"^\s*- name:\s*(.+)$", workflow, re.M)
    assert [name for name, _ in ci_local.STEPS] == [n for n in names if "Install" not in n]


def test_started_with_a_bare_interpreter_it_still_uses_the_projects_virtualenv(repo, tmp_path):
    """The documented command is `python3 scripts/ci_local.py`, and `python3`
    on PATH has no pytest. The steps must run under the checkout's .venv."""
    assert ci_local.default_python(str(repo)) == sys.executable      # no .venv: fall back
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    marker = tmp_path / "venv-python-ran"
    fake = venv_bin / "python"
    fake.write_text("#!/bin/sh\ntouch '{}'\nexec '{}' \"$@\"\n".format(marker, sys.executable))
    os.chmod(fake, 0o755)
    assert ci_local.default_python(str(repo)) == str(fake)
    out = io.StringIO()
    work = tmp_path / "work2"
    work.mkdir()
    code, _failed = ci_local.run(str(repo), "HEAD", CHECK, None, str(work), out)
    assert code == 0 and marker.exists(), out.getvalue()
    assert str(fake) in out.getvalue(), "the run says which interpreter it used"


def test_the_documented_command_works_from_a_bare_python3():
    """End to end on this repository: started by whatever `python3` is, the
    secret-scan step must run rather than die on a missing pytest."""
    import shutil
    bare = shutil.which("python3")
    if bare is None or not os.path.exists(os.path.join(REPO, ".venv", "bin", "python")):
        pytest.skip("needs python3 on PATH and the project virtualenv")
    code = ("import sys; sys.path.insert(0, {!r}); import ci_local; "
            "c, f = ci_local.run({!r}, 'HEAD', ci_local.STEPS[:1]); sys.exit(c)").format(
                os.path.join(REPO, "scripts"), REPO)
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    proc = subprocess.run([bare, "-c", code], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stdout[-600:] + proc.stderr[-600:]


def test_looking_at_the_working_tree_takes_no_git_lock(repo, tmp_path):
    index = repo / ".git" / "index"
    before = index.stat().st_mtime_ns
    (repo / "check.py").write_text("import sys\nsys.exit(0)\n# touched\n")
    assert ci_local.dirty(str(repo)) == ["check.py"]
    assert index.stat().st_mtime_ns == before
