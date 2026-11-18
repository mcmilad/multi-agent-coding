"""scripts/verify_issue.py runs an issue's Run: command and records evidence."""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "verify_issue.py")
GATE = os.path.join(REPO, ".claude", "hooks", "jira_transition_verify_gate.py")
TRANSITION = "mcp__plugin_atlassian_atlassian__transitionJiraIssue"
CONFIG = {"projectKey": "AG", "gatedStatuses": ["In Review", "Done"],
          "transitions": {"21": "In Progress", "6": "In Review", "31": "Done"}}


def make_repo(tmp_path):
    """A real git repository with one tracked file the issue owns."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("VALUE = 1\n")
    (repo / "other.txt").write_text("unrelated\n")
    for argv in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                  "commit", "-q", "-m", "init"]):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    return repo


def env_for(tmp_path, repo):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps(CONFIG))
    return dict(os.environ, HOME=str(tmp_path / "home"), JIRA_CONFIG_PATH=str(cfg),
                CLAUDE_PROJECT_DIR=str(repo))


def know(tmp_path, key="AG-8", run="true", files=("src/a.py",)):
    d = tmp_path / "home" / ".claude" / "logs" / "jira-mirror"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "AG.jsonl", "a") as fh:
        fh.write(json.dumps({"op": "sync", "key": key, "status": "In Progress",
                             "labels": ["role-coding"], "files": list(files),
                             "run": run}) + "\n")


def verify(tmp_path, repo, *args):
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=repo, capture_output=True,
                          text=True, env=env_for(tmp_path, repo))


def gate(tmp_path, repo, key="AG-8", tid="6", agent=None):
    payload = {"tool_name": TRANSITION, "cwd": str(repo),
               "tool_input": {"issueIdOrKey": key, "transitionId": tid}}
    if agent:
        payload.update(agent_id="a" + agent + "-00", agent_type=agent)
    return subprocess.run([sys.executable, GATE], input=json.dumps(payload),
                          capture_output=True, text=True, env=env_for(tmp_path, repo))


def record_path(tmp_path, key="AG-8"):
    return tmp_path / "home" / ".claude" / "logs" / "verified" / "AG" / (key + ".verified")


def test_a_passing_run_writes_evidence_the_gate_accepts(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, run="test -f src/a.py")
    proc = verify(tmp_path, repo, "AG-8")
    assert proc.returncode == 0, proc.stderr
    record = json.load(open(record_path(tmp_path)))
    assert record["exit_code"] == 0 and record["kind"] == "run"
    assert record["command"] == "test -f src/a.py"
    assert gate(tmp_path, repo).returncode == 0


def test_a_failing_run_writes_no_evidence_and_removes_an_old_record(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, run="true")
    assert verify(tmp_path, repo, "AG-8").returncode == 0
    know(tmp_path, run="echo nope; exit 7")
    proc = verify(tmp_path, repo, "AG-8")
    assert proc.returncode == 1
    assert "nope" in proc.stdout and "exit 7" in proc.stderr
    assert not record_path(tmp_path).exists()
    assert gate(tmp_path, repo).returncode == 2


def test_editing_an_owned_file_after_verification_makes_the_evidence_stale(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path)
    assert verify(tmp_path, repo, "AG-8").returncode == 0
    (repo / "src" / "a.py").write_text("VALUE = 2\n")
    proc = gate(tmp_path, repo)
    assert proc.returncode == 2 and "stale" in proc.stderr


def test_editing_a_file_the_issue_does_not_own_leaves_the_evidence_valid(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path)
    assert verify(tmp_path, repo, "AG-8").returncode == 0
    (repo / "other.txt").write_text("a peer changed this\n")
    assert gate(tmp_path, repo).returncode == 0


def test_a_new_untracked_file_in_an_owned_directory_makes_the_evidence_stale(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, files=("src",))
    assert verify(tmp_path, repo, "AG-8").returncode == 0
    (repo / "src" / "b.py").write_text("NEW = 1\n")
    assert gate(tmp_path, repo).returncode == 2


def test_deleting_an_owned_file_makes_the_evidence_stale(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path)
    assert verify(tmp_path, repo, "AG-8").returncode == 0
    os.remove(repo / "src" / "a.py")
    assert gate(tmp_path, repo).returncode == 2


def test_a_glob_entry_binds_the_files_it_matches(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, files=("src/*.py",))
    assert verify(tmp_path, repo, "AG-8").returncode == 0
    assert gate(tmp_path, repo).returncode == 0
    (repo / "src" / "a.py").write_text("VALUE = 3\n")
    assert verify(tmp_path, repo, "AG-8").returncode == 0   # consumed above; verify again
    (repo / "src" / "a.py").write_text("VALUE = 4\n")
    assert gate(tmp_path, repo).returncode == 2


def test_a_hand_written_record_with_the_wrong_hash_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path)
    path = record_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": 1, "issue": "AG-8", "kind": "run", "command": "true",
                                "exit_code": 0, "tree": "f" * 64}))
    assert gate(tmp_path, repo).returncode == 2


def test_review_evidence_is_needed_to_close_and_comes_from_the_review_flag(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path)
    assert verify(tmp_path, repo, "AG-8").returncode == 0
    assert gate(tmp_path, repo, tid="31", agent="review-1").returncode == 2
    assert verify(tmp_path, repo, "AG-8", "--review").returncode == 0
    assert json.load(open(record_path(tmp_path)))["kind"] == "review"
    assert gate(tmp_path, repo, tid="31", agent="review-1").returncode == 0


def test_an_unknown_issue_and_a_missing_run_have_distinct_exit_codes(tmp_path):
    repo = make_repo(tmp_path)
    proc = verify(tmp_path, repo, "AG-8")
    assert proc.returncode == 2 and "getJiraIssue" in proc.stderr
    know(tmp_path, run="")
    proc = verify(tmp_path, repo, "AG-8")
    assert proc.returncode == 3 and "Run:" in proc.stderr
    assert verify(tmp_path, repo, "OTHER-1").returncode == 2


def test_a_command_that_overruns_is_abandoned_without_evidence(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, run="sleep 5")
    proc = verify(tmp_path, repo, "AG-8", "--timeout", "1")
    assert proc.returncode == 4
    assert not record_path(tmp_path).exists()


def test_a_lowercase_key_is_accepted(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path)
    assert verify(tmp_path, repo, "ag-8").returncode == 0
    assert record_path(tmp_path).exists()


def test_outside_a_git_repository_no_evidence_is_written(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    know(tmp_path)
    proc = verify(tmp_path, plain, "AG-8")
    if proc.returncode == 0:      # the temp directory sits inside some repository
        import pytest
        pytest.skip("temporary directory is inside a git repository")
    assert proc.returncode == 5 and not record_path(tmp_path).exists()


# --- project checks run here, in the agent's shell, not in the hook (AG-18, R1) ---

def _with_checks(tmp_path, repo, checks, gates=()):
    path = tmp_path / "gates.json"
    path.write_text(json.dumps({"gates": list(gates), "agentChecks": checks}))
    return dict(env_for(tmp_path, repo), CLAUDE_HARNESS_GATES_PATH=str(path))


def _verify(env, repo, *args):
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=repo, capture_output=True,
                          text=True, env=env)


def _gate(env, repo, key="AG-8", tid="6"):
    payload = {"tool_name": TRANSITION, "cwd": str(repo),
               "tool_input": {"issueIdOrKey": key, "transitionId": tid}}
    return subprocess.run([sys.executable, GATE], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def test_a_project_check_is_run_and_its_result_recorded(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, run="true")
    marker = tmp_path / "suite-ran"
    env = _with_checks(tmp_path, repo, [
        {"name": "tests", "command": ["bash", "-c", "touch '{}'".format(marker)]}])
    proc = _verify(env, repo, "AG-8")
    assert proc.returncode == 0, proc.stderr
    assert marker.exists(), "the project check must really have been executed"
    assert "project check 'tests'" in proc.stdout
    assert json.load(open(record_path(tmp_path)))["checks"] == {"tests": 0}
    assert _gate(env, repo).returncode == 0


def test_a_failing_project_check_leaves_no_evidence(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, run="true")
    assert verify(tmp_path, repo, "AG-8").returncode == 0     # an older, passing record
    assert record_path(tmp_path).exists()
    env = _with_checks(tmp_path, repo, [
        {"name": "tests", "command": ["bash", "-c", "echo '1 failed'; exit 1"]}])
    proc = _verify(env, repo, "AG-8")
    assert proc.returncode == 1
    assert "project check 'tests' exited 1" in proc.stderr and "1 failed" in proc.stdout
    assert not record_path(tmp_path).exists()
    assert _gate(env, repo).returncode == 2


def test_the_project_check_is_not_run_when_the_issues_own_command_fails(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, run="false")
    marker = tmp_path / "suite-ran"
    env = _with_checks(tmp_path, repo, [
        {"name": "tests", "command": ["bash", "-c", "touch '{}'".format(marker)]}])
    assert _verify(env, repo, "AG-8").returncode == 1
    assert not marker.exists()


def test_every_configured_check_must_pass_and_all_are_recorded(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, run="true")
    env = _with_checks(tmp_path, repo, [{"name": "tests", "command": ["true"]},
                                        {"name": "types", "command": ["true"]}])
    assert _verify(env, repo, "AG-8").returncode == 0
    assert json.load(open(record_path(tmp_path)))["checks"] == {"tests": 0, "types": 0}
    env = _with_checks(tmp_path, repo, [{"name": "tests", "command": ["true"]},
                                        {"name": "types", "command": ["false"]}])
    assert _verify(env, repo, "AG-8").returncode == 1
    assert not record_path(tmp_path).exists()


def test_evidence_recorded_before_a_check_was_configured_does_not_satisfy_the_gate(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, run="true")
    assert verify(tmp_path, repo, "AG-8").returncode == 0
    env = _with_checks(tmp_path, repo, [{"name": "tests", "command": ["true"]}])
    proc = _gate(env, repo)
    assert proc.returncode == 2 and "project check 'tests'" in proc.stderr
    assert _verify(env, repo, "AG-8").returncode == 0
    assert _gate(env, repo).returncode == 0


def test_a_check_that_cannot_start_and_a_broken_config_exit_6(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, run="true")
    env = _with_checks(tmp_path, repo, [
        {"name": "tests", "command": [str(tmp_path / "no-such-program")]}])
    assert _verify(env, repo, "AG-8").returncode == 6
    assert not record_path(tmp_path).exists()
    bad = tmp_path / "gates.json"
    bad.write_text("{not json")
    assert _verify(env, repo, "AG-8").returncode == 6
    assert not record_path(tmp_path).exists()


def test_a_check_that_overruns_is_abandoned_without_evidence(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, run="true")
    env = _with_checks(tmp_path, repo, [{"name": "tests", "command": ["sleep", "30"]}])
    assert _verify(env, repo, "AG-8", "--timeout", "1").returncode == 4
    assert not record_path(tmp_path).exists()


def test_review_evidence_carries_the_checks_too(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, run="true")
    env = _with_checks(tmp_path, repo, [{"name": "tests", "command": ["true"]}])
    assert _verify(env, repo, "AG-8", "--review").returncode == 0
    record = json.load(open(record_path(tmp_path)))
    assert record["kind"] == "review" and record["checks"] == {"tests": 0}


# --- cadence: the full suite runs on the reviewer's run, not twice per issue ---

def _cadence(tmp_path, repo):
    lint = tmp_path / "lint-ran"
    suite = tmp_path / "suite-ran"
    env = _with_checks(tmp_path, repo, [
        {"name": "lint", "command": ["bash", "-c", "touch '{}'".format(lint)]},
        {"name": "tests", "when": "review",
         "command": ["bash", "-c", "touch '{}'".format(suite)]}])
    return env, lint, suite


def test_a_worker_run_skips_review_only_checks_and_still_reaches_in_review(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, run="true")
    env, lint, suite = _cadence(tmp_path, repo)
    assert _verify(env, repo, "AG-8").returncode == 0
    assert lint.exists() and not suite.exists(), "the worker run must not run the full suite"
    assert json.load(open(record_path(tmp_path)))["checks"] == {"lint": 0}
    assert _gate(env, repo).returncode == 0, "In Review needs only the always checks"


def test_done_needs_the_review_run_with_every_check(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, run="true")
    env, lint, suite = _cadence(tmp_path, repo)
    review_env = dict(env, CLAUDE_HARNESS_AGENT_NAME="review-1")
    assert _verify(env, repo, "AG-8", "--review").returncode == 0
    assert suite.exists()
    assert json.load(open(record_path(tmp_path)))["checks"] == {"lint": 0, "tests": 0}
    # A review record without the review-only check does not close the issue.
    record = json.load(open(record_path(tmp_path)))
    del record["checks"]["tests"]
    json.dump(record, open(record_path(tmp_path), "w"))
    payload = {"tool_name": TRANSITION, "cwd": str(repo), "agent_id": "areview-1-01",
               "agent_type": "review-1", "tool_input": {"issueIdOrKey": "AG-8", "transitionId": "31"}}
    proc = subprocess.run([sys.executable, GATE], input=json.dumps(payload), capture_output=True,
                          text=True, env=review_env)
    assert proc.returncode == 2 and "project check 'tests'" in proc.stderr


def test_an_unknown_when_is_a_configuration_fault(tmp_path):
    repo = make_repo(tmp_path)
    know(tmp_path, run="true")
    env = _with_checks(tmp_path, repo, [{"name": "tests", "when": "sometimes",
                                         "command": ["true"]}])
    proc = _verify(env, repo, "AG-8")
    assert proc.returncode == 6 and "`when` must be one of" in proc.stderr
