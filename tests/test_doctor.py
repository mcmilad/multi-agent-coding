"""The preflight: each check reports a broken harness, and the doctor's exit code follows."""
import json
import os
import stat
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(REPO, ".claude", "hooks")
DOCTOR = os.path.join(REPO, "scripts", "doctor.py")
SELFTEST = os.path.join(HOOKS, "session_selftest.py")
sys.path.insert(0, HOOKS)
import selfcheck  # noqa: E402

GOOD_CONFIG = {"projectKey": "AG", "gatedStatuses": ["In Review", "Done"],
               "missingGatedTransitions": [],
               "transitions": {"21": "In Progress", "6": "In Review", "31": "Done"}}


def statuses(results):
    return {r.name: r.status for r in results}


def project(tmp_path, settings=None):
    root = tmp_path / "proj"
    (root / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "settings.json").write_text(json.dumps(
        {"env": {}, "hooks": {}, "permissions": {"deny": ["Read(./.env)"]},
         "sandbox": {"enabled": True}} if settings is None else settings))
    return root


# --- settings ---------------------------------------------------------------

def test_healthy_settings_pass(tmp_path):
    assert statuses(selfcheck.check_settings(str(project(tmp_path)))) == {"settings": "ok"}


def test_a_credential_in_settings_env_fails(tmp_path):
    root = project(tmp_path, {"env": {"JIRA_API_TOKEN": "x", "MAX_MCP_OUTPUT_TOKENS": "5"},
                              "permissions": {"deny": ["x"]}, "sandbox": {"enabled": True}})
    results = selfcheck.check_settings(str(root))
    assert statuses(results)["settings.env"] == "fail"
    assert "JIRA_API_TOKEN" in results[0].message and "MAX_MCP_OUTPUT_TOKENS" not in results[0].message


def test_a_wired_hook_that_does_not_exist_fails(tmp_path):
    root = project(tmp_path, {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [
        {"type": "command",
         "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/gone.py"'}]}]},
        "permissions": {"deny": ["x"]}, "sandbox": {"enabled": True}})
    results = selfcheck.check_settings(str(root))
    assert statuses(results)["settings.hooks"] == "fail" and "gone.py" in results[0].message


def test_missing_deny_rules_and_sandbox_are_warnings(tmp_path):
    got = statuses(selfcheck.check_settings(str(project(tmp_path, {"hooks": {}}))))
    assert got == {"settings.permissions": "warn", "settings.sandbox": "warn"}


def test_unreadable_settings_fail(tmp_path):
    root = project(tmp_path)
    (root / ".claude" / "settings.json").write_text("{nope")
    assert statuses(selfcheck.check_settings(str(root))) == {"settings": "fail"}


# --- environment ------------------------------------------------------------

def test_a_token_shaped_value_in_the_environment_fails_without_printing_it():
    token = "ATATT" + "x" * 40
    results = selfcheck.check_environment({"JIRA_API_TOKEN": token, "PATH": "/bin"})
    assert results[0].status == "fail" and "JIRA_API_TOKEN" in results[0].message
    assert token not in results[0].message


def test_credential_named_variables_warn_and_harness_ones_are_ignored():
    results = selfcheck.check_environment({
        "AWS_SECRET_ACCESS_KEY": "abc", "MAX_MCP_OUTPUT_TOKENS": "5",
        "CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "EMPTY_TOKEN": ""})
    assert [r.status for r in results] == ["warn"]
    assert "AWS_SECRET_ACCESS_KEY" in results[0].message
    for name in ("MAX_MCP_OUTPUT_TOKENS", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                 "EMPTY_TOKEN"):
        assert name not in results[0].message


def test_a_clean_environment_passes():
    assert selfcheck.check_environment({"PATH": "/bin", "HOME": "/h"})[0].status == "ok"


# --- jira config ------------------------------------------------------------

def _cfg(monkeypatch, tmp_path, config):
    path = tmp_path / "jira-config.json"
    if config is not None:
        path.write_text(json.dumps(config))
    monkeypatch.setenv("JIRA_CONFIG_PATH", str(path))


def test_a_complete_jira_config_passes(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path, GOOD_CONFIG)
    assert statuses(selfcheck.check_jira_config()) == {"jira-config": "ok"}


def test_a_missing_jira_config_fails(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path, None)
    assert selfcheck.check_jira_config()[0].status == "fail"


def test_an_empty_gate_set_fails(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path, dict(GOOD_CONFIG, gatedStatuses=[]))
    assert statuses(selfcheck.check_jira_config())["jira-config.gates"] == "fail"


def test_a_gated_status_no_transition_reaches_fails(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path, dict(GOOD_CONFIG, transitions={"21": "In Progress"}))
    results = selfcheck.check_jira_config()
    assert statuses(results)["jira-config.gates"] == "fail" and "Done" in results[0].message


def test_a_board_without_in_review_warns(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path, dict(GOOD_CONFIG, gatedStatuses=["Done"]))
    assert statuses(selfcheck.check_jira_config()) == {"jira-config.gates": "warn"}


# --- gates ------------------------------------------------------------------


def _gates(monkeypatch, tmp_path, content):
    path = tmp_path / "gates.json"
    if content is None:
        monkeypatch.setenv("CLAUDE_HARNESS_GATES_PATH", str(tmp_path / "missing.json"))
        return
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    monkeypatch.setenv("CLAUDE_HARNESS_GATES_PATH", str(path))


def test_configured_agent_checks_pass_the_preflight(monkeypatch, tmp_path):
    _gates(monkeypatch, tmp_path, {"agentChecks": [
        {"name": "tests", "command": ["{python}", "-m", "pytest", "-q"]}]})
    results = selfcheck.check_gates(str(tmp_path))
    assert statuses(results) == {"gates": "ok"} and "tests" in results[0].message


def test_leftover_hook_run_gates_fail_the_preflight(monkeypatch, tmp_path):
    _gates(monkeypatch, tmp_path, {"gates": [{"name": "lint", "command": ["ruff", "check", "."]}]})
    result = selfcheck.check_gates(str(tmp_path))[0]
    assert result.status == "fail" and "agentChecks" in result.message


def test_no_checks_is_a_warning_and_a_broken_file_a_failure(monkeypatch, tmp_path):
    _gates(monkeypatch, tmp_path, None)
    assert {r.status for r in selfcheck.check_gates(str(tmp_path))} == {"warn"}
    _gates(monkeypatch, tmp_path, "{broken")
    assert selfcheck.check_gates(str(tmp_path))[0].status == "fail"


# --- state and audit log ----------------------------------------------------

def test_a_missing_mirror_warns_and_a_fresh_one_passes(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _cfg(monkeypatch, tmp_path, GOOD_CONFIG)
    assert statuses(selfcheck.check_state()) == {"state.mirror": "warn"}
    mirror = tmp_path / "home" / ".claude" / "logs" / "jira-mirror"
    mirror.mkdir(parents=True)
    (mirror / "AG.jsonl").write_text("{}\n")
    assert statuses(selfcheck.check_state()) == {"state": "ok"}
    old = 3 * 24 * 3600
    os.utime(mirror / "AG.jsonl", (os.path.getmtime(mirror / "AG.jsonl") - old,) * 2)
    assert statuses(selfcheck.check_state()) == {"state.mirror": "warn"}


def test_hook_errors_in_the_audit_log_are_surfaced_without_payloads(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    logs = tmp_path / "home" / ".claude" / "logs"
    logs.mkdir(parents=True)
    records = [{"decision": "allow", "reason": "issue format ok", "payload": {"x": "SECRETISH"}},
               {"decision": "allow", "reason": "hook error (fail-open): name 'x' is not defined",
                "payload": {"x": "SECRETISH"}},
               {"decision": "block", "reason": "gate error (fail-closed): boom"}]
    (logs / "team-hooks.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\ngarbage\n")
    result = selfcheck.check_audit_log()[0]
    assert result.status == "warn" and "2 hook error(s)" in result.message
    assert "SECRETISH" not in result.message


def test_a_quiet_audit_log_passes(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert selfcheck.check_audit_log()[0].status == "ok"


# --- hooks ------------------------------------------------------------------

def test_every_hook_in_this_repository_starts_and_answers():
    result = selfcheck.check_hooks(REPO)[0]
    assert result.status == "ok", result.message


def test_a_hook_that_cannot_import_is_reported(tmp_path):
    root = project(tmp_path)
    (root / ".claude" / "hooks" / "bad.py").write_text(
        'import does_not_exist_xyz\nif __name__ == "__main__":\n    pass\n')
    (root / ".claude" / "hooks" / "fine.py").write_text(
        'import sys\nif __name__ == "__main__":\n    sys.exit(0)\n')
    (root / ".claude" / "hooks" / "library.py").write_text("raise RuntimeError('not run')\n")
    result = selfcheck.check_hooks(str(root))[0]
    assert result.status == "fail" and "bad.py" in result.message
    assert "fine.py" not in result.message and "library.py" not in result.message


# --- capabilities -----------------------------------------------------------

PLUGIN_LIST = """Installed plugins:

  > atlassian@claude-plugins-official
    Version: b74220d41142
    Scope: user
    Status: + enabled

  > context7@claude-plugins-official
    Version: c07c748f1c68
    Scope: project
    Status: + enabled

  > context7@context7-marketplace
    Version: 1.0.2
    Scope: user
    Status: + enabled

  > pr-review-toolkit@claude-plugins-official
    Version: 9.9.9
    Scope: project
    Status: x disabled
"""
MCP_LIST = """Checking MCP server health...

plugin:context7:context7: https://mcp.context7.com/mcp (HTTP) - + Connected
plugin:atlassian:atlassian: https://mcp.atlassian.com/v2/mcp (HTTP) - ! Needs authentication
plugin:playwright:playwright: npx @playwright/mcp@latest - x Failed to connect - ENOENT
plugin:aws-core:aws-mcp: uvx mcp-proxy-for-aws@1.6.4 https://aws-mcp.us-east-1.api.aws/mcp - + Connected
"""


def fake_claude(tmp_path, monkeypatch, plugins=PLUGIN_LIST, mcp=MCP_LIST, fail=False):
    script = tmp_path / "claude"
    if fail:
        script.write_text("#!/bin/sh\nexit 1\n")
    else:
        (tmp_path / "plugins.txt").write_text(plugins)
        (tmp_path / "mcp.txt").write_text(mcp)
        script.write_text("#!/bin/sh\ncase \"$1\" in\n  plugin) cat '{}' ;;\n  mcp) cat '{}' ;;\n"
                          "esac\n".format(tmp_path / "plugins.txt", tmp_path / "mcp.txt"))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLAUDE_HARNESS_CLAUDE_BIN", str(script))


def manifest(tmp_path, monkeypatch, profiles):
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps({"schema": 1, "profiles": profiles}))
    monkeypatch.setenv("CLAUDE_HARNESS_CAPABILITIES_PATH", str(path))


def test_plugin_and_server_output_is_parsed(tmp_path, monkeypatch):
    fake_claude(tmp_path, monkeypatch)
    plugins = selfcheck.installed_plugins()
    assert plugins["atlassian"]["enabled"] and plugins["atlassian"]["versions"] == {"b74220d41142"}
    assert plugins["context7"]["marketplaces"] == {"claude-plugins-official", "context7-marketplace"}
    assert plugins["pr-review-toolkit"]["enabled"] is False
    servers = selfcheck.connected_servers()
    assert servers["plugin:context7:context7"] == "connected"
    assert servers["plugin:atlassian:atlassian"] == "auth"
    assert servers["plugin:playwright:playwright"] == "failed"


def test_missing_required_capabilities_fail_and_optional_ones_warn(tmp_path, monkeypatch):
    fake_claude(tmp_path, monkeypatch)
    manifest(tmp_path, monkeypatch, {"core": {
        "plugins": [
            {"name": "atlassian", "marketplace": "m", "version": "b74220d41142", "required": True},
            {"name": "pr-review-toolkit", "marketplace": "m", "version": "9.9.9", "required": True},
            {"name": "not-installed", "marketplace": "m", "version": "1", "required": False},
            {"name": "context7", "marketplace": "m", "version": "0.0.1", "required": True}],
        "mcpServers": [
            {"match": "atlassian", "required": True, "why": "Jira"},
            {"match": "playwright", "required": False},
            {"match": "aws-mcp", "required": True},
            {"match": "nothing-like-this", "required": True}],
        "clis": [{"name": "git", "required": True},
                 {"name": "no-such-cli-xyz", "required": True},
                 {"name": "no-such-optional-cli-xyz", "required": False}]}})
    results = selfcheck.check_capabilities(("core",))
    got = {}
    for r in results:
        got.setdefault(r.name, []).append((r.status, r.message))
    assert "plugin.atlassian" not in got and "mcp.aws-mcp" not in got and "cli.git" not in got
    assert got["plugin.pr-review-toolkit"][0][0] == "fail"          # installed but disabled
    assert got["plugin.not-installed"][0][0] == "warn"
    assert any("0.0.1" in m for _s, m in got["plugin.context7"])     # version drift
    assert any("more than one marketplace" in m for _s, m in got["plugin.context7"])
    assert got["mcp.atlassian"][0][0] == "fail" and "authentication" in got["mcp.atlassian"][0][1]
    assert got["mcp.playwright"][0][0] == "warn"
    assert got["mcp.nothing-like-this"][0][0] == "fail"
    assert got["cli.no-such-cli-xyz"][0][0] == "fail"
    assert got["cli.no-such-optional-cli-xyz"][0][0] == "warn"


def test_an_item_is_required_if_any_selected_profile_requires_it(tmp_path, monkeypatch):
    fake_claude(tmp_path, monkeypatch)
    manifest(tmp_path, monkeypatch, {
        "core": {"clis": [{"name": "no-such-cli-xyz", "required": False}]},
        "frontend": {"clis": [{"name": "no-such-cli-xyz", "required": True}]}})
    results = selfcheck.check_capabilities(("core", "frontend"))
    assert statuses(results)["cli.no-such-cli-xyz"] == "fail"
    assert statuses(selfcheck.check_capabilities(("core",)))["cli.no-such-cli-xyz"] == "warn"


def test_an_unpinned_plugin_is_reported(tmp_path, monkeypatch):
    fake_claude(tmp_path, monkeypatch)
    manifest(tmp_path, monkeypatch, {"core": {"plugins": [
        {"name": "atlassian", "marketplace": "m", "version": None, "required": True}]}})
    result = selfcheck.check_capabilities(("core",))[0]
    assert result.status == "warn" and "no pinned version" in result.message


def test_an_unreachable_claude_cli_is_a_warning_not_a_pass(tmp_path, monkeypatch):
    fake_claude(tmp_path, monkeypatch, fail=True)
    manifest(tmp_path, monkeypatch, {"core": {"plugins": [
        {"name": "atlassian", "marketplace": "m", "version": "1", "required": True}]}})
    results = selfcheck.check_capabilities(("core",))
    assert [r.status for r in results] == ["warn"] and "could not be queried" in results[0].message


def test_unknown_profiles_and_a_missing_manifest_fail(tmp_path, monkeypatch):
    manifest(tmp_path, monkeypatch, {"core": {}})
    assert selfcheck.check_capabilities(("nope",), live=False)[0].status == "fail"
    monkeypatch.setenv("CLAUDE_HARNESS_CAPABILITIES_PATH", str(tmp_path / "absent.json"))
    assert selfcheck.check_capabilities(("core",), live=False)[0].status == "fail"


def test_the_repository_manifest_is_well_formed_and_names_the_essentials(monkeypatch):
    monkeypatch.delenv("CLAUDE_HARNESS_CAPABILITIES_PATH", raising=False)
    cfg, problem = selfcheck.load_capabilities(REPO)
    assert problem is None
    assert set(cfg["profiles"]) >= {"core", "serverless", "traditional", "agentic", "frontend"}
    core_servers = [s["match"] for s in cfg["profiles"]["core"]["mcpServers"] if s["required"]]
    assert "atlassian" in core_servers
    for profile in ("serverless", "traditional", "agentic"):
        required = [s["match"] for s in cfg["profiles"][profile]["mcpServers"] if s["required"]]
        assert "aws-mcp" in required, profile
    seen = {}
    for spec in cfg["profiles"].values():
        for plugin in spec.get("plugins", []):
            assert set(plugin) >= {"name", "marketplace", "version", "required"}
            assert seen.setdefault(plugin["name"], plugin["marketplace"]) == plugin["marketplace"], \
                "{} is listed from two marketplaces".format(plugin["name"])


# --- the doctor and the session hook ----------------------------------------

def _doctor_env(tmp_path, config=GOOD_CONFIG, **extra):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps(config))
    env = {k: v for k, v in os.environ.items() if k not in ("JIRA_API_TOKEN",)}
    env.update(HOME=str(tmp_path / "home"), JIRA_CONFIG_PATH=str(cfg),
               CLAUDE_PROJECT_DIR=REPO,
               CLAUDE_HARNESS_GATES_PATH=os.path.join(REPO, ".claude", "gates.json"))
    env.update(extra)
    return env


def test_the_doctor_exits_zero_when_nothing_fails(tmp_path):
    proc = subprocess.run([sys.executable, DOCTOR, "--fast", "--json"], capture_output=True,
                          text=True, env=_doctor_env(tmp_path))
    report = json.loads(proc.stdout)
    assert proc.returncode == 0, proc.stdout
    assert report["verdict"] in ("ok", "warn")
    assert all(r["status"] != "fail" for r in report["results"])


def test_the_doctor_exits_one_on_a_failure_and_names_it(tmp_path):
    env = _doctor_env(tmp_path, config=dict(GOOD_CONFIG, gatedStatuses=[]))
    proc = subprocess.run([sys.executable, DOCTOR, "--fast"], capture_output=True, text=True,
                          env=env)
    assert proc.returncode == 1
    assert "[FAIL] jira-config.gates" in proc.stdout and "failure(s)" in proc.stdout


def test_the_doctor_fails_on_a_token_in_the_environment(tmp_path):
    env = _doctor_env(tmp_path, JIRA_API_TOKEN="ATATT" + "y" * 40)
    proc = subprocess.run([sys.executable, DOCTOR, "--fast"], capture_output=True, text=True,
                          env=env)
    assert proc.returncode == 1 and "JIRA_API_TOKEN" in proc.stdout
    assert "y" * 40 not in proc.stdout


def test_the_session_hook_reports_problems_and_never_blocks(tmp_path):
    env = _doctor_env(tmp_path, config=dict(GOOD_CONFIG, gatedStatuses=[]))
    proc = subprocess.run([sys.executable, SELFTEST], input="{}", capture_output=True,
                          text=True, env=env)
    assert proc.returncode == 0
    assert "harness self-test" in proc.stdout and "jira-config.gates" in proc.stdout
    assert "doctor.py" in proc.stdout


def test_the_session_hook_is_wired_at_session_start():
    settings = json.load(open(os.path.join(REPO, ".claude", "settings.json")))
    commands = [h["command"] for e in settings["hooks"].get("SessionStart", [])
                for h in e["hooks"]]
    assert any("session_selftest.py" in c for c in commands)


@pytest.mark.parametrize("flag", ["--fast", "--no-live"])
def test_the_doctor_never_needs_the_claude_cli_in_offline_modes(tmp_path, flag):
    env = _doctor_env(tmp_path, CLAUDE_HARNESS_CLAUDE_BIN="/nonexistent/claude")
    proc = subprocess.run([sys.executable, DOCTOR, flag, "--json"], capture_output=True,
                          text=True, env=env, timeout=120)
    assert proc.returncode in (0, 1)
    json.loads(proc.stdout)


def test_a_sandbox_declaration_in_the_project_tree_fails_the_preflight(monkeypatch, tmp_path):
    """Review cycle 2, N4: this check had no test. A declaration an agent can write
    is ignored by the guard; the doctor must say it is there."""
    root = project(tmp_path)
    monkeypatch.setenv("CLAUDE_HARNESS_HUMAN_DIR", str(tmp_path / "human"))
    monkeypatch.delenv("CLAUDE_HARNESS_AWS_SANDBOX_PATH", raising=False)
    assert selfcheck.check_sandbox_account(str(root))[0].status == "warn"
    (root / ".claude" / "aws-sandbox.json").write_text(
        json.dumps({"accountId": "111122223333", "regions": ["ap-southeast-2"]}))
    result = selfcheck.check_sandbox_account(str(root))[0]
    assert result.status == "fail" and "project tree" in result.message


def test_an_operator_declared_sandbox_passes_and_a_malformed_one_fails(monkeypatch, tmp_path):
    root = project(tmp_path)
    human = tmp_path / "human"
    human.mkdir()
    monkeypatch.setenv("CLAUDE_HARNESS_HUMAN_DIR", str(human))
    monkeypatch.delenv("CLAUDE_HARNESS_AWS_SANDBOX_PATH", raising=False)
    (human / "aws-sandbox.json").write_text(
        json.dumps({"accountId": "111122223333", "regions": ["ap-southeast-2"]}))
    assert selfcheck.check_sandbox_account(str(root))[0].status == "ok"
    (human / "aws-sandbox.json").write_text(json.dumps({"accountId": "12", "regions": []}))
    assert selfcheck.check_sandbox_account(str(root))[0].status == "fail"
