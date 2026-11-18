"""scripts/mirror_resync.py rebuilds the mirror from a saved Jira response."""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "mirror_resync.py")

ISSUE = {
    "id": "1", "key": "AG-7",
    "fields": {
        "summary": "[coding] do it",
        "status": {"name": "In Progress"},
        "labels": ["role-coding", "spec-x", "agent-coding-1"],
        "description": "Spec: s  \nFiles: src/a.py, tests/  \nAcceptance: ok  \nRun: `pytest tests`",
        "issuelinks": [
            {"type": {"inward": "is blocked by", "outward": "blocks"},
             "inwardIssue": {"key": "AG-6"}},
            {"type": {"inward": "is blocked by", "outward": "blocks"},
             "outwardIssue": {"key": "AG-9"}},
            {"type": {"inward": "relates to", "outward": "relates to"},
             "inwardIssue": {"key": "AG-3"}},
        ],
    },
}


def run(tmp_path, payload, *args, config=None, stdin=False):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({"projectKey": "AG"} if config is None else config))
    env = dict(os.environ, HOME=str(tmp_path / "home"), JIRA_CONFIG_PATH=str(cfg))
    src = tmp_path / "saved.json"
    src.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    argv = [sys.executable, SCRIPT, *args, "-" if stdin else str(src)]
    return subprocess.run(argv, input=src.read_text() if stdin else None,
                          capture_output=True, text=True, env=env)


def state(tmp_path):
    path = tmp_path / "home" / ".claude" / "logs" / "jira-mirror" / "AG.jsonl"
    folded = {}
    if not path.exists():
        return folded
    for line in path.read_text().splitlines():
        event = json.loads(line)
        folded.setdefault(event["key"], {}).update(event)
    return folded


def test_rebuilds_an_issue_from_a_search_result(tmp_path):
    proc = run(tmp_path, {"data": {"issues": [ISSUE]}})
    assert proc.returncode == 0, proc.stderr
    issue = state(tmp_path)["AG-7"]
    assert issue["status"] == "In Progress"
    assert issue["files"] == ["src/a.py", "tests"]
    assert issue["run"] == "pytest tests"
    assert "role-coding" in issue["labels"]


def test_records_only_inward_blocked_by_links(tmp_path):
    run(tmp_path, {"issues": [ISSUE]})
    assert state(tmp_path)["AG-7"]["blocked_by"] == ["AG-6"]


def test_accepts_a_single_issue_and_a_bare_list(tmp_path):
    assert run(tmp_path, {"data": ISSUE}).returncode == 0
    assert run(tmp_path, [ISSUE]).returncode == 0
    assert "AG-7" in state(tmp_path)


def test_reads_stdin(tmp_path):
    assert run(tmp_path, {"issues": [ISSUE]}, stdin=True).returncode == 0
    assert "AG-7" in state(tmp_path)


def test_ignores_issues_of_other_projects(tmp_path):
    other = dict(ISSUE, key="KAN-1")
    proc = run(tmp_path, {"issues": [other]})
    assert proc.returncode == 3
    assert state(tmp_path) == {}


def test_a_compact_row_does_not_erase_known_files(tmp_path):
    run(tmp_path, {"issues": [ISSUE]})
    compact = {"key": "AG-7", "fields": {"status": {"name": "In Review"}}}
    run(tmp_path, {"issues": [compact]})
    issue = state(tmp_path)["AG-7"]
    assert issue["status"] == "In Review"
    assert issue["files"] == ["src/a.py", "tests"]


def test_replace_discards_stale_entries_and_keeps_a_backup(tmp_path):
    stale = dict(ISSUE, key="AG-99")
    run(tmp_path, {"issues": [stale]})
    proc = run(tmp_path, {"issues": [ISSUE]}, "--replace")
    assert proc.returncode == 0
    assert set(state(tmp_path)) == {"AG-7"}
    backup = tmp_path / "home" / ".claude" / "logs" / "jira-mirror" / "AG.jsonl.bak"
    assert "AG-99" in backup.read_text()


def test_unreadable_input_and_missing_config_have_distinct_exit_codes(tmp_path):
    assert run(tmp_path, "{not json").returncode == 1
    assert run(tmp_path, {"issues": [ISSUE]}, config={}).returncode == 2


def test_an_unwritable_mirror_is_reported_not_claimed_as_synced(tmp_path):
    """The mirror is denied to an agent's shell. append_event never raises, so
    without this the script printed "synced" having written nothing."""
    state = tmp_path / "state"
    (state / "jira-mirror").mkdir(parents=True)
    os.chmod(state / "jira-mirror", 0o500)
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({"projectKey": "AG"}))
    src = tmp_path / "saved.json"
    src.write_text(json.dumps({"issues": [ISSUE]}))
    env = dict(os.environ, HOME=str(tmp_path / "home"), JIRA_CONFIG_PATH=str(cfg),
               CLAUDE_HARNESS_STATE_DIR=str(state))
    try:
        proc = subprocess.run([sys.executable, SCRIPT, str(src)], capture_output=True,
                              text=True, env=env)
    finally:
        os.chmod(state / "jira-mirror", 0o700)
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "your own terminal" in proc.stderr and "synced" not in proc.stdout
