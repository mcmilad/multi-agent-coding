"""guardrail_guard: agents do not edit the harness's own guardrails outside a maintenance session."""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(REPO, ".claude", "hooks", "guardrail_guard.py")

PROTECTED = [".claude/hooks/claim_gate.py", ".claude/hooks/new_hook.py", ".claude/settings.json",
             ".claude/settings.local.json", ".claude/gates.json", ".claude/capabilities.json",
             ".claude/aws-sandbox.json", ".claude/jira-config.json", ".github/workflows/ci.yml",
             "scripts/harness_admin.py", ".claude/agents/review-agent.md",
             ".claude/rules/agent-team-protocol.md", ".claude/skills/jira-workflow/SKILL.md",
             ".git/config", ".git/commondir", ".git/hooks/pre-commit"]
FREE = ["src/app.py", "tests/test_x.py", "README.md", ".claude/specs/x/spec.md",
        ".claude/hooksmith.py", ".claude/settings.json.bak", "docs/hooks/readme.md",
        "scripts/doctor.py", ".claude/agentsmith/notes.md", "rules/readme.md"]


def setup(tmp_path, maintenance=None):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    env = dict(os.environ, HOME=str(tmp_path / "home"), CLAUDE_PROJECT_DIR=str(project))
    env.pop("CLAUDE_HARNESS_MAINTENANCE", None)
    if maintenance is not None:
        env["CLAUDE_HARNESS_MAINTENANCE"] = maintenance
    return env, project


def run(env, path, tool="Edit", agent=None):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    payload = {"tool_name": tool, "tool_input": {key: str(path)}}
    if agent:
        payload.update(agent_id="a{}-01".format(agent), agent_type=agent)
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


@pytest.mark.parametrize("rel", PROTECTED)
def test_guardrail_files_are_blocked_in_an_ordinary_session(tmp_path, rel):
    env, project = setup(tmp_path)
    for tool in ("Write", "Edit"):
        proc = run(env, project / rel, tool=tool)
        assert proc.returncode == 2, (rel, tool)
        assert "CLAUDE_HARNESS_MAINTENANCE=1" in proc.stderr
        assert "Do not look for another way" in proc.stderr


@pytest.mark.parametrize("rel", FREE)
def test_ordinary_files_are_never_blocked(tmp_path, rel):
    env, project = setup(tmp_path)
    assert run(env, project / rel).returncode == 0, rel


@pytest.mark.parametrize("rel", [".claude/hooks/claim_gate.py", ".claude/settings.json",
                                 ".claude/agents/review-agent.md", ".git/config"])
def test_a_maintenance_session_may_edit_guardrail_files(tmp_path, rel):
    env, project = setup(tmp_path, maintenance="1")
    assert run(env, project / rel).returncode == 0, rel


@pytest.mark.parametrize("value", ["0", "", "off", "no", "false"])
def test_maintenance_must_be_switched_on_explicitly(tmp_path, value):
    env, project = setup(tmp_path, maintenance=value)
    assert run(env, project / ".claude/settings.json").returncode == 2


def test_approval_markers_are_never_editable_even_in_maintenance(tmp_path):
    for maintenance in (None, "1"):
        env, project = setup(tmp_path, maintenance=maintenance)
        proc = run(env, project / ".claude/approvals/abc123.approved", tool="Write")
        assert proc.returncode == 2
        assert "human decision" in proc.stderr


def test_the_operator_decision_directory_is_never_writable(tmp_path):
    human = tmp_path / "human"
    for maintenance in (None, "1"):
        env, project = setup(tmp_path, maintenance=maintenance)
        env["CLAUDE_HARNESS_HUMAN_DIR"] = str(human)
        for target in (human / "approvals" / "0123456789abcdef.approved",
                       human / "aws-sandbox.json"):
            proc = run(env, target, tool="Write")
            assert proc.returncode == 2, (target, maintenance)
            assert "operator decisions" in proc.stderr
    env, project = setup(tmp_path)
    home_default = tmp_path / "home" / ".claude" / "harness" / "anyrepo" / "aws-sandbox.json"
    assert run(env, home_default, tool="Write").returncode == 2


def test_it_applies_to_the_lead_and_to_every_worker(tmp_path):
    env, project = setup(tmp_path)
    for agent in (None, "coding-1", "devops-1", "review-1"):
        assert run(env, project / ".claude/gates.json", agent=agent).returncode == 2, agent


def test_notebook_edit_is_covered(tmp_path):
    env, project = setup(tmp_path)
    assert run(env, project / ".claude/hooks/x.py", tool="NotebookEdit").returncode == 2


def test_a_symlinked_path_to_a_guardrail_file_is_blocked(tmp_path):
    env, project = setup(tmp_path)
    (project / ".claude").mkdir()
    link = tmp_path / "alias"
    os.symlink(project, link)
    assert run(env, link / ".claude" / "settings.json").returncode == 2


def test_the_copy_inside_a_worktree_is_protected_too(tmp_path):
    env, project = setup(tmp_path)
    for argv in (["git", "init", "-q"],
                 ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                  "commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(argv, cwd=project, check=True, capture_output=True)
    worktree = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(worktree), "-b", "b1"],
                   cwd=project, check=True, capture_output=True)
    assert run(env, worktree / ".claude" / "hooks" / "claim_gate.py").returncode == 2
    assert run(env, worktree / "src" / "app.py").returncode == 0


def test_files_outside_the_repository_and_other_tools_are_ignored(tmp_path):
    env, project = setup(tmp_path)
    assert run(env, tmp_path / "elsewhere" / ".claude" / "settings.json").returncode == 0
    assert run(env, project / ".claude/settings.json", tool="Read").returncode == 0
    proc = subprocess.run([sys.executable, HOOK], input="{nope", capture_output=True,
                          text=True, env=env)
    assert proc.returncode == 0


def test_a_block_is_recorded_in_the_audit_log(tmp_path):
    env, project = setup(tmp_path)
    run(env, project / ".claude/gates.json")
    log = open(os.path.join(str(tmp_path / "home"), ".claude", "logs", "team-hooks.jsonl")).read()
    assert ".claude/gates.json" in log and '"block"' in log


# --- review cycle 2, R2: letter case and final-component symlinks -----------

def _insensitive(tmp_path):
    probe = tmp_path / "CaseProbe"
    probe.write_text("x")
    return (tmp_path / "caseprobe").exists()


SHOUTED = [".claude/HOOKS/aws_guard.py", ".claude/Settings.json", ".Claude/gates.json",
           ".CLAUDE/AGENTS/review-agent.md", ".GitHub/Workflows/ci.yml",
           "Scripts/Harness_Admin.py"]


@pytest.mark.parametrize("rel", SHOUTED)
def test_another_spelling_of_a_guardrail_path_is_still_blocked(tmp_path, rel):
    """On a case-insensitive disk these ARE the guardrail files. The guard matched
    only the lower-case spelling and let every one of them through."""
    env, project = setup(tmp_path)
    proc = run(env, project / rel)
    assert proc.returncode == 2, rel
    assert "CLAUDE_HARNESS_MAINTENANCE=1" in proc.stderr


def test_a_symlink_as_the_final_component_is_followed(tmp_path):
    """Only the directory part used to be resolved, so a link to a guardrail file
    was judged by the link's own harmless-looking path."""
    env, project = setup(tmp_path)
    (project / ".claude" / "hooks").mkdir(parents=True)
    target = project / ".claude" / "hooks" / "aws_guard.py"
    target.write_text("# guard\n")
    (project / "notes").mkdir()
    link = project / "notes" / "scratch.py"
    os.symlink(target, link)
    proc = run(env, link)
    assert proc.returncode == 2 and ".claude/hooks/" in proc.stderr


def test_a_symlink_into_the_operator_directory_is_blocked(tmp_path):
    env, project = setup(tmp_path)
    human = tmp_path / "human"
    (human / "approvals").mkdir(parents=True)
    env["CLAUDE_HARNESS_HUMAN_DIR"] = str(human)
    link = project / "innocent.txt"
    os.symlink(human / "approvals" / "0123456789abcdef.approved", link)
    proc = run(env, link, tool="Write")
    assert proc.returncode == 2 and "operator decisions" in proc.stderr


def test_another_spelling_of_the_operator_directory_is_blocked(tmp_path):
    """The self-approval chain from the review: a Write to ~/.claude/HARNESS/..."""
    env, project = setup(tmp_path)
    human = tmp_path / "human"
    env["CLAUDE_HARNESS_HUMAN_DIR"] = str(human)
    shouted = tmp_path / "HUMAN" / "approvals" / "0123456789abcdef.approved"
    for maintenance in (None, "1"):
        if maintenance:
            env["CLAUDE_HARNESS_MAINTENANCE"] = maintenance
        proc = run(env, shouted, tool="Write")
        if _insensitive(tmp_path):
            assert proc.returncode == 2, "same directory on this filesystem"
    home_shouted = tmp_path / "home" / ".claude" / "HARNESS" / "x" / "approvals" / "a.approved"
    assert run(env, home_shouted, tool="Write").returncode == 2


def test_agent_definitions_rules_and_skills_need_maintenance_too(tmp_path):
    """N3: an agent file's frontmatter is what denies the reviewer its editing
    tools; leaving it writable left that control writable."""
    for rel in (".claude/agents/coding-agent.md", ".claude/rules/untrusted-content.md",
                ".claude/skills/spec-workflow/SKILL.md"):
        env, project = setup(tmp_path)
        assert run(env, project / rel).returncode == 2, rel
        env, project = setup(tmp_path, maintenance="1")
        assert run(env, project / rel).returncode == 0, rel


# --- review cycle 3: a hard link is another name for the same file -----------

def _hard_link(source, link):
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, link)
    except OSError as exc:              # a sandbox or filesystem that forbids it
        pytest.skip("cannot create a hard link here: {}".format(exc))


def test_a_hard_link_to_a_guardrail_file_is_that_file(tmp_path):
    """`ln .claude/hooks/aws_guard.py docs/x.py`: nothing for realpath to resolve,
    and every byte written to docs/x.py lands in the hook."""
    env, project = setup(tmp_path)
    (project / ".claude" / "hooks").mkdir(parents=True)
    target = project / ".claude" / "hooks" / "aws_guard.py"
    target.write_text("# guard\n")
    link = project / "docs" / "tmp3" / "hardlink.py"
    _hard_link(target, link)
    for tool in ("Write", "Edit"):
        proc = run(env, link, tool=tool)
        assert proc.returncode == 2, tool
        assert "CLAUDE_HARNESS_MAINTENANCE=1" in proc.stderr and ".claude/hooks/" in proc.stderr


@pytest.mark.parametrize("rel", [".claude/settings.json", ".claude/gates.json",
                                 ".claude/agents/review-agent.md",
                                 ".claude/rules/untrusted-content.md",
                                 ".github/workflows/ci.yml", "scripts/harness_admin.py"])
def test_a_hard_link_to_any_protected_file_is_blocked(tmp_path, rel):
    env, project = setup(tmp_path)
    target = project / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x\n")
    link = tmp_path / "elsewhere" / "alias.txt"        # outside the project entirely
    _hard_link(target, link)
    assert run(env, link, tool="Write").returncode == 2, rel


def test_a_hard_link_to_an_operator_record_is_blocked_in_any_mode(tmp_path):
    env, project = setup(tmp_path, maintenance="1")
    human = tmp_path / "human"
    (human / "approvals").mkdir(parents=True)
    env["CLAUDE_HARNESS_HUMAN_DIR"] = str(human)
    record = human / "aws-sandbox.json"
    record.write_text("{}")
    link = project / "notes.json"
    _hard_link(record, link)
    proc = run(env, link, tool="Write")
    assert proc.returncode == 2 and "operator decisions" in proc.stderr


def test_a_symlink_to_a_hard_link_of_a_guardrail_file_is_blocked(tmp_path):
    env, project = setup(tmp_path)
    (project / ".claude" / "hooks").mkdir(parents=True)
    target = project / ".claude" / "hooks" / "claim_gate.py"
    target.write_text("# gate\n")
    hard = project / "docs" / "h.py"
    _hard_link(target, hard)
    soft = project / "s.py"
    os.symlink(hard, soft)
    assert run(env, soft).returncode == 2


def test_a_maintenance_session_may_still_edit_through_a_hard_link(tmp_path):
    env, project = setup(tmp_path, maintenance="1")
    (project / ".claude" / "hooks").mkdir(parents=True)
    target = project / ".claude" / "hooks" / "aws_guard.py"
    target.write_text("# guard\n")
    link = project / "docs" / "h.py"
    _hard_link(target, link)
    assert run(env, link).returncode == 0


def test_an_ordinary_file_with_several_links_is_not_blocked(tmp_path):
    env, project = setup(tmp_path)
    (project / ".claude" / "hooks").mkdir(parents=True)
    (project / ".claude" / "hooks" / "aws_guard.py").write_text("# guard\n")
    a = project / "src" / "a.py"
    a.parent.mkdir()
    a.write_text("x = 1\n")
    _hard_link(a, project / "src" / "b.py")
    assert run(env, a).returncode == 0
    assert run(env, project / "src" / "b.py").returncode == 0


def test_the_shell_sandbox_denies_writes_to_every_guardrail_path():
    """The Write/Edit guard is one layer; a shell redirect never reaches it. The
    sandbox's deny list is what covers Bash, and it has to name the same paths."""
    settings = json.load(open(os.path.join(REPO, ".claude", "settings.json")))
    denied = set(settings["sandbox"]["filesystem"]["denyWrite"])
    for path in ("./.claude/hooks", "./.claude/agents", "./.claude/rules", "./.claude/skills",
                 "./.claude/settings.json", "./.claude/settings.local.json",
                 "./.claude/gates.json", "./.claude/capabilities.json",
                 "./.claude/jira-config.json", "./.claude/aws-sandbox.json",
                 "./.claude/approvals", "./.github/workflows", "./scripts/harness_admin.py",
                 "~/.claude/harness", "./.git/config", "./.git/commondir", "./.git/hooks"):
        assert path in denied, path
    sys.path.insert(0, os.path.join(REPO, ".claude", "hooks"))
    import guardrail_guard
    for entry in guardrail_guard.PROTECTED + guardrail_guard.HUMAN_ONLY:
        assert "./" + entry.rstrip("/") in denied, (
            "{} is protected from Write/Edit but writable from the shell".format(entry))
    # And the other way: what the shell is denied, the file tools are denied too.
    hook_only = "./.git/" + guardrail_guard.state_root.__globals__["STATE_SUBDIR"] + "/jira-mirror"
    assert hook_only in denied
    for path in denied:
        if not path.startswith("./") or path == hook_only:
            continue
        rel = path[2:]
        assert (guardrail_guard.covered(rel, guardrail_guard.PROTECTED)
                or guardrail_guard.covered(rel + "/x", guardrail_guard.PROTECTED)
                or guardrail_guard.covered(rel + "/x", guardrail_guard.HUMAN_ONLY)), (
            "{} is denied to the shell but open to Write/Edit".format(path))


# --- review cycle 4 ----------------------------------------------------------

def _repo_with_worktrees(tmp_path, env, project, names=("w1",)):
    for argv in (["git", "init", "-q"],
                 ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                  "commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(argv, cwd=project, check=True, capture_output=True)
    trees = []
    for name in names:
        tree = tmp_path / name
        subprocess.run(["git", "worktree", "add", "-q", str(tree), "-b", "b-" + name],
                       cwd=project, check=True, capture_output=True)
        (tree / ".claude" / "hooks").mkdir(parents=True)
        (tree / ".claude" / "hooks" / "aws_guard.py").write_text("# guard\n")
        trees.append(tree)
    return trees


def test_a_hard_link_to_a_worktrees_own_guardrail_copy_is_blocked(tmp_path):
    """The session sits in the main checkout; the link and its target are both
    in a linked worktree, whose copy becomes the real hook when the branch merges."""
    env, project = setup(tmp_path)
    (w1,) = _repo_with_worktrees(tmp_path, env, project)
    link = w1 / "docs" / "hl3.py"
    _hard_link(w1 / ".claude" / "hooks" / "aws_guard.py", link)
    assert run(env, w1 / ".claude" / "hooks" / "aws_guard.py").returncode == 2
    assert run(env, link).returncode == 2


def test_a_hard_link_from_one_worktree_to_anothers_guardrail_copy_is_blocked(tmp_path):
    env, project = setup(tmp_path)
    w1, w2 = _repo_with_worktrees(tmp_path, env, project, names=("w1", "w2"))
    link = w1 / "docs" / "other.py"
    _hard_link(w2 / ".claude" / "hooks" / "aws_guard.py", link)
    env_w1 = dict(env, CLAUDE_PROJECT_DIR=str(w1))
    assert run(env_w1, link).returncode == 2
    assert run(env, link).returncode == 2


def test_gits_own_configuration_and_hooks_need_maintenance(tmp_path):
    env, project = setup(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True, capture_output=True)
    for rel in (".git/config", ".git/hooks/pre-commit", ".git/commondir"):
        assert run(env, project / rel, tool="Write").returncode == 2, rel
    assert run(env, project / ".git" / "info" / "exclude", tool="Write").returncode == 0


def test_the_jira_mirror_is_never_written_with_a_file_tool(tmp_path):
    """One appended line saying the lead applied skip-verify opened the gate."""
    for maintenance in (None, "1"):
        env, project = setup(tmp_path, maintenance=maintenance)
        state = tmp_path / "state"
        env["CLAUDE_HARNESS_STATE_DIR"] = str(state)
        mirror = state / "jira-mirror" / "AG.jsonl"
        for tool in ("Write", "Edit"):
            proc = run(env, mirror, tool=tool)
            assert proc.returncode == 2, (maintenance, tool)
            assert "Only the journal hook writes it" in proc.stderr
        # the rest of the coordination state stays the agent's to write
        assert run(env, state / "claims" / "AG" / "AG-1" / "owner").returncode == 0


def test_the_mirror_in_the_git_directory_is_covered_by_default(tmp_path):
    env, project = setup(tmp_path)
    env.pop("CLAUDE_HARNESS_STATE_DIR", None)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True, capture_output=True)
    mirror = project / ".git" / "claude-harness" / "jira-mirror" / "AG.jsonl"
    assert run(env, mirror, tool="Write").returncode == 2
    link = project / "docs" / "m.jsonl"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("")
    _hard_link(mirror, link)
    assert run(env, link, tool="Edit").returncode == 2, "a hard link to the mirror is the mirror"


def _firmlink_spelling(path):
    """The same path through macOS's data-volume firmlink, or None off macOS."""
    alias = "/System/Volumes/Data" + os.path.realpath(str(path))
    try:
        if os.path.samefile(alias, str(path)) and os.path.realpath(alias) == alias:
            return alias
    except OSError:
        pass
    return None


def test_a_second_spelling_of_the_project_root_is_the_same_root(tmp_path):
    """realpath does not collapse a firmlink, so the long spelling of a hook file
    compared as a file outside the repository and was allowed."""
    env, project = setup(tmp_path)
    (project / ".claude" / "hooks").mkdir(parents=True)
    (project / ".claude" / "hooks" / "aws_guard.py").write_text("# guard\n")
    alias = _firmlink_spelling(project)
    if alias is None:
        pytest.skip("no firmlink spelling of this path on this system")
    assert run(env, os.path.join(alias, ".claude", "hooks", "aws_guard.py")).returncode == 2
    assert run(env, os.path.join(alias, ".claude", "hooks", "new.py"), tool="Write").returncode == 2
    assert run(env, os.path.join(alias, "src", "app.py")).returncode == 0


def test_a_second_spelling_of_the_operator_directory_is_blocked(tmp_path):
    env, project = setup(tmp_path)
    human = tmp_path / "human"
    (human / "approvals").mkdir(parents=True)
    env["CLAUDE_HARNESS_HUMAN_DIR"] = str(human)
    alias = _firmlink_spelling(human)
    if alias is None:
        pytest.skip("no firmlink spelling of this path on this system")
    assert run(env, os.path.join(alias, "approvals", "x.approved"), tool="Write").returncode == 2


# --- review cycle 5: the firmlink fix itself opened the guard -----------------

def _git_init(project):
    subprocess.run(["git", "init", "-q"], cwd=project, check=True, capture_output=True)


GUARDED_IN_A_CHECKOUT = [".claude/hooks/jira_transition_verify_gate.py", ".claude/settings.json",
                         ".claude/gates.json", ".claude/rules/x.md", "scripts/harness_admin.py",
                         ".git/config", ".git/hooks/pre-commit"]


@pytest.mark.parametrize("rel", GUARDED_IN_A_CHECKOUT)
def test_the_long_spelling_of_a_guardrail_file_in_a_git_checkout_is_blocked(tmp_path, rel):
    """Cycle 4's tests never ran `git init`, so they missed that in a real
    checkout the path's own root, in its long spelling, joined the list of roots
    and the path was respelled straight back to it -- and allowed."""
    env, project = setup(tmp_path)
    _git_init(project)
    (project / ".claude" / "hooks").mkdir(parents=True)
    (project / ".claude" / "hooks" / "jira_transition_verify_gate.py").write_text("# gate\n")
    alias = _firmlink_spelling(project)
    if alias is None:
        pytest.skip("no firmlink spelling of this path on this system")
    for tool in ("Write", "Edit"):
        assert run(env, os.path.join(alias, rel), tool=tool, agent="coding-agent").returncode == 2, (
            rel, tool)
    assert run(env, os.path.join(alias, "src", "app.py")).returncode == 0


def test_the_long_spelling_of_the_mirror_in_a_git_checkout_is_blocked(tmp_path):
    env, project = setup(tmp_path)
    env.pop("CLAUDE_HARNESS_STATE_DIR", None)
    _git_init(project)
    mirror = project / ".git" / "claude-harness" / "jira-mirror" / "AG.jsonl"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("")
    alias = _firmlink_spelling(project)
    if alias is None:
        pytest.skip("no firmlink spelling of this path on this system")
    long_mirror = os.path.join(alias, ".git", "claude-harness", "jira-mirror", "AG.jsonl")
    for maintenance in (None, "1"):
        env2 = dict(env)
        if maintenance:
            env2["CLAUDE_HARNESS_MAINTENANCE"] = maintenance
        assert run(env2, long_mirror, tool="Write").returncode == 2


def test_the_long_spelling_of_a_worktrees_guardrail_copy_is_blocked(tmp_path):
    env, project = setup(tmp_path)
    (w1,) = _repo_with_worktrees(tmp_path, env, project)
    alias = _firmlink_spelling(w1)
    if alias is None:
        pytest.skip("no firmlink spelling of this path on this system")
    assert run(env, os.path.join(alias, ".claude", "hooks", "aws_guard.py")).returncode == 2


def test_a_worktree_registered_under_its_long_spelling_does_not_open_the_guard(tmp_path):
    """`.git/worktrees/<name>/gitdir` is the worker's to write. A root injected
    there in another spelling must not become the spelling paths are judged in."""
    env, project = setup(tmp_path)
    _git_init(project)
    (project / ".claude" / "hooks").mkdir(parents=True)
    (project / ".claude" / "hooks" / "aws_guard.py").write_text("# guard\n")
    alias = _firmlink_spelling(project)
    if alias is None:
        pytest.skip("no firmlink spelling of this path on this system")
    fake = project / ".git" / "worktrees" / "evil"
    fake.mkdir(parents=True)
    (fake / "gitdir").write_text(os.path.join(alias, ".git") + "\n")
    for spelling in (str(project), alias):
        assert run(env, os.path.join(spelling, ".claude", "hooks", "aws_guard.py")).returncode == 2
        assert run(env, os.path.join(spelling, ".claude", "settings.json")).returncode == 2


def test_a_fifo_in_the_worktree_registry_does_not_hang_the_guard(tmp_path):
    env, project = setup(tmp_path)
    _git_init(project)
    fake = project / ".git" / "worktrees" / "stall"
    fake.mkdir(parents=True)
    os.mkfifo(fake / "gitdir")
    payload = {"tool_name": "Write",
               "tool_input": {"file_path": str(project / ".claude" / "settings.json")}}
    proc = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=20)
    assert proc.returncode == 2
    payload["tool_input"]["file_path"] = str(project / "src" / "app.py")
    proc = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=20)
    assert proc.returncode == 0
