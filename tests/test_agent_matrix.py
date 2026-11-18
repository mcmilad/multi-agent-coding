"""Every agent definition is least-privilege, bounded, and says how it uses AWS.

Field names are the ones documented for Claude Code subagent files: an unknown field
is silently ignored by the harness, so a typo here would be a restriction that
looks present and is not.
"""
import os
import re

import pytest
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS_DIR = os.path.join(REPO, ".claude", "agents")
AGENTS = ("fullstack-agent", "coding-agent", "frontend-agent", "backend-agent", "devops-agent",
          "review-agent", "sa-agent")
WORKERS = AGENTS[1:]

DOCUMENTED_FIELDS = {
    "name", "description", "tools", "disallowedTools", "model", "permissionMode",
    "maxTurns", "autoCompactWindow", "skills", "mcpServers", "hooks", "memory",
    "background", "omitClaudeMd", "effort", "isolation", "color", "initialPrompt",
    "experimental",
}
FILE_EDIT_TOOLS = {"Write", "Edit", "NotebookEdit"}
AWS_DEPLOY_TOOLS = {
    "mcp__plugin_aws-serverless_aws-serverless-mcp__sam_deploy",
    "mcp__plugin_aws-serverless_aws-serverless-mcp__deploy_webapp",
    "mcp__plugin_aws-serverless_aws-serverless-mcp__update_webapp_frontend",
    "mcp__plugin_aws-serverless_aws-serverless-mcp__configure_domain",
}
AWS_MCP_TOOLS = ("search_documentation", "read_documentation",
                 "get_regional_availability", "run_script")
# Things earlier versions of these prompts told agents to use that do not exist
# in this configuration (see .claude/capabilities.json for what does).
NONEXISTENT = (
    "deploy-on-aws", "aws-amplify", "databases-on-aws", "aws-data-analytics",
    "superpowers:code-reviewer", "code-simplifier:code-simplifier", "call_aws",
    "`frontend-design` subagent", "awsknowledge", "dsql_", "awspricing", "awsiac",
)


def load(agent):
    text = open(os.path.join(AGENTS_DIR, agent + ".md"), encoding="utf-8").read()
    assert text.startswith("---\n"), "{}: frontmatter must open on the first line".format(agent)
    end = text.index("\n---\n", 4)
    return yaml.safe_load(text[4:end]), text[end + 5:]


def denied(front):
    value = front.get("disallowedTools") or []
    return set(value if isinstance(value, list) else [v.strip() for v in value.split(",")])


@pytest.mark.parametrize("agent", AGENTS)
def test_frontmatter_uses_only_documented_field_names(agent):
    front, _ = load(agent)
    unknown = set(front) - DOCUMENTED_FIELDS
    assert not unknown, "{}: undocumented fields are silently ignored: {}".format(agent, unknown)
    assert front["name"] == agent and front.get("description")


@pytest.mark.parametrize("agent", AGENTS)
def test_every_agent_has_a_turn_limit(agent):
    front, _ = load(agent)
    assert isinstance(front.get("maxTurns"), int) and 0 < front["maxTurns"] <= 400, agent


@pytest.mark.parametrize("agent", AGENTS)
def test_every_agent_restricts_its_tools(agent):
    front, _ = load(agent)
    assert front.get("tools") or front.get("disallowedTools"), (
        "{} inherits every tool; declare tools or disallowedTools".format(agent))


def test_the_reviewer_cannot_edit_files():
    front, _ = load("review-agent")
    assert FILE_EDIT_TOOLS <= denied(front)


def test_the_architect_keeps_only_what_its_review_file_needs():
    front, _ = load("sa-agent")
    assert "NotebookEdit" in denied(front)
    assert not {"Write", "Edit"} <= denied(front), "sa-agent writes sa-review.md"


def test_implementers_keep_their_editing_tools():
    for agent in ("coding-agent", "frontend-agent", "backend-agent", "devops-agent"):
        assert not ({"Write", "Edit", "Bash"} & denied(load(agent)[0])), agent


@pytest.mark.parametrize("agent", [a for a in AGENTS if a != "devops-agent"])
def test_only_devops_keeps_the_aws_deploy_tools(agent):
    assert AWS_DEPLOY_TOOLS <= denied(load(agent)[0]), agent


def test_devops_keeps_the_aws_deploy_tools():
    assert not (AWS_DEPLOY_TOOLS & denied(load("devops-agent")[0]))


def test_a_denied_tool_is_named_whole_never_with_a_specifier():
    """`Bash(git push *)` in disallowedTools removes ALL of Bash, per the docs."""
    for agent in AGENTS:
        for tool in denied(load(agent)[0]):
            assert "(" not in tool, "{}: {} would remove the whole tool".format(agent, tool)


@pytest.mark.parametrize("agent", WORKERS)
def test_workers_preload_the_protocol_skills(agent):
    front, _ = load(agent)
    assert {"jira-workflow", "spec-workflow"} <= set(front.get("skills") or [])


@pytest.mark.parametrize("agent", AGENTS)
def test_every_agent_is_told_how_to_use_the_aws_mcp_server(agent):
    _, body = load(agent)
    assert "aws-mcp" in body, agent
    for tool in AWS_MCP_TOOLS:
        assert "`{}`".format(tool) in body, "{} does not mention {}".format(agent, tool)
    assert "aws_guard.py" in body, "{} must know changes are guarded".format(agent)


def test_each_role_is_told_its_own_level_of_aws_access():
    assert "only role that changes AWS" in load("devops-agent")[1]
    for agent in ("coding-agent", "frontend-agent", "backend-agent", "review-agent", "sa-agent"):
        assert re.search(r"Your use of it is read-only", load(agent)[1]), agent
    lead = load("fullstack-agent")[1]
    assert "Your use of it is for design" in lead and "You do not deploy" in lead


def test_the_lead_knows_the_default_stack_and_the_capability_manifest():
    lead = load("fullstack-agent")[1]
    for phrase in ("serverless on AWS", "Python", "CDK or SAM", "React + TypeScript + Vite",
                   ".claude/capabilities.json", "scripts/doctor.py"):
        assert phrase in lead, phrase


@pytest.mark.parametrize("agent", AGENTS)
def test_no_agent_is_pointed_at_something_that_does_not_exist(agent):
    _, body = load(agent)
    found = [name for name in NONEXISTENT if name in body]
    assert not found, "{} references: {}".format(agent, found)


def test_the_reviewer_breaks_guards_in_a_scratch_copy_not_the_shared_tree():
    _, body = load("review-agent")
    assert "git worktree add --detach" in body and "never in the shared tree" in body
    assert "Break it on disk" not in body


def test_every_skill_named_in_a_prompt_is_one_the_manifest_can_supply():
    """A `plugin:skill` reference must come from a plugin the manifest lists."""
    import json
    manifest = json.load(open(os.path.join(REPO, ".claude", "capabilities.json")))
    plugins = {p["name"] for prof in manifest["profiles"].values()
               for p in prof.get("plugins", [])}
    builtin = {"superpowers"}          # optional workflow skills, not required by any role
    not_a_plugin = {"file"}            # `file:line` is a citation format, not a skill
    for agent in AGENTS:
        _, body = load(agent)
        for plugin in set(re.findall(r"`([a-z][a-z0-9-]+):[a-z][a-z0-9-]+`", body)):
            if plugin in not_a_plugin:
                continue
            assert plugin in plugins | builtin, (
                "{} names a skill from plugin '{}', which .claude/capabilities.json does "
                "not list".format(agent, plugin))


def test_every_role_has_an_agent_file_and_a_row_in_the_lead_pool_table():
    """Adding a role is three things: the ROLES tuple, <role>-agent.md, and the
    lead's pool table. This fails until all three agree."""
    import sys
    sys.path.insert(0, os.path.join(REPO, ".claude", "hooks"))
    import team_hook_common
    files = {n[:-len("-agent.md")] for n in os.listdir(AGENTS_DIR) if n.endswith("-agent.md")}
    assert set(team_hook_common.ROLES) == files - {"fullstack"}
    lead = load("fullstack-agent")[1]
    for role in team_hook_common.ROLES:
        assert "| `{}-agent` |".format(role) in lead, role


def test_new_instance_names_resolve_to_their_roles_and_others_do_not():
    import sys
    sys.path.insert(0, os.path.join(REPO, ".claude", "hooks"))
    from team_hook_common import role_of_teammate
    assert role_of_teammate("frontend-2") == "frontend"
    assert role_of_teammate("backend-agent") == "backend"
    assert role_of_teammate("frontendish") is None and role_of_teammate("back") is None


@pytest.mark.parametrize("role", ["frontend", "backend"])
def test_specialists_read_lessons_and_point_at_the_one_gate_description(role):
    body = load(role + "-agent")[1]
    assert "scripts/lessons.py show --role {}".format(role) in body
    assert "scripts/lessons.py add --roles {}".format(role) in body
    assert "rules/agent-team-protocol.md` → \"Verification gate\"" in body


ROLE_DOCS = (".claude/rules/agent-team-protocol.md", ".claude/skills/jira-workflow/SKILL.md",
             ".claude/skills/spec-workflow/SKILL.md", "README.md", "commands/plan-it.md")


@pytest.mark.parametrize("doc", ROLE_DOCS)
def test_every_documented_role_tag_list_names_every_role(doc):
    """A lead follows the docs: a tag list missing a role leaves that role's pool idle."""
    import sys
    sys.path.insert(0, os.path.join(REPO, ".claude", "hooks"))
    from team_hook_common import ROLES
    text = open(os.path.join(REPO, doc), encoding="utf-8").read()
    lists = [m.split("|") for m in re.findall(r"\[((?:[a-z]+\|)+[a-z]+)\]", text)
             if "coding" in m.split("|")]
    assert lists, "{} documents no role tag list".format(doc)
    for found in lists:
        assert set(found) == set(ROLES), "{} lists {}".format(doc, found)


def test_the_spawn_guard_rule_names_every_role_prefix():
    import sys
    sys.path.insert(0, os.path.join(REPO, ".claude", "hooks"))
    from team_hook_common import ROLES
    rule = open(os.path.join(REPO, ".claude", "rules", "agent-team-protocol.md"),
                encoding="utf-8").read()
    section = rule.split("### 12. Spawn guard", 1)[1].split("##", 1)[0]
    for role in ROLES:
        assert "`{}-*`".format(role) in section, role
    assert "named after no role" in section


def test_the_spawn_guard_message_reads_correctly(tmp_path):
    import json
    import subprocess
    import sys
    hook = os.path.join(REPO, ".claude", "hooks", "agent_spawn_guard.py")
    payload = {"tool_name": "Agent", "tool_input": {"name": "frontend-explorer", "prompt": "x"},
               "agent_id": "acoding-1-01", "agent_type": "coding-1"}
    proc = subprocess.run([sys.executable, hook], input=json.dumps(payload), capture_output=True,
                          text=True, env=dict(os.environ, HOME=str(tmp_path)))
    assert proc.returncode == 2 and "review, sa, the lead)" in proc.stderr


def _flat(agent):
    return " ".join(load(agent)[1].split())


@pytest.mark.parametrize("agent", ["backend-agent", "devops-agent"])
def test_builders_look_up_facts_and_consult_the_architect_on_design(agent):
    """Facts come from the AWS docs, cited; unsettled design goes to sa-agent, never guessed."""
    body = _flat(agent)
    assert "## AWS Questions: Look Up Facts, Consult on Design" in body
    for phrase in ("from the AWS documentation through `aws-mcp` and cite the page",
                   "`Source: <url>`", "never make it silently",
                   "`SendMessage` the running `sa-*` instance (the lead if there is none)",
                   "flag the issue as an impediment until the answer arrives",
                   "recorded in the spec's `decisions.md`"):
        assert phrase in body, (agent, phrase)


def test_the_architect_answers_consults_with_a_recorded_decision():
    body = _flat("sa-agent")
    assert "## Consult Duty" in body
    for phrase in ("a decision and its reasoning", "`rules/AWS-security-guidelines.md`",
                   "Never answer limits, pricing or behaviour from memory",
                   "Record it in the spec's `decisions.md`",
                   "A consult takes priority over your own `[sa]` issue"):
        assert phrase in body, phrase


def test_the_lead_keeps_an_architect_running_through_an_aws_build():
    body = _flat("fullstack-agent")
    assert "**Keep `sa-agent` running as consultant.**" in body
    assert "keep it alive until the last build group closes, even with no `[sa]` issue" in body
    assert "builders consult you instead" in body


def test_the_frontend_agent_audits_with_chrome_devtools_and_the_profile_lists_it():
    import json
    body = _flat("frontend-agent")
    for phrase in ("`chrome-devtools`", "`lighthouse_audit`", "performance trace",
                   "if the server is not connected, say so"):
        assert phrase in body, phrase
    caps = json.load(open(os.path.join(REPO, ".claude", "capabilities.json")))
    servers = {s["match"]: s for s in caps["profiles"]["frontend"]["mcpServers"]}
    assert servers["chrome-devtools"]["required"] is False


def test_the_documented_chrome_devtools_registration_is_pinned_and_locked_down():
    readme = open(os.path.join(REPO, "README.md"), encoding="utf-8").read()
    block = readme.split("claude mcp add chrome-devtools", 1)[1].split("```", 1)[0]
    assert "--scope local" in block and re.search(r"chrome-devtools-mcp@\d+\.\d+\.\d+ ", block)
    for flag in ("--isolated", "--headless", "--usageStatistics=false",
                 "--performanceCrux=false", "--fileNavigations=false",
                 "--allowedUrlPattern 'http://localhost:*/*'"):
        assert flag in block, flag
    assert "@latest" not in block
