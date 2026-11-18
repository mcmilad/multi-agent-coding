"""The prose an agent follows must describe the gates as they are implemented.

A hook can be perfect and still be defeated by a document that teaches the old
procedure: an agent told to `echo PASSED > …verified` would be refused by the
gate and, worse, would learn that the gate is something to write a file at.
"""
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = [
    ".claude/rules/agent-team-protocol.md",
    ".claude/rules/spec-and-jira-required.md",
    ".claude/skills/jira-workflow/SKILL.md",
    ".claude/skills/spec-workflow/SKILL.md",
    ".claude/skills/documentation/SKILL.md",
    ".claude/agents/fullstack-agent.md",
    ".claude/agents/coding-agent.md",
    ".claude/agents/devops-agent.md",
    ".claude/agents/review-agent.md",
    ".claude/agents/sa-agent.md",
    "commands/plan-it.md",
    "commands/ship-it.md",
    "README.md",
]
PROCEDURE_DOCS = DOCS[:1] + DOCS[2:3] + DOCS[5:10]


def read(rel):
    return open(os.path.join(REPO, rel), encoding="utf-8").read()


def test_no_doc_points_at_the_old_state_location():
    for rel in DOCS:
        stale = re.findall(r"~/\.claude/logs/(?:claims|verified|locks|jira-mirror)", read(rel))
        assert not stale, "{} still names {}".format(rel, sorted(set(stale)))


def test_no_doc_teaches_writing_the_evidence_file_by_hand():
    for rel in DOCS:
        text = read(rel)
        assert not re.search(r"echo\s+[\"'][^\"'\n]*(PASSED|PASS)[\"']\s*>\s*\S*\.verified", text), (
            "{} tells the agent to write the evidence file itself; the gate refuses that".format(rel))


@pytest.mark.parametrize("rel", PROCEDURE_DOCS)
def test_every_procedure_doc_names_the_verification_script(rel):
    assert "scripts/verify_issue.py" in read(rel), rel


def test_every_shell_block_that_uses_the_state_directory_defines_it():
    for rel in DOCS:
        for block in re.findall(r"```bash\n(.*?)```", read(rel), re.S):
            if "$STATE" in block:
                assert "STATE=" in block, "{}: a bash block uses $STATE without defining it:\n{}".format(
                    rel, block[:200])


def test_no_doc_uses_the_old_atlassian_tool_or_field_names():
    for rel in DOCS:
        text = read(rel)
        for old in ("addCommentToJiraIssue", "getTransitionsForJiraIssue", "transition.id ="):
            assert old not in text, "{} still uses {}".format(rel, old)


def test_the_rule_no_longer_calls_enforced_things_conventions():
    text = " ".join(read(".claude/rules/agent-team-protocol.md").split())
    for stale in ("The gate checks only that a sentinel exists", "not machine-enforced",
                  "The hook cannot see who you are", "blocks once per session"):
        assert stale not in text, stale
    for present in ("The hook runs no project tool itself", "only when the lead", "blocked every time",
                    "the caller is a reviewer or the lead"):
        assert present in text, present


def test_the_skill_states_closing_is_enforced_and_names_its_one_remaining_gap():
    text = read(".claude/skills/jira-workflow/SKILL.md")
    assert "**This is enforced**" in text
    assert "cannot be told from a solo run" in text, "the lead exception must be stated, not hidden"
    assert "Not enforced." not in text


def test_the_always_on_rules_stay_within_a_size_budget():
    """Rules load into every session. This is a ceiling, not a target."""
    total = 0
    rules = os.path.join(REPO, ".claude", "rules")
    for name in os.listdir(rules):
        total += len(open(os.path.join(rules, name), encoding="utf-8").read().splitlines())
    assert total <= 620, "always-on rules have grown to {} lines".format(total)


@pytest.mark.parametrize("rel", [d for d in DOCS if d.startswith(".claude/skills/")])
def test_skills_do_not_name_plugins_outside_the_manifest(rel):
    text = read(rel)
    for gone in ("deploy-on-aws", "databases-on-aws", "aws-amplify", "aws-data-analytics"):
        assert gone not in text, "{} still names {}".format(rel, gone)


# --- AG-18: the hook runs static gates; the tests run in verify_issue.py ------

def test_no_doc_says_the_hook_runs_the_test_suite():
    for rel in DOCS:
        text = " ".join(read(rel).split())
        stale = re.findall(r"runs the project's standard gates|lint and the test suite\) \*\*itself",
                           text)
        assert not stale, "{} still describes hook-run tests: {}".format(rel, stale)


def test_no_doc_says_a_hook_runs_a_project_tool():
    for rel in DOCS:
        text = " ".join(read(rel).split())
        stale = re.findall(r"static gates \(lint\) itself|hook-run (?:gate|lint)s? (?:is|are) |"
                           r"readsNoTreeConfig|The hook runs these itself", text)
        assert not stale, "{} still describes hook-run gates: {}".format(rel, stale)


def test_no_agent_file_says_a_hand_written_record_is_refused():
    """A hand-written note is refused. A hand-written RECORD with the right
    command and hash is not, and the documents must not claim otherwise."""
    for rel in DOCS:
        assert "hand-written record" not in read(rel), rel


def test_the_docs_state_what_hooks_are_for_and_what_enforces():
    readme = " ".join(read("README.md").split())
    for boundary in ("Bash sandbox", "Branch protection", "CODEOWNERS", "AWS credentials",
                     "not** the security boundary", "is the authority"):
        assert boundary in readme, boundary
    rule = " ".join(read(".claude/rules/agent-team-protocol.md").split())
    assert "They catch mistakes" in rule and "four boundaries no agent can write" in rule
    assert "operator's to set up" in rule, "the rule must not imply CI is already required"


def test_the_protocol_and_readme_say_where_each_kind_of_check_runs():
    for rel in (".claude/rules/agent-team-protocol.md", "README.md",
                ".claude/skills/jira-workflow/SKILL.md"):
        text = read(rel)
        assert "agentChecks" in text and "verify_issue.py" in text, rel


def test_the_docs_say_agents_rules_and_skills_need_a_maintenance_session():
    for rel in (".claude/rules/agent-team-protocol.md", "README.md",
                ".claude/agents/fullstack-agent.md"):
        text = " ".join(read(rel).split())
        assert re.search(r"agent definitions, rules(,| and) skills", text), rel


# --- local-ci spec: the gate is described once, and the lead re-checks the commit ---

WORKER_AGENT_FILES = [".claude/agents/coding-agent.md", ".claude/agents/devops-agent.md",
                      ".claude/agents/sa-agent.md", ".claude/agents/review-agent.md"]


@pytest.mark.parametrize("rel", WORKER_AGENT_FILES)
def test_agent_files_point_at_the_rule_instead_of_restating_the_gate(rel):
    """Nine documents each described the gate; every change to it left some of
    them wrong. The mechanism lives in the protocol rule and nowhere else."""
    text = " ".join(read(rel).split())
    assert "scripts/verify_issue.py" in text
    assert "rules/agent-team-protocol.md` → \"Verification gate\"" in text, rel
    for internal in ("the gate refuses", "evidence bound to", "tree hash", "agentChecks",
                     "hand-written", "CI on the pull request re-runs"):
        assert internal not in text, "{} restates the gate: {!r}".format(rel, internal)


def test_the_rule_is_where_the_gate_is_described():
    rule = " ".join(read(".claude/rules/agent-team-protocol.md").split())
    for needed in ("### 2. Verification gate", "agentChecks", "file hash matches",
                   "An empty file, a hand-written note"):
        assert needed in rule, needed


def test_the_lead_is_told_to_run_the_ci_checks_on_the_committed_tree():
    lead = read(".claude/agents/fullstack-agent.md")
    assert "scripts/ci_local.py" in lead and "before closing the Epic" in lead
    readme = " ".join(read("README.md").split())
    assert "scripts/ci_local.py" in readme and "not an enforcement boundary" in readme
