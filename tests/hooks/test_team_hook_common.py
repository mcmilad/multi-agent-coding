"""Shared hook helpers. These are small, but every hook depends on them, so a
wrong answer here misroutes or silently disables a guardrail.
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, ".claude", "hooks"))

import team_hook_common  # noqa: E402

role_of_teammate = team_hook_common.role_of_teammate


def test_exact_role_name_maps_to_its_role():
    for role in ("coding", "devops", "sa", "review"):
        assert role_of_teammate(role) == role


def test_instance_suffixed_names_map_to_their_role():
    assert role_of_teammate("coding-2") == "coding"
    assert role_of_teammate("coding-agent") == "coding"
    assert role_of_teammate("review-1") == "review"
    assert role_of_teammate("sa-agent") == "sa"
    assert role_of_teammate("DevOps-3") == "devops"


def test_a_bare_prefix_match_is_not_a_role():
    """'sample-1'.startswith('sa') is true, which used to hand a teammate named
    'sample-1' the sa pool's work. Only an exact name or a `<role>-` instance
    suffix is a real role."""
    assert role_of_teammate("sample-1") is None
    assert role_of_teammate("sandbox") is None
    assert role_of_teammate("reviewer") is None
    assert role_of_teammate("codingagent") is None


def test_unknown_and_empty_names_map_to_nothing():
    assert role_of_teammate("mystery-bot") is None
    assert role_of_teammate("") is None
    assert role_of_teammate(None) is None


def test_as_dict_coalesces_truthy_non_dicts():
    """`value or {}` only guards falsy values; a truthy non-dict -- a JSON
    string, exactly what an LLM emits for additional_fields when it stringifies
    a nested object -- passes through and raises AttributeError on .get()."""
    assert team_hook_common.as_dict({"a": 1}) == {"a": 1}
    assert team_hook_common.as_dict('{"labels": ["role-coding"]}') == {}
    assert team_hook_common.as_dict(["labels"]) == {}
    assert team_hook_common.as_dict(None) == {}
    assert team_hook_common.as_dict(0) == {}


tool_input_of = team_hook_common.tool_input_of


def test_tool_input_of_decodes_a_stringified_object():
    """as_dict() would reduce this to {}, which names no project -- and every
    gating hook reads its scope out of tool_input, so an empty one reads as
    'somebody else's project' and the guardrail silently switches off."""
    assert tool_input_of({"tool_input": {"projectKey": "AGENT"}}) == (
        {"projectKey": "AGENT"}, None)
    assert tool_input_of({"tool_input": '{"projectKey": "AGENT"}'}) == (
        {"projectKey": "AGENT"}, None)


def test_tool_input_of_reports_a_genuinely_unreadable_input():
    """Unreadable must be distinguishable from empty. Reporting it as {} is what
    let both hooks record 'belongs to another project' for an input whose
    project they never managed to read."""
    for bad in ("AGENT-14 to In Review", "[1, 2]", 7, ["a"]):
        value, unreadable = tool_input_of({"tool_input": bad})
        assert value == {}
        assert unreadable, "a %s tool_input is unreadable, not empty" % type(bad).__name__
        assert "tool_input" in unreadable and "JSON object" in unreadable


def test_tool_input_of_treats_an_absent_input_as_empty_not_unreadable():
    assert tool_input_of({}) == ({}, None)
    assert tool_input_of({"tool_input": None}) == ({}, None)
    assert tool_input_of("not a payload") == ({}, None)


# --- per-repository state root ---------------------------------------------

import importlib  # noqa: E402


def _common(monkeypatch, tmp_path, project_dir=None, override=None):
    """Fresh import of team_hook_common with a controlled environment."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    if override is None:
        monkeypatch.delenv("CLAUDE_HARNESS_STATE_DIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_HARNESS_STATE_DIR", override)
    if project_dir is None:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    hooks = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), ".claude", "hooks")
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    import team_hook_common
    return importlib.reload(team_hook_common)


def _make_repo(root):
    (root / ".git").mkdir(parents=True)
    return root


def test_state_root_lives_in_the_repositorys_git_directory(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "repo")
    thc = _common(monkeypatch, tmp_path, project_dir=repo)
    assert thc.state_root() == os.path.join(os.path.realpath(str(repo / ".git")),
                                            "claude-harness")


def test_two_checkouts_with_one_project_key_do_not_share_state(monkeypatch, tmp_path):
    a = _make_repo(tmp_path / "a")
    b = _make_repo(tmp_path / "b")
    root_a = _common(monkeypatch, tmp_path, project_dir=a).verified_dir()
    root_b = _common(monkeypatch, tmp_path, project_dir=b).verified_dir()
    assert root_a != root_b
    assert os.path.join(root_a, "AGENT") != os.path.join(root_b, "AGENT")


def test_a_linked_worktree_shares_the_main_checkouts_state(monkeypatch, tmp_path):
    main = _make_repo(tmp_path / "main")
    wt_gitdir = main / ".git" / "worktrees" / "feat"
    wt_gitdir.mkdir(parents=True)
    (wt_gitdir / "commondir").write_text("../..\n")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: {}\n".format(wt_gitdir))
    from_main = _common(monkeypatch, tmp_path, project_dir=main).state_root()
    from_wt = _common(monkeypatch, tmp_path, project_dir=worktree).state_root()
    assert from_wt == from_main


def test_a_subdirectory_resolves_to_the_enclosing_repository(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "repo")
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    thc = _common(monkeypatch, tmp_path, project_dir=sub)
    assert thc.git_common_dir() == os.path.realpath(str(repo / ".git"))


def test_explicit_override_wins_and_expands_home(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "repo")
    thc = _common(monkeypatch, tmp_path, project_dir=repo, override="~/elsewhere")
    assert thc.state_root() == str(tmp_path / "home" / "elsewhere")


def test_without_a_repository_state_falls_back_to_the_log_dir(monkeypatch, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    thc = _common(monkeypatch, tmp_path, project_dir=plain)
    if thc.git_common_dir() is not None:      # tmp_path itself sits inside a repo
        import pytest
        pytest.skip("temporary directory is inside a git repository")
    assert thc.state_root() == thc.log_dir()


def test_the_audit_log_stays_in_the_home_log_dir(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "repo")
    thc = _common(monkeypatch, tmp_path, project_dir=repo)
    assert thc.log_path() == str(tmp_path / "home" / ".claude" / "logs" / "team-hooks.jsonl")


# --- Atlassian tool-name recognition ---------------------------------------

def test_jira_op_recognises_any_atlassian_server_name(monkeypatch, tmp_path):
    thc = _common(monkeypatch, tmp_path)
    for prefix in ("mcp__plugin_atlassian_atlassian__", "mcp__plugin_engineering_atlassian__",
                   "mcp__atlassian__", "mcp__claude_ai_Atlassian__"):
        assert thc.jira_op(prefix + "transitionJiraIssue") == "transitionJiraIssue"


def test_jira_op_ignores_other_servers_and_non_mcp_tools(monkeypatch, tmp_path):
    thc = _common(monkeypatch, tmp_path)
    for name in ("mcp__github__transitionJiraIssue", "Write", "", None,
                 "transitionJiraIssue", "mcp__atlassian"):
        assert thc.jira_op(name) is None


def test_jira_op_folds_the_renamed_comment_tool(monkeypatch, tmp_path):
    thc = _common(monkeypatch, tmp_path)
    assert (thc.jira_op("mcp__atlassian__addCommentToJiraIssue")
            == thc.jira_op("mcp__atlassian__addOrEditJiraIssueComment")
            == "addOrEditJiraIssueComment")


# --- caller identity (shapes captured from the live harness) ---------------

# Field names exactly as delivered by Claude Code 2.1.319; values are synthetic.
MAIN_SESSION_PAYLOAD = {
    "session_id": "s-1", "prompt_id": "p-1", "cwd": "/repo", "permission_mode": "default",
    "effort": "high", "hook_event_name": "PreToolUse", "tool_use_id": "t-1",
    "transcript_path": "/x.jsonl", "mcp_server": {"name": "plugin:atlassian:atlassian"},
    "tool_name": "mcp__plugin_atlassian_atlassian__transitionJiraIssue",
    "tool_input": {"issueIdOrKey": "AG-5", "transitionId": "21"},
}


def _subagent_payload(spawn_name):
    return dict(MAIN_SESSION_PAYLOAD, agent_id="a{}-cf0598ff321bb655".format(spawn_name),
                agent_type=spawn_name)


def test_a_call_without_agent_id_is_the_lead(monkeypatch, tmp_path):
    who = _common(monkeypatch, tmp_path).caller_identity(MAIN_SESSION_PAYLOAD)
    assert who == {"is_lead": True, "agent_id": None, "name": None, "role": None}


def test_a_subagent_is_identified_by_its_spawn_name(monkeypatch, tmp_path):
    who = _common(monkeypatch, tmp_path).caller_identity(_subagent_payload("review-1"))
    assert who["is_lead"] is False
    assert who["name"] == "review-1"
    assert who["role"] == "review"
    assert who["agent_id"].startswith("areview-1-")


def test_a_worker_with_an_unrecognised_name_has_no_role(monkeypatch, tmp_path):
    who = _common(monkeypatch, tmp_path).caller_identity(_subagent_payload("payload-probe"))
    assert who["is_lead"] is False and who["role"] is None


def test_teammate_name_is_preferred_when_present(monkeypatch, tmp_path):
    payload = dict(_subagent_payload("general-purpose"), teammate_name="coding-c3-2")
    who = _common(monkeypatch, tmp_path).caller_identity(payload)
    assert who["name"] == "coding-c3-2" and who["role"] == "coding"


def test_identity_cannot_be_claimed_through_tool_arguments(monkeypatch, tmp_path):
    payload = dict(MAIN_SESSION_PAYLOAD)
    payload["tool_input"] = dict(payload["tool_input"], agent_type="review-1",
                                 agent_id="areview-1-x")
    payload = dict(_subagent_payload("coding-2"), tool_input=payload["tool_input"])
    who = _common(monkeypatch, tmp_path).caller_identity(payload)
    assert who["role"] == "coding"


def test_identity_is_total_on_junk(monkeypatch, tmp_path):
    thc = _common(monkeypatch, tmp_path)
    for junk in (None, [], "x", {"agent_id": 7}, {"agent_id": "  "}, {"agent_type": "review-1"}):
        assert thc.caller_identity(junk)["is_lead"] is True


# --- the audit log keeps decisions, not content (review nit N2) -------------

def _audit_records(home):
    path = os.path.join(str(home), ".claude", "logs", "team-hooks.jsonl")
    return [json.loads(line) for line in open(path).read().splitlines()]


def test_the_audit_log_never_stores_command_text_file_bodies_or_responses(monkeypatch, tmp_path):
    thc = _common(monkeypatch, tmp_path)
    thc.audit("PreToolUse", {
        "tool_name": "Bash", "session_id": "s1", "agent_id": "acoding-1-x",
        "agent_type": "coding-1",
        "tool_input": {"command": "AWS_SECRET_ACCESS_KEY=hunter2SECRET aws s3 ls",
                       "content": "BODY-OF-A-FILE", "file_path": "src/a.py"},
        "tool_response": {"data": "RESPONSE-BODY"}}, "allow", reason="aws-guard: read")
    raw = open(os.path.join(str(tmp_path / "home"), ".claude", "logs", "team-hooks.jsonl")).read()
    for secret in ("hunter2SECRET", "BODY-OF-A-FILE", "RESPONSE-BODY"):
        assert secret not in raw
    record = _audit_records(tmp_path / "home")[-1]
    assert record["decision"] == "allow" and record["reason"] == "aws-guard: read"
    view = record["payload"]
    assert view["tool_name"] == "Bash" and view["agent_type"] == "coding-1"
    assert view["tool_input"]["file_path"] == "src/a.py"
    assert view["tool_input"]["keys"] == ["command", "content", "file_path"]
    assert len(view["tool_input"]["sha256"]) == 16


def test_the_audit_log_keeps_what_identifies_a_jira_call(monkeypatch, tmp_path):
    thc = _common(monkeypatch, tmp_path)
    thc.audit("PreToolUse", {"tool_name": "mcp__atlassian__transitionJiraIssue",
                             "tool_input": {"issueIdOrKey": "AG-7", "transitionId": "31",
                                            "fields": {"description": "LONG TEXT"}}}, "block")
    view = _audit_records(tmp_path / "home")[-1]["payload"]
    assert view["tool_input"]["issueIdOrKey"] == "AG-7"
    assert view["tool_input"]["transitionId"] == "31"
    assert "LONG TEXT" not in json.dumps(view)


def test_two_identical_calls_share_a_digest_and_different_ones_do_not(monkeypatch, tmp_path):
    thc = _common(monkeypatch, tmp_path)
    a = thc.audit_view({"tool_input": {"command": "ls"}})["tool_input"]["sha256"]
    b = thc.audit_view({"tool_input": {"command": "ls"}})["tool_input"]["sha256"]
    c = thc.audit_view({"tool_input": {"command": "ls -la"}})["tool_input"]["sha256"]
    assert a == b != c


def test_the_audit_view_is_total_on_junk(monkeypatch, tmp_path):
    thc = _common(monkeypatch, tmp_path)
    for junk in (None, [], "x", {"tool_input": "not json"}, {"tool_input": 7},
                 {"tool_input": {"file_path": "y" * 5000}}):
        view = thc.audit_view(junk)
        assert isinstance(view, dict)
        assert len(json.dumps(view)) < 2000


# --- review cycle 4: identity, not spelling ----------------------------------

def test_respell_under_rewrites_a_path_that_is_under_the_root_by_identity(tmp_path):
    import pytest
    import team_hook_common as common
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    # already under the root as written, or nowhere near it: unchanged
    assert common.respell_under(str(root / "a" / "f.py"), str(root)) == str(root / "a" / "f.py")
    assert common.respell_under(str(tmp_path / "other" / "f.py"), str(root)) == str(
        tmp_path / "other" / "f.py")
    assert common.respell_under(str(root / "x"), str(tmp_path / "missing")) == str(root / "x")
    alias = "/System/Volumes/Data" + os.path.realpath(str(root))
    try:
        usable = os.path.samefile(alias, str(root)) and os.path.realpath(alias) == alias
    except OSError:
        usable = False
    if not usable:
        pytest.skip("no firmlink spelling of this path on this system")
    real_root = os.path.realpath(str(root))
    assert common.respell_under(alias + "/a/new/f.py", str(root)) == real_root + "/a/new/f.py"
    assert common.respell_under(alias, str(root)) == real_root


# --- review cycle 5 ----------------------------------------------------------

def test_write_private_replaces_a_link_and_never_writes_through_it(tmp_path):
    import team_hook_common as common
    victim = tmp_path / "victim.py"
    victim.write_text("original\n")
    target = tmp_path / "state" / "f.json"
    target.parent.mkdir()
    os.symlink(victim, target)
    assert common.write_private(str(target), "new") is True
    assert victim.read_text() == "original\n" and target.read_text() == "new"
    assert not os.path.islink(target)
    os.remove(target)
    os.link(victim, target)
    assert common.write_private(str(target), "newer") is True
    assert victim.read_text() == "original\n" and target.read_text() == "newer"


def test_write_private_refuses_a_directory_outside_its_bounds(tmp_path):
    import team_hook_common as common
    outside = tmp_path / "hooks"
    outside.mkdir()
    base = tmp_path / "state"
    base.mkdir()
    os.symlink(outside, base / "claim")
    assert common.write_private(str(base / "claim" / "heartbeat"), "x", within=str(base)) is False
    assert os.listdir(outside) == []
    assert common.write_private(str(base / "real" / "heartbeat"), "x", within=str(base)) is True


def test_append_private_refuses_links_pipes_and_second_names(tmp_path):
    import team_hook_common as common
    victim = tmp_path / "victim.py"
    victim.write_text("original\n")
    log = tmp_path / "log.jsonl"
    assert common.append_private(str(log), "a\n") and common.append_private(str(log), "b\n")
    assert log.read_text() == "a\nb\n"
    link = tmp_path / "link.jsonl"
    os.symlink(victim, link)
    assert common.append_private(str(link), "x") is False
    hard = tmp_path / "hard.jsonl"
    os.link(victim, hard)
    assert common.append_private(str(hard), "x") is False
    assert victim.read_text() == "original\n"
    pipe = tmp_path / "pipe.jsonl"
    os.mkfifo(pipe)
    assert common.append_private(str(pipe), "x") is False


def test_read_small_reads_only_a_small_regular_file(tmp_path):
    import team_hook_common as common
    f = tmp_path / "owner"
    f.write_text("coding-1\n")
    assert common.read_small(str(f)) == "coding-1\n"
    link = tmp_path / "link"
    os.symlink(f, link)
    assert common.read_small(str(link)) is None
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    assert common.read_small(str(pipe)) is None
    f.write_text("x" * 5000)
    assert common.read_small(str(f)) is None
    assert common.read_small(str(tmp_path / "missing")) is None


def test_unfirm_gives_the_short_spelling_only_for_the_same_place(tmp_path):
    import pytest
    import team_hook_common as common
    assert common.unfirm("/usr/lib/x") == "/usr/lib/x"
    assert common.unfirm("/System/Volumes/Data/definitely/not/here") == (
        "/System/Volumes/Data/definitely/not/here")
    real = os.path.realpath(str(tmp_path))
    alias = "/System/Volumes/Data" + real
    try:
        usable = os.path.samefile(alias, real)
    except OSError:
        usable = False
    if not usable:
        pytest.skip("no firmlink spelling of this path on this system")
    assert common.unfirm(alias) == real
    assert common.unfirm(alias + "/not/yet/created.py") == real + "/not/yet/created.py"
    assert common.real_path(alias + "/x") == real + "/x"
    assert common.canonical_path(alias + "/x", fold=False) == real + "/x"
    assert common.git_common_dir(alias) == common.git_common_dir(real)
