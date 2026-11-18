"""jira_execute_guard: the Atlassian generic runners are not a way around the gates."""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(REPO, ".claude", "hooks", "jira_execute_guard.py")
P = "mcp__plugin_atlassian_atlassian__"


def run(tmp_path, name, inputs, runner="executeWrite", agent=None, prefix=P, config=None):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({"projectKey": "AG"} if config is None else config))
    env = dict(os.environ, HOME=str(tmp_path / "home"), JIRA_CONFIG_PATH=str(cfg))
    payload = {"tool_name": prefix + runner, "tool_input": {"name": name, "inputs": inputs}}
    if agent:
        payload.update(agent_id="a{}-01".format(agent), agent_type=agent)
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


@pytest.mark.parametrize("name,inputs", [
    ("transitionJiraIssue", {"issueIdOrKey": "AG-7", "transitionId": "31"}),
    ("createJiraIssue", {"projectKey": "AG", "summary": "x", "issueType": "Task"}),
    ("editJiraIssue", {"issueIdOrKey": "ag-7", "fields": {"labels": ["skip-verify"]}}),
])
def test_a_gated_operation_cannot_be_sent_through_a_generic_runner(tmp_path, name, inputs):
    for agent in (None, "coding-2", "review-1"):
        proc = run(tmp_path, name, inputs, agent=agent)
        assert proc.returncode == 2, (name, agent)
        assert "must be called through its own tool" in proc.stderr


def test_a_gated_operation_with_no_stated_project_is_blocked(tmp_path):
    proc = run(tmp_path, "transitionJiraIssue", {"issueIdOrKey": "10234", "transitionId": "31"})
    assert proc.returncode == 2


def test_the_block_applies_through_any_atlassian_server(tmp_path):
    proc = run(tmp_path, "transitionJiraIssue", {"issueIdOrKey": "AG-7"},
               prefix="mcp__plugin_engineering_atlassian__")
    assert proc.returncode == 2


@pytest.mark.parametrize("name", ["bulkTransitionJiraIssues", "deleteJiraIssue",
                                  "moveJiraIssuesToSprint", "convertJiraIssueHierarchy"])
def test_issue_mutations_under_other_names_are_the_leads(tmp_path, name):
    inputs = {"issueIdOrKey": "AG-7"}
    assert run(tmp_path, name, inputs, runner="executeDestructive",
               agent="coding-2").returncode == 2
    assert run(tmp_path, name, inputs, runner="executeDestructive", agent=None).returncode == 0


@pytest.mark.parametrize("name", ["listJiraProjects", "createJiraIssueLink", "getContentFormatGuide",
                                  "createArtifactsUpload", "addWorklog"])
def test_unrelated_operations_pass(tmp_path, name):
    assert run(tmp_path, name, {"issueIdOrKey": "AG-7"}, agent="coding-2").returncode == 0


def test_other_projects_are_not_policed(tmp_path):
    assert run(tmp_path, "transitionJiraIssue", {"issueIdOrKey": "KAN-3"}).returncode == 0
    assert run(tmp_path, "createJiraIssue", {"projectKey": "KAN"}).returncode == 0


def test_read_runner_other_servers_and_garbage_are_ignored(tmp_path):
    assert run(tmp_path, "transitionJiraIssue", {"issueIdOrKey": "AG-7"},
               runner="executeRead").returncode == 0
    assert run(tmp_path, "transitionJiraIssue", {"issueIdOrKey": "AG-7"},
               prefix="mcp__github__").returncode == 0
    assert run(tmp_path, "transitionJiraIssue", {"issueIdOrKey": "AG-7"}, config={}).returncode == 0
    env = dict(os.environ, HOME=str(tmp_path / "home"))
    proc = subprocess.run([sys.executable, HOOK], input="{nope", capture_output=True,
                          text=True, env=env)
    assert proc.returncode == 0
