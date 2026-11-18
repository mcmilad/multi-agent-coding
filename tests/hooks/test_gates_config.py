"""The gate configuration: what a hook may run, and what only the agent's shell may.

Review cycle 2 (R1): the verification gate ran the test suite itself, with the
working tree's own interpreter, outside the sandbox. Every transition was an
unsandboxed code-execution path for whoever could write a conftest.py. Hook-run
gates are now static tools resolved outside the tree; the test suite is an agent
check, run in the sandboxed shell and recorded in the evidence.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, ".claude", "hooks"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence  # noqa: E402
from evidence_helpers import make_tool  # noqa: E402

GATES = os.path.join(REPO, ".claude", "gates.json")


# --- the repository's own configuration -------------------------------------

def _file(monkeypatch, tmp_path, content):
    path = tmp_path / "gates.json"
    if content is None:
        monkeypatch.setenv(evidence.GATES_PATH_ENV, str(tmp_path / "missing.json"))
        return
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    monkeypatch.setenv(evidence.GATES_PATH_ENV, str(path))


def test_the_repository_checks_are_lint_and_tests_run_by_the_agent(monkeypatch):
    monkeypatch.delenv(evidence.GATES_PATH_ENV, raising=False)
    cfg = json.load(open(GATES))
    assert not cfg.get("gates"), "a hook runs no project tool; checks go in agentChecks"
    assert {c["name"] for c in cfg["agentChecks"]} >= {"lint", "tests"}
    checks, problem = evidence.load_agent_checks(REPO)
    assert problem is None and [c["name"] for c in checks] == ["lint", "tests"]


def test_a_gates_file_that_still_lists_hook_run_gates_is_a_reported_problem(
        monkeypatch, tmp_path):
    """Silently ignoring the list would look like the lint still being enforced."""
    _file(monkeypatch, tmp_path, {"gates": [{"name": "lint", "command": ["ruff", "check", "."]}],
                                  "agentChecks": []})
    checks, problem = evidence.load_agent_checks(str(tmp_path))
    assert checks == [] and "no longer exist" in problem and "agentChecks" in problem
    _file(monkeypatch, tmp_path, {"gates": [], "agentChecks": []})
    assert evidence.load_agent_checks(str(tmp_path)) == ([], None)


# Every program hook-side code starts, by file. A new entry here is a decision,
# not a detail: each is unsandboxed. design.md ("Verification gate") lists the same.
HOOK_SPAWNS = {
    "evidence.py": 2,        # git ls-files, git rev-parse
    "aws_guard.py": 1,       # aws sts get-caller-identity
    "selfcheck.py": 2,       # each hook once on empty input; `claude` to list plugins
}


def test_hook_side_code_starts_only_the_programs_it_is_known_to():
    import re
    hooks = os.path.join(REPO, ".claude", "hooks")
    found = {}
    for name in sorted(os.listdir(hooks)):
        if not name.endswith(".py"):
            continue
        source = open(os.path.join(hooks, name)).read()
        assert not re.search(r"os\.(system|popen|exec[lv]p?e?|spawn\w+)\(", source), name
        calls = len(re.findall(r"subprocess\.(?:run|Popen|call|check_call|check_output)\(", source))
        if calls:
            found[name] = calls
    assert found == HOOK_SPAWNS, found
    for removed in ("run_gates", "load_gates", "gate_program_problem",
                    "gate_isolation_problem", "ATTESTATION_KEY"):
        assert not hasattr(evidence, removed), removed
    source = open(os.path.join(hooks, "evidence.py")).read()
    starts = re.findall(r"subprocess\.run\(\s*([^,\n]+)", source)
    assert all(s.strip().startswith("GIT +") for s in starts), starts
    guard = open(os.path.join(hooks, "aws_guard.py")).read()
    assert '"sts", "get-caller-identity"' in guard
    security = open(os.path.join(REPO, "SECURITY.md"), encoding="utf-8").read()
    for program in ("`git`", "`aws sts", "`claude` CLI"):
        assert program in security, "SECURITY.md must list {} as started by a hook".format(program)


def test_a_missing_gates_file_means_no_checks(monkeypatch, tmp_path):
    _file(monkeypatch, tmp_path, None)
    assert evidence.load_agent_checks(REPO) == ([], None)


def test_a_malformed_gates_file_is_a_reported_problem(monkeypatch, tmp_path):
    for content in ("{not json", "[]", '"text"'):
        _file(monkeypatch, tmp_path, content)
        checks, problem = evidence.load_agent_checks(REPO)
        assert checks == [] and problem, content


def test_malformed_agent_checks_are_a_reported_problem(monkeypatch, tmp_path):
    _file(monkeypatch, tmp_path, {"gates": [], "agentChecks": [{"name": "t", "command": "x"}]})
    checks, problem = evidence.load_agent_checks(REPO)
    assert checks == [] and "agentChecks" in problem


# --- agent checks -----------------------------------------------------------

def test_agent_checks_may_use_the_python_token(monkeypatch, tmp_path):
    _file(monkeypatch, tmp_path, {"gates": [], "agentChecks": [
        {"name": "tests", "command": ["{python}", "-m", "pytest", "-q"]}]})
    checks, problem = evidence.load_agent_checks(REPO)
    assert problem is None and checks[0]["name"] == "tests"
    argv = evidence.resolve_command(checks[0]["command"], str(tmp_path), str(tmp_path))
    assert os.path.isabs(argv[0]) and argv[1:] == ["-m", "pytest", "-q"]


def test_the_python_token_prefers_the_working_copys_virtualenv_then_the_projects(tmp_path):
    work, project = tmp_path / "wt", tmp_path / "main"
    for root in (work, project):
        (root / ".venv" / "bin").mkdir(parents=True)
        (root / ".venv" / "bin" / "python").write_text("")
    assert evidence.resolve_python(str(work), str(project)).startswith(str(work))
    os.remove(work / ".venv" / "bin" / "python")
    assert evidence.resolve_python(str(work), str(project)).startswith(str(project))
    assert ".venv" not in evidence.resolve_python(str(tmp_path / "none"), str(tmp_path / "none"))


# --- evidence must record the agent checks ----------------------------------

def _record(**checks):
    return evidence.make_record("AG-1", "run", "true", 0, "t" * 64, 1.0, 2.0, checks=checks)


def test_evidence_without_a_required_check_is_refused():
    ok, why = evidence.validate(_record(), "AG-1", "true", "t" * 64, required_checks=["tests"])
    assert not ok and "'tests'" in why and "not run" in why


def test_evidence_with_a_failing_check_is_refused():
    ok, why = evidence.validate(_record(tests=1), "AG-1", "true", "t" * 64,
                                required_checks=["tests"])
    assert not ok and "exit 1" in why


def test_evidence_with_every_required_check_passing_is_accepted():
    assert evidence.validate(_record(tests=0, types=0), "AG-1", "true", "t" * 64,
                             required_checks=["tests", "types"])[0]


def test_a_boolean_or_missing_checks_field_does_not_count_as_passing():
    record = _record()
    record["checks"] = {"tests": False}
    assert not evidence.validate(record, "AG-1", "true", "t" * 64, required_checks=["tests"])[0]
    record["checks"] = "tests=0"
    assert not evidence.validate(record, "AG-1", "true", "t" * 64, required_checks=["tests"])[0]


def test_with_no_required_checks_the_field_is_irrelevant():
    assert evidence.validate(_record(), "AG-1", "true", "t" * 64)[0]


# --- review cycle 3: a vetted program still obeys the tree's configuration ----
#
# `ruff` is static and installed outside the tree, and `ruff check .` still took
# `fix = true` and an isort `required-imports` line from a worker-written
# ruff.toml: the gate's own run rewrote the hook files from outside the sandbox.


SAFE = ["ruff", "check", "--isolated", "--no-fix", "--no-cache", "--no-respect-gitignore",
        "--ignore-noqa", "--exclude", "./.venv"]


# Written out, not read from the module: dropping one from the code must fail here.


def _snapshot(root):
    out = {}
    for folder, _dirs, names in os.walk(root):
        if os.sep + ".git" in folder:
            continue
        for name in names:
            path = os.path.join(folder, name)
            out[os.path.relpath(path, root)] = open(path, "rb").read()
    return out


# --- review cycle 3: git must not run what the tree's git configuration names ---

def test_every_git_call_in_the_hook_disables_configured_programs():
    assert evidence.GIT[0] == "git" and "core.fsmonitor=false" in evidence.GIT
    source = open(os.path.join(REPO, ".claude", "hooks", "evidence.py")).read()
    import re
    bare = re.findall(r'\[\s*"git"\s*,\s*"(?!-c")', source)
    assert not bare, "a git call in evidence.py bypasses the GIT prefix"
    assert evidence.git_env()["GIT_CONFIG_NOSYSTEM"] == "1"
    assert evidence.git_env()["GIT_NO_LAZY_FETCH"] == "1"
    assert "protocol.allow=never" in evidence.GIT


def test_hashing_the_tree_does_not_run_a_configured_fsmonitor(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    marker = tmp_path / "fsmonitor-ran"
    hook = make_tool(tmp_path / "evil", "fsmonitor", "touch '{}'".format(marker))
    for argv in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                  "commit", "-q", "-m", "init"],
                 ["git", "config", "core.fsmonitor", hook]):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    assert evidence.tree_hash(str(repo), ["a.py"]) is not None
    assert not marker.exists(), "the hook ran a program named by the repository's git config"
    # Contrast: the same listing without the override does run it.
    subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=repo,
                   capture_output=True)
    if not marker.exists():
        pytest.skip("this git does not run core.fsmonitor for ls-files; nothing to contrast")


# --- review cycle 4 ----------------------------------------------------------


def _git_repo(tmp_path, files):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    for argv in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                  "commit", "-q", "-m", "init"]):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    return repo


def test_hashing_the_tree_does_not_run_a_promisor_remotes_uploadpack(tmp_path):
    """A partial-clone marker plus a missing object makes `ls-files` fetch, and the
    fetch runs `remote.<name>.uploadpack` through a shell. From the gate that was
    an unsandboxed command of the worker's choosing (it wrote an AWS approval)."""
    import subprocess
    repo = _git_repo(tmp_path, {"a.py": "x = 1\n", "sub/deep/b.py": "y = 1\n"})
    marker = tmp_path / "uploadpack-ran"

    def git(*args, check=True):
        return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True,
                              text=True)
    git("sparse-checkout", "init", "--cone", "--sparse-index")
    git("sparse-checkout", "set", "nothing")
    tree = git("rev-parse", "HEAD:sub").stdout.strip()
    os.remove(repo / ".git" / "objects" / tree[:2] / tree[2:])
    git("config", "extensions.partialClone", "origin")
    git("config", "remote.origin.url", str(repo))
    git("config", "remote.origin.promisor", "true")
    git("config", "remote.origin.uploadpack", "touch '{}'; git-upload-pack".format(marker))

    evidence.tree_hash(str(repo), ["."])     # None or a hash; either way, nothing ran
    assert not marker.exists(), "the hook's git call ran the repository's uploadpack"

    # Each defence holds alone.
    for argv, env in ((evidence.GIT, dict(os.environ)),
                      (["git"], evidence.git_env())):
        subprocess.run(argv + ["-C", str(repo), "ls-files", "-z", "--cached", "--others",
                               "--exclude-standard", "--", "."], capture_output=True, env=env)
        assert not marker.exists(), argv
    # Contrast: a plain call does run it.
    subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                    "--", "."], cwd=repo, capture_output=True)
    if not marker.exists():
        pytest.skip("this git does not lazy-fetch for ls-files; nothing to contrast")


def test_a_symlink_under_the_issues_files_is_hashed_as_a_link_not_followed(tmp_path):
    repo = _git_repo(tmp_path, {"a.py": "x = 1\n"})
    outside = tmp_path / "outside.txt"
    outside.write_text("one\n")
    os.symlink(outside, repo / "link.py")
    first = evidence.tree_hash(str(repo), ["."])
    assert first is not None
    outside.write_text("two\n")
    assert evidence.tree_hash(str(repo), ["."]) == first, "the gate read through the link"
    os.remove(repo / "link.py")
    os.symlink(tmp_path / "elsewhere", repo / "link.py")
    assert evidence.tree_hash(str(repo), ["."]) != first, "retargeting a link must change the hash"


@pytest.mark.parametrize("kind", ["fifo", "zero"])
def test_hashing_cannot_be_stalled_by_what_a_link_points_at(tmp_path, kind):
    """A FIFO blocked `open()` for ever; /dev/zero never reached end of file."""
    import subprocess
    repo = _git_repo(tmp_path, {"a.py": "x = 1\n"})
    if kind == "fifo":
        target = tmp_path / "pipe"
        os.mkfifo(target)
    else:
        target = "/dev/zero"
    os.symlink(target, repo / "stall.py")
    code = ("import sys; sys.path.insert(0, {!r}); import evidence; "
            "print(evidence.tree_hash({!r}, ['.']))".format(
                os.path.join(REPO, ".claude", "hooks"), str(repo)))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and len(proc.stdout.strip()) == 64, proc.stderr


def test_a_fifo_in_the_tree_itself_is_not_opened(tmp_path):
    import subprocess
    repo = _git_repo(tmp_path, {"a.py": "x = 1\n"})
    os.mkfifo(repo / "pipe.py")
    code = ("import sys; sys.path.insert(0, {!r}); import evidence; "
            "print(evidence.tree_hash({!r}, ['.']))".format(
                os.path.join(REPO, ".claude", "hooks"), str(repo)))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and len(proc.stdout.strip()) == 64, proc.stderr


def test_a_tree_larger_than_the_hash_budget_cannot_be_verified(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, {"a.py": "x = 1\n", "big.bin": "0" * 4096})
    assert evidence.tree_hash(str(repo), ["."]) is not None
    monkeypatch.setattr(evidence, "TREE_HASH_MAX_BYTES", 1024)
    assert evidence.tree_hash(str(repo), ["."]) is None, "None is 'cannot verify', never a match"


def test_an_evidence_record_is_only_ever_a_small_regular_file(tmp_path, monkeypatch):
    import subprocess
    good = tmp_path / "ok.verified"
    evidence.write_record(str(good), {"schema": 1})
    assert evidence.load_record(str(good)) == {"schema": 1}
    link = tmp_path / "link.verified"
    os.symlink(good, link)
    assert evidence.load_record(str(link)) is None, "a symlink is not a record"
    monkeypatch.setattr(evidence, "RECORD_MAX_BYTES", 4)
    assert evidence.read_regular_file(str(good), 4) is None
    pipe = tmp_path / "pipe.verified"
    os.mkfifo(pipe)
    code = ("import sys; sys.path.insert(0, {!r}); import evidence; "
            "print(evidence.load_record({!r}))".format(
                os.path.join(REPO, ".claude", "hooks"), str(pipe)))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert proc.stdout.strip() == "None", proc.stderr


# --- review cycle 5 ----------------------------------------------------------


def test_a_protocol_allowed_in_repository_config_does_not_reopen_the_transport(tmp_path):
    """`-c protocol.allow=never` loses to `protocol.file.allow=always` in the
    repository's config; the environment variable does not."""
    assert evidence.git_env()["GIT_ALLOW_PROTOCOL"] == ""
    import subprocess
    repo = _git_repo(tmp_path, {"a.py": "x = 1\n", "sub/deep/b.py": "y = 1\n"})
    marker = tmp_path / "uploadpack-ran"

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                              text=True)
    git("sparse-checkout", "init", "--cone", "--sparse-index")
    git("sparse-checkout", "set", "nothing")
    tree = git("rev-parse", "HEAD:sub").stdout.strip()
    os.remove(repo / ".git" / "objects" / tree[:2] / tree[2:])
    git("config", "extensions.partialClone", "origin")
    git("config", "remote.origin.url", str(repo))
    git("config", "remote.origin.promisor", "true")
    git("config", "protocol.file.allow", "always")
    git("config", "remote.origin.uploadpack", "touch '{}'; git-upload-pack".format(marker))
    env = evidence.git_env()
    env.pop("GIT_NO_LAZY_FETCH")          # as an older git would behave
    subprocess.run(evidence.GIT + ["-C", str(repo), "ls-files", "-z", "--cached", "--others",
                                   "--exclude-standard", "--", "."], capture_output=True, env=env)
    assert not marker.exists()
    env.pop("GIT_ALLOW_PROTOCOL")
    subprocess.run(evidence.GIT + ["-C", str(repo), "ls-files", "-z", "--cached", "--others",
                                   "--exclude-standard", "--", "."], capture_output=True, env=env)
    if not marker.exists():
        pytest.skip("this git does not lazy-fetch here; nothing to contrast")


def test_the_shipped_cadence_lints_every_run_and_tests_on_review(monkeypatch):
    monkeypatch.delenv(evidence.GATES_PATH_ENV, raising=False)
    checks, _ = evidence.load_agent_checks(REPO)
    when = {c["name"]: c["when"] for c in checks}
    assert when == {"lint": "always", "tests": "review"}
    assert [c["name"] for c in evidence.checks_for(checks, "run")] == ["lint"]
    assert [c["name"] for c in evidence.checks_for(checks, "review")] == ["lint", "tests"]


def test_the_shipped_full_suite_runs_in_parallel(monkeypatch):
    monkeypatch.delenv(evidence.GATES_PATH_ENV, raising=False)
    checks, _ = evidence.load_agent_checks(REPO)
    (tests,) = [c for c in checks if c["name"] == "tests"]
    assert tests["command"][-2:] == ["-n", "auto"]
