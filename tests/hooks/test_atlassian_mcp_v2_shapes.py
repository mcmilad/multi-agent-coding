"""The Atlassian MCP changed its input names; the hooks must read both shapes.

Current server (verified live, 2026-11-15): createJiraIssue takes `issueType`
and a top-level `labels` array and REJECTS `additional_fields.labels`;
transitionJiraIssue takes `transitionId` / `transitionName`; the comment tool is
`addOrEditJiraIssueComment`. Hooks that read only the older names blocked every
create and resolved every transition to "unknown", which is never gated.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from evidence_helpers import make_known, write_evidence  # noqa: E402
HOOKS = os.path.join(REPO, ".claude", "hooks")
PREFIX = "mcp__plugin_atlassian_atlassian__"
CONFIG = {
    "projectKey": "AGENT",
    "gatedStatuses": ["In Review", "Done"],
    "statuses": {"To Do": "1", "In Progress": "2", "In Review": "3", "Done": "4"},
    "transitions": {"11": "To Do", "21": "In Progress", "31": "In Review", "41": "Done"},
}
DESCRIPTION = (
    "Spec: .claude/specs/x/spec.md\n"
    "Files: src/a.py, tests/test_a.py\n"
    "Acceptance: it works\n"
    "Run: pytest tests/test_a.py"
)


def run(hook, tool, tool_input, tmp_path, response=None):
    if hook == "jira_transition_verify_gate.py":
        make_known(tmp_path / "home", "AGENT-7")   # the gate refuses unknown issues
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps(CONFIG))
    env = dict(os.environ, HOME=str(tmp_path / "home"), JIRA_CONFIG_PATH=str(cfg))
    payload = {"tool_name": PREFIX + tool, "tool_input": tool_input}
    if response is not None:
        payload["tool_response"] = response
    return subprocess.run([sys.executable, os.path.join(HOOKS, hook)],
                          input=json.dumps(payload), capture_output=True,
                          text=True, env=env)


def mirror(tmp_path):
    path = tmp_path / "home" / ".claude" / "logs" / "jira-mirror" / "AGENT.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def v2_task(**overrides):
    issue = {
        "cloudId": "x", "projectKey": "AGENT", "issueType": "Task",
        "summary": "[coding] add a thing", "description": DESCRIPTION,
        "labels": ["spec-x", "role-coding", "group-1"],
    }
    issue.update(overrides)
    return issue


# --- format check -----------------------------------------------------------

def test_top_level_labels_satisfy_the_format_check(tmp_path):
    assert run("jira_issue_format_check.py", "createJiraIssue", v2_task(), tmp_path).returncode == 0


def test_top_level_labels_are_still_policed(tmp_path):
    proc = run("jira_issue_format_check.py", "createJiraIssue",
               v2_task(labels=["spec-x"]), tmp_path)
    assert proc.returncode == 2
    assert "role-" in proc.stderr


def test_issue_type_field_exempts_an_epic(tmp_path):
    proc = run("jira_issue_format_check.py", "createJiraIssue",
               {"projectKey": "AGENT", "issueType": "Epic", "summary": "A spec",
                "description": "about", "labels": ["spec-x"]}, tmp_path)
    assert proc.returncode == 0


def test_a_task_is_not_exempted_by_claiming_nothing(tmp_path):
    proc = run("jira_issue_format_check.py", "createJiraIssue",
               v2_task(summary="add a thing"), tmp_path)
    assert proc.returncode == 2


def test_top_level_bypass_label_is_honoured(tmp_path):
    proc = run("jira_issue_format_check.py", "createJiraIssue",
               {"projectKey": "AGENT", "issueType": "Task", "summary": "coordinate",
                "description": "n/a", "labels": ["skip-format-check"]}, tmp_path)
    assert proc.returncode == 0


def test_summary_tag_must_still_agree_with_a_top_level_role_label(tmp_path):
    proc = run("jira_issue_format_check.py", "createJiraIssue",
               v2_task(labels=["spec-x", "role-devops"]), tmp_path)
    assert proc.returncode == 2


# --- verification gate ------------------------------------------------------

def test_transition_id_field_is_gated(tmp_path):
    proc = run("jira_transition_verify_gate.py", "transitionJiraIssue",
               {"issueIdOrKey": "AGENT-7", "transitionId": "31"}, tmp_path)
    assert proc.returncode == 2
    assert "no evidence record" in proc.stderr.lower()


def test_transition_id_field_ungated_target_is_allowed(tmp_path):
    proc = run("jira_transition_verify_gate.py", "transitionJiraIssue",
               {"issueIdOrKey": "AGENT-7", "transitionId": "21"}, tmp_path)
    assert proc.returncode == 0


def test_status_named_transition_is_gated(tmp_path):
    proc = run("jira_transition_verify_gate.py", "transitionJiraIssue",
               {"issueIdOrKey": "AGENT-7", "transitionName": "Done"}, tmp_path)
    assert proc.returncode == 2
    assert "no evidence record" in proc.stderr.lower()


def test_unresolvable_transition_name_is_blocked_not_waved_through(tmp_path):
    proc = run("jira_transition_verify_gate.py", "transitionJiraIssue",
               {"issueIdOrKey": "AGENT-7", "transitionName": "Ship it"}, tmp_path)
    assert proc.returncode == 2
    assert "transitionId" in proc.stderr


def test_a_sprint_only_move_is_not_gated(tmp_path):
    proc = run("jira_transition_verify_gate.py", "transitionJiraIssue",
               {"issueIdOrKey": "AGENT-7", "sprintId": 5}, tmp_path)
    assert proc.returncode == 0


def test_transition_id_with_a_sentinel_passes(tmp_path):
    write_evidence(tmp_path / "home", "AGENT-7")
    proc = run("jira_transition_verify_gate.py", "transitionJiraIssue",
               {"issueIdOrKey": "AGENT-7", "transitionId": "31"}, tmp_path)
    assert proc.returncode == 0


# --- mirror journal ---------------------------------------------------------

def test_journal_records_top_level_labels_and_files_on_create(tmp_path):
    run("jira_mirror_journal.py", "createJiraIssue", v2_task(), tmp_path,
        response={"key": "AGENT-9"})
    event = mirror(tmp_path)[-1]
    assert event["key"] == "AGENT-9"
    assert "role-coding" in event["labels"]
    assert event["files"] == ["src/a.py", "tests/test_a.py"]
    assert event["status"] == "To Do"


def test_journal_records_status_for_transition_id(tmp_path):
    run("jira_mirror_journal.py", "transitionJiraIssue",
        {"issueIdOrKey": "AGENT-9", "transitionId": "21"}, tmp_path,
        response={"ok": True})
    assert mirror(tmp_path)[-1]["status"] == "In Progress"


def test_journal_watches_the_renamed_comment_tool(tmp_path):
    run("jira_mirror_journal.py", "addOrEditJiraIssueComment",
        {"issueIdOrKey": "AGENT-9", "commentBody": "Claimed by x."}, tmp_path,
        response={"commentId": "1"})
    assert mirror(tmp_path)[-1]["op"] == "comment"


def test_settings_matcher_covers_the_renamed_comment_tool():
    settings = json.load(open(os.path.join(REPO, ".claude", "settings.json")))
    matchers = [h["matcher"] for h in settings["hooks"]["PostToolUse"]]
    assert any("addOrEditJiraIssueComment" in m for m in matchers)


# --- response envelope ------------------------------------------------------

def _blocks(obj):
    """The harness hands tool_response back as content blocks of JSON text."""
    return [{"type": "text", "text": json.dumps(obj)}]


def test_journal_reads_the_key_out_of_the_data_envelope(tmp_path):
    run("jira_mirror_journal.py", "createJiraIssue", v2_task(), tmp_path,
        response=_blocks({"data": {"id": "10001", "key": "AGENT-2"}}))
    events = mirror(tmp_path)
    assert events and events[-1]["key"] == "AGENT-2"
    assert events[-1]["files"] == ["src/a.py", "tests/test_a.py"]


def test_an_error_inside_the_data_envelope_is_not_journalled(tmp_path):
    run("jira_mirror_journal.py", "createJiraIssue", v2_task(), tmp_path,
        response=_blocks({"data": {"error": "CREATE_FAILED", "status": 400}}))
    assert mirror(tmp_path) == []


def test_an_enveloped_failed_transition_is_not_journalled(tmp_path):
    run("jira_mirror_journal.py", "transitionJiraIssue",
        {"issueIdOrKey": "AGENT-9", "transitionId": "31"}, tmp_path,
        response=_blocks({"data": {"error": "TRANSITION_FAILED"}}))
    assert mirror(tmp_path) == []


# --- any Atlassian server, any key form -------------------------------------

def run_named(hook, tool_name, tool_input, tmp_path, response=None):
    if hook == "jira_transition_verify_gate.py":
        make_known(tmp_path / "home", "AGENT-7")
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps(CONFIG))
    env = dict(os.environ, HOME=str(tmp_path / "home"), JIRA_CONFIG_PATH=str(cfg))
    payload = {"tool_name": tool_name, "tool_input": tool_input}
    if response is not None:
        payload["tool_response"] = response
    return subprocess.run([sys.executable, os.path.join(HOOKS, hook)],
                          input=json.dumps(payload), capture_output=True,
                          text=True, env=env)


OTHER_SERVERS = ("mcp__plugin_engineering_atlassian__", "mcp__atlassian__")


def test_the_gate_applies_through_any_atlassian_server(tmp_path):
    for prefix in OTHER_SERVERS:
        proc = run_named("jira_transition_verify_gate.py", prefix + "transitionJiraIssue",
                         {"issueIdOrKey": "AGENT-7", "transitionId": "41"}, tmp_path)
        assert proc.returncode == 2, prefix


def test_the_format_check_applies_through_any_atlassian_server(tmp_path):
    for prefix in OTHER_SERVERS:
        proc = run_named("jira_issue_format_check.py", prefix + "createJiraIssue",
                         v2_task(summary="no tag"), tmp_path)
        assert proc.returncode == 2, prefix


def test_the_journal_records_through_any_atlassian_server(tmp_path):
    run_named("jira_mirror_journal.py", OTHER_SERVERS[0] + "createJiraIssue", v2_task(),
              tmp_path, response={"data": {"key": "AGENT-31"}})
    assert mirror(tmp_path)[-1]["key"] == "AGENT-31"


def test_a_non_atlassian_server_is_left_alone(tmp_path):
    proc = run_named("jira_transition_verify_gate.py", "mcp__github__transitionJiraIssue",
                     {"issueIdOrKey": "AGENT-7", "transitionId": "41"}, tmp_path)
    assert proc.returncode == 0


def test_a_lowercase_key_is_gated_like_its_uppercase_form(tmp_path):
    proc = run("jira_transition_verify_gate.py", "transitionJiraIssue",
               {"issueIdOrKey": "agent-7", "transitionId": "41"}, tmp_path)
    assert proc.returncode == 2
    assert "no evidence record" in proc.stderr.lower()


def test_an_issue_url_is_gated_like_its_key(tmp_path):
    proc = run("jira_transition_verify_gate.py", "transitionJiraIssue",
               {"issueIdOrKey": "https://x.atlassian.net/browse/AGENT-7?focused=1",
                "transitionId": "41"}, tmp_path)
    assert proc.returncode == 2


def test_a_numeric_id_cannot_walk_past_a_gated_transition(tmp_path):
    proc = run("jira_transition_verify_gate.py", "transitionJiraIssue",
               {"issueIdOrKey": "10234", "transitionId": "41"}, tmp_path)
    assert proc.returncode == 2
    assert "issue key" in proc.stderr


def test_a_numeric_id_on_an_ungated_transition_is_allowed(tmp_path):
    proc = run("jira_transition_verify_gate.py", "transitionJiraIssue",
               {"issueIdOrKey": "10234", "transitionId": "21"}, tmp_path)
    assert proc.returncode == 0


def test_a_sentinel_written_for_the_key_serves_a_lowercase_call(tmp_path):
    write_evidence(tmp_path / "home", "AGENT-7")
    proc = run("jira_transition_verify_gate.py", "transitionJiraIssue",
               {"issueIdOrKey": "agent-7", "transitionId": "31"}, tmp_path)
    assert proc.returncode == 0


def test_the_journal_stores_a_lowercase_key_in_canonical_form(tmp_path):
    run("jira_mirror_journal.py", "transitionJiraIssue",
        {"issueIdOrKey": "agent-9", "transitionId": "21"}, tmp_path,
        response={"data": {"message": "ok"}})
    assert mirror(tmp_path)[-1]["key"] == "AGENT-9"


def test_settings_matcher_covers_the_read_tools_the_mirror_syncs_from():
    settings = json.load(open(os.path.join(REPO, ".claude", "settings.json")))
    matchers = [h["matcher"] for h in settings["hooks"]["PostToolUse"]]
    for tool in ("searchJiraIssuesUsingJql", "getJiraIssue"):
        assert any(tool in m for m in matchers), tool
