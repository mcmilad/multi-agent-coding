"""The journaller must record successful mutations, ignore failures and other
projects, and always exit 0 -- it is observational and must never block."""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(REPO, ".claude", "hooks", "jira_mirror_journal.py")
TOOL = "mcp__plugin_atlassian_atlassian__"


def run_hook(payload, home):
    env = dict(os.environ, HOME=str(home))
    proc = subprocess.run(
        [sys.executable, HOOK], input=json.dumps(payload),
        capture_output=True, text=True, env=env,
    )
    return proc


def write_config(tmp_path, monkeypatch):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({"projectKey": "AGENT"}))
    monkeypatch.setenv("JIRA_CONFIG_PATH", str(cfg))
    return cfg


def read_journal(home, project="AGENT"):
    path = os.path.join(str(home), ".claude", "logs", "jira-mirror", project + ".jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_records_created_issue(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {
            "projectKey": "AGENT",
            "summary": "[coding] impl login",
            "additional_fields": {"labels": ["role-coding", "spec-auth"]},
        },
        "tool_response": {"key": "AGENT-14"},
    }, home)
    assert proc.returncode == 0
    events = read_journal(home)
    assert len(events) == 1
    assert events[0]["key"] == "AGENT-14"
    assert events[0]["summary"] == "[coding] impl login"
    assert events[0]["labels"] == ["role-coding", "spec-auth"]
    assert events[0]["status"] == "To Do"


def read_audit(home):
    path = os.path.join(str(home), ".claude", "logs", "team-hooks.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_stringified_additional_fields_still_journals_the_create(tmp_path, monkeypatch):
    """`(x or {}).get(...)` raises AttributeError when a model emits
    additional_fields as a JSON string; the fail-open handler then swallows it
    and NO create event is written. Since only a create event ever writes
    status 'To Do', that issue becomes permanently invisible to the idle
    work-check -- even after later edits restore its labels.
    """
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {
            "projectKey": "AGENT",
            "summary": "[coding] impl login",
            "additional_fields": '{"labels": ["role-coding"]}',
        },
        "tool_response": {"key": "AGENT-14"},
    }, home)
    assert proc.returncode == 0
    events = read_journal(home)
    assert len(events) == 1, "the create must be journalled even if labels are unreadable"
    assert events[0]["key"] == "AGENT-14"
    assert events[0]["status"] == "To Do"


def test_accepts_a_wrapped_issue_key_in_the_response(tmp_path, monkeypatch):
    """The create path rests on an unverified tool_response shape. If the
    harness wraps the MCP result, _succeeded still says True and the key is
    None, so nothing is journalled while the hook looks installed."""
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] impl login"},
        "tool_response": {"issue": {"key": "AGENT-14"}},
    }, home)
    assert read_journal(home)[0]["key"] == "AGENT-14"


def test_accepts_a_key_shaped_top_level_id_when_no_key_field_is_present(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] impl login"},
        "tool_response": {"id": "AGENT-14"},
    }, home)
    assert read_journal(home)[0]["key"] == "AGENT-14"


def test_rejects_a_purely_numeric_top_level_id(tmp_path, monkeypatch):
    """A bare numeric `id` (e.g. the Jira REST numeric issue id, "10042") is not
    an issue key. Every later event for the issue is keyed by the real key
    (e.g. "AGENT-14") via issueIdOrKey, so journalling under the numeric id
    would create a second, permanently-'To Do' mirror entry that no agent can
    ever fetch or transition -- a phantom claimable issue. Treat a numeric-only
    id as no key found rather than accepting it.
    """
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] impl login"},
        "tool_response": {"id": "10042"},
    }, home)
    assert read_journal(home) == []
    reasons = [r.get("reason", "") for r in read_audit(home)]
    assert any("unrecognized create response shape" in r for r in reasons), reasons


def test_unextractable_create_key_is_distinguishable_in_the_audit_log(tmp_path, monkeypatch):
    """A create that succeeds but yields no key is the failure that makes the
    idle check nudge nobody. It must be diagnosable from team-hooks.jsonl, not
    indistinguishable from an ordinary skip."""
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] impl login"},
        "tool_response": {"self": "https://example.atlassian.net/rest/api/3/issue/10042"},
    }, home)
    assert read_journal(home) == []
    reasons = [r.get("reason", "") for r in read_audit(home)]
    assert any("unrecognized create response shape" in r for r in reasons), reasons


def test_records_created_issue_from_real_content_block_response(tmp_path, monkeypatch):
    """Live capture from a real createJiraIssue call: the harness delivers
    tool_response as a list of content blocks -- each a dict with `type` and
    `text`, where `text` is the issue JSON serialized as a *string* -- not
    the bare issue dict `_created_key` used to assume. Reproduced against the
    shipped (buggy) hook: exit 0, no journal file written, audit reason
    "unrecognized create response shape (keys: none)". This is the ground
    truth payload from that live capture, copied verbatim.
    """
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {
            "projectKey": "AGENT",
            "summary": "[coding] impl login",
            "additional_fields": {"labels": ["role-coding", "spec-auth"]},
        },
        "tool_response": [
            {"type": "text",
             "text": "{\n  \"id\": \"10074\",\n  \"key\": \"AGENT-1\",\n  "
                      "\"self\": \"https://api.atlassian.com/ex/jira/.../issue/10074\"\n}"}
        ],
    }, home)
    assert proc.returncode == 0
    events = read_journal(home)
    assert len(events) == 1
    assert events[0]["key"] == "AGENT-1"
    assert events[0]["labels"] == ["role-coding", "spec-auth"]
    assert events[0]["status"] == "To Do"


def test_content_block_response_carrying_error_messages_is_not_journalled(tmp_path, monkeypatch):
    """A content-block list whose text parses to an object carrying
    errorMessages is a failed call, not a success -- must not be journalled,
    unlike the old `bool(response)` check which counted any non-empty list
    as success regardless of content."""
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] x"},
        "tool_response": [
            {"type": "text",
             "text": json.dumps({"errorMessages": ["summary is required"], "errors": {}})}
        ],
    }, home)
    assert proc.returncode == 0
    assert read_journal(home) == []


def test_content_block_response_with_invalid_json_text_is_diagnosable(tmp_path, monkeypatch):
    """A content-block list whose `text` is not valid JSON must journal
    nothing, exit 0, and still emit the distinguishable unrecognized-shape
    audit reason -- the same diagnosability the bare-dict case already had."""
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] x"},
        "tool_response": [{"type": "text", "text": "not valid json {{{"}],
    }, home)
    assert proc.returncode == 0
    assert read_journal(home) == []
    reasons = [r.get("reason", "") for r in read_audit(home)]
    assert any("unrecognized create response shape" in r for r in reasons), reasons


def test_bare_dict_response_still_journals_after_content_block_support_added(tmp_path, monkeypatch):
    """Backward compatibility: the previously-assumed bare-issue-dict shape
    must keep working once content-block list support is added."""
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] impl signup"},
        "tool_response": {"key": "AGENT-9"},
    }, home)
    assert proc.returncode == 0
    events = read_journal(home)
    assert len(events) == 1
    assert events[0]["key"] == "AGENT-9"
    assert events[0]["status"] == "To Do"


def test_content_block_response_with_only_numeric_id_is_rejected(tmp_path, monkeypatch):
    """The anti-poisoning guard (reject a bare numeric id, e.g. the Jira REST
    internal id "10074", as a key) must also hold when the id arrives inside
    a content-block list rather than a bare dict."""
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] x"},
        "tool_response": [{"type": "text", "text": json.dumps({"id": "10074"})}],
    }, home)
    assert proc.returncode == 0
    assert read_journal(home) == []
    reasons = [r.get("reason", "") for r in read_audit(home)]
    assert any("unrecognized create response shape" in r for r in reasons), reasons


def test_records_transition_with_resolved_status(tmp_path, monkeypatch):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({"projectKey": "AGENT", "transitions": {"21": "In Progress"}}))
    monkeypatch.setenv("JIRA_CONFIG_PATH", str(cfg))
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "transitionJiraIssue",
        "tool_input": {"issueIdOrKey": "AGENT-14", "transition": {"id": "21"}},
        "tool_response": {"ok": True},
    }, home)
    assert proc.returncode == 0
    events = read_journal(home)
    assert events[0]["key"] == "AGENT-14"
    assert events[0]["status"] == "In Progress"


def test_records_label_edit(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "editJiraIssue",
        "tool_input": {
            "issueIdOrKey": "AGENT-14",
            "fields": {"labels": ["role-coding", "agent-coding-2"]},
        },
        "tool_response": {"ok": True},
    }, home)
    events = read_journal(home)
    assert events[0]["labels"] == ["role-coding", "agent-coding-2"]


def test_ignores_other_projects(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "editJiraIssue",
        "tool_input": {"issueIdOrKey": "SCRUM-2", "fields": {"labels": ["AGL"]}},
        "tool_response": {"ok": True},
    }, home)
    assert read_journal(home, "SCRUM") == []
    assert read_journal(home, "AGENT") == []


def test_ignores_failed_calls(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] x"},
        "tool_response": {"error": "field 'summary' is required"},
    }, home)
    assert read_journal(home) == []


def test_ignores_unrelated_tools(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": {},
    }, home)
    assert proc.returncode == 0
    assert read_journal(home) == []


def test_exits_zero_on_garbage_payload(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    env = dict(os.environ, HOME=str(home))
    proc = subprocess.run([sys.executable, HOOK], input="not json",
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0


def test_create_with_a_transition_records_the_real_status(tmp_path, monkeypatch):
    """createJiraIssue exposes a top-level transition (verified against the live
    schema). Hardcoding 'To Do' mirrored an issue created straight into
    In Progress as unclaimed -- and since a create carries no agent-* label, the
    idle check then advertised it to every teammate of that role."""
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({
        "projectKey": "AGENT",
        "transitions": {"11": "To Do", "21": "In Progress", "31": "In Review"},
    }))
    monkeypatch.setenv("JIRA_CONFIG_PATH", str(cfg))
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {
            "projectKey": "AGENT",
            "summary": "[coding] already underway",
            "transition": {"id": "21"},
            "additional_fields": {"labels": ["role-coding", "spec-x"]},
        },
        "tool_response": {"key": "AGENT-50"},
    }, home)
    events = [e for e in read_journal(home) if e.get("key") == "AGENT-50"]
    assert events and events[0]["status"] == "In Progress"


def test_create_without_a_transition_still_defaults_to_to_do(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] fresh",
                       "additional_fields": {"labels": ["role-coding", "spec-x"]}},
        "tool_response": {"key": "AGENT-51"},
    }, home)
    events = [e for e in read_journal(home) if e.get("key") == "AGENT-51"]
    assert events and events[0]["status"] == "To Do"


def test_create_journals_the_declared_files(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {
            "projectKey": "AGENT",
            "summary": "[coding] impl",
            "description": ("Spec: x\nFiles: src/a.py, `src/b.py`\n"
                            "Acceptance: y\nRun: pytest -q"),
            "additional_fields": {"labels": ["role-coding", "spec-x"]},
        },
        "tool_response": {"key": "AGENT-52"},
    }, home)
    events = [e for e in read_journal(home) if e.get("key") == "AGENT-52"]
    assert events and events[0]["files"] == ["src/a.py", "src/b.py"]


def test_declared_files_are_journalled_normalised(tmp_path, monkeypatch):
    """claim_gate compares os.path.relpath() output against these strings, and
    the overlap check compares them to another issue's parsed list. Journalled
    raw, a './'-prefixed or doubled-slash path equals neither -- so that one
    file silently left both guardrails while the issue looked perfectly formed."""
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {
            "projectKey": "AGENT",
            "summary": "[coding] impl",
            "description": ("Spec: x\nFiles: ./src/a.py, src//b.py, `./src/c.py` \n"
                            "Acceptance: y\nRun: pytest -q"),
            "additional_fields": {"labels": ["role-coding", "spec-x"]},
        },
        "tool_response": {"key": "AGENT-53"},
    }, home)
    events = [e for e in read_journal(home) if e.get("key") == "AGENT-53"]
    assert events and events[0]["files"] == ["src/a.py", "src/b.py", "src/c.py"]


# --- strict success, description edits, read-through sync (AG-7) ------------

V2 = "mcp__plugin_atlassian_atlassian__"


def _run_v2(tmp_path, op, tool_input, response, raw_tool_input=None):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({
        "projectKey": "AGENT",
        "transitions": {"21": "In Progress", "31": "In Review", "41": "Done"},
        "statuses": {"In Progress": "1", "In Review": "2", "Done": "3"}}))
    env = dict(os.environ, HOME=str(tmp_path / "home"), JIRA_CONFIG_PATH=str(cfg))
    payload = {"tool_name": V2 + op, "tool_response": response,
               "tool_input": tool_input if raw_tool_input is None else raw_tool_input}
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def _events(tmp_path):
    path = tmp_path / "home" / ".claude" / "logs" / "jira-mirror" / "AGENT.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def _state(tmp_path):
    folded = {}
    for event in _events(tmp_path):
        folded.setdefault(event["key"], {}).update(event)
    return folded


DESC = "Spec: s\nFiles: src/a.py, src/b.py\nAcceptance: ok\nRun: pytest tests/a"


def test_an_error_string_response_is_never_journalled(tmp_path):
    for text in ("Error: 401 Unauthorized", "Request failed with status 500",
                 "Transition rejected: not found", "operation timed out"):
        _run_v2(tmp_path, "transitionJiraIssue",
                {"issueIdOrKey": "AGENT-4", "transitionId": "31"}, text)
    assert _events(tmp_path) == []


def test_an_error_text_block_is_never_journalled(tmp_path):
    _run_v2(tmp_path, "transitionJiraIssue",
            {"issueIdOrKey": "AGENT-4", "transitionId": "31"},
            [{"type": "text", "text": "Error: 401 Unauthorized"}])
    assert _events(tmp_path) == []


def test_an_http_error_status_in_the_envelope_is_never_journalled(tmp_path):
    _run_v2(tmp_path, "transitionJiraIssue",
            {"issueIdOrKey": "AGENT-4", "transitionId": "31"},
            {"data": {"message": "Jira said no", "status": 403}})
    assert _events(tmp_path) == []


def test_is_error_flag_is_never_journalled(tmp_path):
    _run_v2(tmp_path, "transitionJiraIssue",
            {"issueIdOrKey": "AGENT-4", "transitionId": "31"},
            {"isError": True, "content": []})
    assert _events(tmp_path) == []


def test_a_plain_success_string_is_still_journalled(tmp_path):
    _run_v2(tmp_path, "transitionJiraIssue",
            {"issueIdOrKey": "AGENT-4", "transitionId": "31"},
            "Issue transitioned successfully")
    assert _events(tmp_path)[-1]["status"] == "In Review"


def test_create_records_the_run_command(tmp_path):
    _run_v2(tmp_path, "createJiraIssue",
            {"projectKey": "AGENT", "summary": "[coding] x", "description": DESC,
             "labels": ["role-coding", "spec-x"]},
            {"data": {"key": "AGENT-4"}})
    assert _state(tmp_path)["AGENT-4"]["run"] == "pytest tests/a"


def test_editing_the_description_updates_files_and_run(tmp_path):
    _run_v2(tmp_path, "createJiraIssue",
            {"projectKey": "AGENT", "summary": "[coding] x", "description": DESC,
             "labels": ["role-coding", "spec-x"]},
            {"data": {"key": "AGENT-4"}})
    _run_v2(tmp_path, "editJiraIssue",
            {"issueIdOrKey": "AGENT-4",
             "fields": {"description": DESC.replace("src/a.py, src/b.py", "src/c.py")
                        .replace("pytest tests/a", "`pytest tests/c`")}},
            {"data": {"key": "AGENT-4"}})
    issue = _state(tmp_path)["AGENT-4"]
    assert issue["files"] == ["src/c.py"]
    assert issue["run"] == "pytest tests/c"


def test_an_edit_without_a_description_keeps_the_declared_files(tmp_path):
    _run_v2(tmp_path, "createJiraIssue",
            {"projectKey": "AGENT", "summary": "[coding] x", "description": DESC,
             "labels": ["role-coding", "spec-x"]},
            {"data": {"key": "AGENT-4"}})
    _run_v2(tmp_path, "editJiraIssue",
            {"issueIdOrKey": "AGENT-4", "fields": {"labels": ["role-coding", "agent-c-1"]}},
            {"data": {"key": "AGENT-4", "fields": {"status": {"name": "To Do"}}}})
    issue = _state(tmp_path)["AGENT-4"]
    assert issue["files"] == ["src/a.py", "src/b.py"]
    assert "agent-c-1" in issue["labels"]


def test_a_string_typed_tool_input_is_decoded_not_dropped(tmp_path):
    tool_input = {"issueIdOrKey": "AGENT-4", "transitionId": "21"}
    _run_v2(tmp_path, "transitionJiraIssue", tool_input, {"data": {"message": "ok"}},
            raw_tool_input=json.dumps(tool_input))
    assert _events(tmp_path)[-1]["status"] == "In Progress"


def test_string_typed_fields_do_not_crash_the_edit_path(tmp_path):
    proc = _run_v2(tmp_path, "editJiraIssue",
                   {"issueIdOrKey": "AGENT-4", "fields": json.dumps({"labels": ["x"]})},
                   {"data": {"key": "AGENT-4"}})
    assert proc.returncode == 0
    assert _events(tmp_path)[-1]["op"] == "edit"


def test_a_search_response_syncs_every_row_of_this_project(tmp_path):
    rows = [{"key": "AGENT-1", "fields": {"summary": "a", "status": {"name": "Done"},
                                          "labels": ["role-coding"]}},
            {"key": "AGENT-2", "fields": {"status": {"name": "To Do"}, "labels": []}},
            {"key": "OTHER-9", "fields": {"status": {"name": "To Do"}}}]
    _run_v2(tmp_path, "searchJiraIssuesUsingJql", {"jql": "project = AGENT"},
            [{"type": "text", "text": json.dumps({"data": {"issues": rows}})}])
    state = _state(tmp_path)
    assert set(state) == {"AGENT-1", "AGENT-2"}
    assert state["AGENT-1"]["status"] == "Done"


def test_a_single_issue_read_syncs_files_run_and_blockers(tmp_path):
    issue = {"key": "AGENT-5", "fields": {
        "summary": "s", "status": {"name": "In Progress"}, "labels": ["role-devops"],
        "description": DESC,
        "issuelinks": [{"type": {"inward": "is blocked by"}, "inwardIssue": {"key": "agent-3"}}]}}
    _run_v2(tmp_path, "getJiraIssue", {"issueIdOrKey": "AGENT-5"}, {"data": issue})
    state = _state(tmp_path)["AGENT-5"]
    assert state["files"] == ["src/a.py", "src/b.py"]
    assert state["run"] == "pytest tests/a"
    assert state["blocked_by"] == ["AGENT-3"]


def test_a_read_corrects_a_status_changed_by_hand_on_the_board(tmp_path):
    _run_v2(tmp_path, "transitionJiraIssue",
            {"issueIdOrKey": "AGENT-4", "transitionId": "31"}, {"data": {"message": "ok"}})
    _run_v2(tmp_path, "getJiraIssue", {"issueIdOrKey": "AGENT-4"},
            {"data": {"key": "AGENT-4", "fields": {"status": {"name": "To Do"}}}})
    assert _state(tmp_path)["AGENT-4"]["status"] == "To Do"


def test_a_failed_read_syncs_nothing(tmp_path):
    _run_v2(tmp_path, "getJiraIssue", {"issueIdOrKey": "AGENT-4"},
            {"data": {"error": "NOT_FOUND", "status": 404}})
    assert _events(tmp_path) == []


# --- who applied a bypass label --------------------------------------------

def _run_as(tmp_path, agent, op, tool_input, response):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({"projectKey": "AGENT"}))
    env = dict(os.environ, HOME=str(tmp_path / "home"), JIRA_CONFIG_PATH=str(cfg))
    payload = {"tool_name": V2 + op, "tool_input": tool_input, "tool_response": response}
    if agent:
        payload.update(agent_id="a{}-01".format(agent), agent_type=agent)
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def _edit_labels(tmp_path, agent, labels):
    _run_as(tmp_path, agent, "editJiraIssue",
            {"issueIdOrKey": "AGENT-4", "fields": {"labels": labels}},
            {"data": {"key": "AGENT-4"}})


def _author(tmp_path, label="skip-verify"):
    return (_state(tmp_path)["AGENT-4"].get("bypass_by") or {}).get(label, "")


def test_a_bypass_label_records_the_lead_as_its_author(tmp_path):
    _edit_labels(tmp_path, None, ["role-sa", "skip-verify"])
    assert _author(tmp_path) == "lead"


def test_a_bypass_label_records_a_worker_as_its_author(tmp_path):
    _edit_labels(tmp_path, "coding-2", ["role-sa", "skip-verify"])
    assert _author(tmp_path) == "coding-2"


def test_resending_the_label_keeps_the_original_author(tmp_path):
    _edit_labels(tmp_path, None, ["role-sa", "skip-verify"])
    _edit_labels(tmp_path, "sa-1", ["role-sa", "skip-verify", "agent-sa-1"])
    assert _author(tmp_path) == "lead"


def test_removing_the_label_clears_the_author(tmp_path):
    _edit_labels(tmp_path, None, ["role-sa", "skip-verify"])
    _edit_labels(tmp_path, None, ["role-sa"])
    assert _author(tmp_path) == ""


def _read_labels(tmp_path, labels):
    _run_as(tmp_path, None, "getJiraIssue", {"issueIdOrKey": "AGENT-4"},
            {"data": {"key": "AGENT-4", "fields": {"labels": labels}}})


def test_a_label_seen_only_in_a_read_has_no_author(tmp_path):
    _read_labels(tmp_path, ["role-sa", "skip-verify"])
    assert _author(tmp_path) == ""


def test_each_bypass_label_has_its_own_author(tmp_path):
    _edit_labels(tmp_path, None, ["role-sa", "skip-format-check"])
    _edit_labels(tmp_path, "sa-1", ["role-sa", "skip-format-check", "skip-verify"])
    assert _author(tmp_path, "skip-format-check") == "lead"
    assert _author(tmp_path, "skip-verify") == "sa-1"


def test_a_leads_format_bypass_does_not_vouch_for_a_skip_verify_seen_in_a_read(tmp_path):
    """Review finding I2: one shared author field let a lead-applied
    skip-format-check make a later, unattributed skip-verify count as the lead's."""
    _edit_labels(tmp_path, None, ["role-sa", "skip-format-check"])
    _read_labels(tmp_path, ["role-sa", "skip-format-check", "skip-verify"])
    assert _author(tmp_path, "skip-format-check") == "lead"
    assert _author(tmp_path, "skip-verify") == ""


def test_a_read_keeps_the_author_of_a_label_the_lead_really_applied(tmp_path):
    _edit_labels(tmp_path, None, ["role-sa", "skip-verify"])
    _read_labels(tmp_path, ["role-sa", "skip-verify"])
    assert _author(tmp_path) == "lead"


# --- review cycle 2, R3: re-sending labels must not adopt a bypass ----------

def test_the_lead_resending_labels_does_not_adopt_an_unattributed_skip_verify(tmp_path):
    """The label arrived by a route no hook saw; the lead then released a claim,
    re-sending every label. The label must stay unattributed."""
    _read_labels(tmp_path, ["role-sa", "skip-verify", "agent-sa-1"])
    assert _author(tmp_path) == ""
    _edit_labels(tmp_path, None, ["role-sa", "skip-verify"])           # release
    assert _author(tmp_path) == ""
    _edit_labels(tmp_path, None, ["role-sa", "skip-verify", "group-2"])  # add a group
    assert _author(tmp_path) == ""


def test_a_worker_resending_labels_does_not_adopt_one_either(tmp_path):
    _read_labels(tmp_path, ["role-sa", "skip-verify"])
    _edit_labels(tmp_path, "sa-1", ["role-sa", "skip-verify", "agent-sa-1"])
    assert _author(tmp_path) == ""


def test_the_lead_applies_a_bypass_by_removing_and_re_adding_it(tmp_path):
    _read_labels(tmp_path, ["role-sa", "skip-verify"])
    _edit_labels(tmp_path, None, ["role-sa"])
    _edit_labels(tmp_path, None, ["role-sa", "skip-verify"])
    assert _author(tmp_path) == "lead"


# --- N5: reads through the generic runner reach the mirror (AG-18) -----------

def test_a_single_issue_read_through_execute_read_syncs_the_mirror(tmp_path):
    issue = {"key": "AGENT-5", "fields": {
        "summary": "s", "status": {"name": "In Progress"}, "labels": ["role-devops"],
        "description": DESC}}
    proc = _run_v2(tmp_path, "executeRead",
                   {"name": "getJiraIssue", "cloudId": "c", "inputs": {"issueIdOrKey": "AGENT-5"}},
                   {"data": issue})
    assert proc.returncode == 0
    state = _state(tmp_path)["AGENT-5"]
    assert state["status"] == "In Progress" and state["run"] == "pytest tests/a"
    assert state["files"] == ["src/a.py", "src/b.py"]


def test_a_search_through_execute_read_syncs_every_row(tmp_path):
    rows = [{"key": "AGENT-1", "fields": {"status": {"name": "Done"}, "labels": []}},
            {"key": "OTHER-9", "fields": {"status": {"name": "To Do"}}}]
    _run_v2(tmp_path, "executeRead",
            {"name": "searchJiraIssuesUsingJql", "inputs": {"jql": "project = AGENT"}},
            [{"type": "text", "text": json.dumps({"data": {"issues": rows}})}])
    assert set(_state(tmp_path)) == {"AGENT-1"}


def test_execute_read_of_another_operation_journals_nothing(tmp_path):
    for name in ("getConfluenceContent", "listJiraIssueComments", "", None):
        proc = _run_v2(tmp_path, "executeRead", {"name": name, "inputs": {}},
                       {"data": {"key": "AGENT-7", "fields": {"status": {"name": "Done"}}}})
        assert proc.returncode == 0
    assert _events(tmp_path) == []


def test_execute_read_can_never_record_a_write(tmp_path):
    """A read runner naming a write operation is not evidence anything was written."""
    for name in ("transitionJiraIssue", "editJiraIssue", "createJiraIssue"):
        _run_v2(tmp_path, "executeRead",
                {"name": name, "inputs": {"issueIdOrKey": "AGENT-4", "transitionId": "41"}},
                {"data": {"message": "ok"}})
    assert _events(tmp_path) == []


def test_a_failed_execute_read_syncs_nothing(tmp_path):
    _run_v2(tmp_path, "executeRead", {"name": "getJiraIssue", "inputs": {}},
            {"data": {"error": "NOT_FOUND", "status": 404}})
    assert _events(tmp_path) == []


def test_the_journal_is_wired_to_execute_read():
    import re
    settings = json.load(open(os.path.join(REPO, ".claude", "settings.json")))
    matchers = [h["matcher"] for h in settings["hooks"]["PostToolUse"]
                if any("jira_mirror_journal.py" in x["command"] for x in h["hooks"])]
    assert any(re.fullmatch(m, "mcp__plugin_atlassian_atlassian__executeRead")
               for m in matchers)
