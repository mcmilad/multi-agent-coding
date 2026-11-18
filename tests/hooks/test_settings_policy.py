"""settings.json: deny rules, sandbox, hook wiring and plugins are what the design says."""
import json
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SETTINGS = json.load(open(os.path.join(REPO, ".claude", "settings.json")))
HOOKS = SETTINGS["hooks"]
ATLASSIAN = ("mcp__plugin_atlassian_atlassian__", "mcp__plugin_engineering_atlassian__",
             "mcp__atlassian__", "mcp__claude_ai_Atlassian__")


def scripts_for(event, tool):
    """Hook scripts that would fire for `tool` on `event`."""
    out = []
    for entry in HOOKS.get(event, []):
        matcher = entry.get("matcher")
        if matcher is None or re.search(matcher, tool) if not _plain(matcher) else tool in (
                matcher or "").split("|"):
            out += [re.search(r"hooks/([a-z_]+\.py)", h["command"]).group(1)
                    for h in entry["hooks"]]
    return out


def _plain(matcher):
    """Claude Code treats a matcher of only letters, digits, _ - , | and spaces as names."""
    return matcher is not None and re.fullmatch(r"[A-Za-z0-9_\-,| ]*", matcher) is not None


# --- permissions ------------------------------------------------------------

DENY = SETTINGS["permissions"]["deny"]


@pytest.mark.parametrize("rule", [
    "Read(./.env)", "Read(**/.env)", "Read(~/.aws/credentials)", "Read(~/.ssh/**)",
    "Edit(/.claude/approvals/**)", "Edit(~/.claude/harness/**)", "Bash(claude *)",
    "Bash(git push --force *)", "Bash(git push -f *)",
    "Bash(git reset --hard *)", "Bash(sudo *)", "Bash(rm -rf /*)",
])
def test_deny_rule_is_present(rule):
    assert rule in DENY


def test_deny_rules_use_documented_syntax():
    for rule in DENY:
        assert re.fullmatch(r"(Read|Edit|Bash|WebFetch|Agent)\(.+\)|mcp__[A-Za-z0-9_*\-]+", rule), rule
        assert not rule.startswith("Write("), "Edit rules cover every file-editing tool"


def test_no_allow_rule_is_shipped_in_project_settings():
    assert not SETTINGS["permissions"].get("allow"), (
        "allow rules belong to the operator's own settings, not to the shared file")


def test_the_deny_list_stays_small():
    assert len(DENY) <= 30, "a long deny list is mostly preferences; keep the enforced set small"


# --- sandbox ----------------------------------------------------------------

SANDBOX = SETTINGS["sandbox"]


def test_the_sandbox_is_enabled_with_a_network_allowlist():
    assert SANDBOX["enabled"] is True
    domains = SANDBOX["network"]["allowedDomains"]
    assert domains and all(isinstance(d, str) and d for d in domains)
    assert "*" not in domains
    for needed in ("github.com", "pypi.org", "*.amazonaws.com"):
        assert needed in domains


def test_the_sandbox_denies_writes_to_guardrail_config_it_does_not_protect_by_default():
    deny = SANDBOX["filesystem"]["denyWrite"]
    for path in ("./.claude/gates.json", "./.claude/capabilities.json",
                 "./.claude/jira-config.json", "./.claude/approvals",
                 "./scripts/harness_admin.py", "~/.claude/harness"):
        assert path in deny
    assert all(p.startswith(("./", "~/")) for p in deny), "a bare / is absolute in sandbox paths"


def test_the_admin_token_is_removed_from_sandboxed_shells():
    envvars = {e["name"]: e["mode"] for e in SANDBOX["credentials"]["envVars"]}
    assert envvars.get("JIRA_API_TOKEN") == "deny"


def test_only_keys_a_project_file_may_set_are_used():
    ignored_in_project = {"allowAppleEvents", "allowNestedUserNamespaces", "ripgrep",
                          "bwrapPath", "socatPath"}
    assert not ignored_in_project & set(SANDBOX)
    assert "disabled" not in SANDBOX.get("filesystem", {})
    for entry in SANDBOX["credentials"].get("envVars", []) + SANDBOX["credentials"].get("files", []):
        assert entry["mode"] == "deny", "mask entries are ignored in project settings"
    for key in ("strictAllowlist", "tlsTerminate", "allowManagedDomainsOnly"):
        assert key not in SANDBOX.get("network", {})


def test_sandbox_keys_are_ones_the_documentation_lists():
    known = {"enabled", "autoAllowBashIfSandboxed", "allowUnsandboxedCommands",
             "failIfUnavailable", "excludedCommands", "enableWeakerNestedSandbox",
             "enableWeakerNetworkIsolation", "ignoreViolations", "filesystem", "network",
             "credentials"}
    assert set(SANDBOX) <= known
    assert set(SANDBOX["filesystem"]) <= {"allowWrite", "denyWrite", "denyRead", "allowRead"}
    assert set(SANDBOX["network"]) <= {"allowedDomains", "deniedDomains", "allowUnixSockets",
                                       "allowLocalBinding", "allowMachLookup"}
    assert set(SANDBOX["credentials"]) <= {"files", "envVars"}


# --- hook wiring ------------------------------------------------------------

@pytest.mark.parametrize("prefix", ATLASSIAN)
def test_jira_gates_fire_for_any_atlassian_server_name(prefix):
    assert "jira_issue_format_check.py" in scripts_for("PreToolUse", prefix + "createJiraIssue")
    assert "jira_issue_format_check.py" in scripts_for("PreToolUse", prefix + "editJiraIssue")
    assert "jira_transition_verify_gate.py" in scripts_for("PreToolUse", prefix + "transitionJiraIssue")
    assert "jira_execute_guard.py" in scripts_for("PreToolUse", prefix + "executeWrite")
    assert "jira_execute_guard.py" in scripts_for("PreToolUse", prefix + "executeDestructive")
    for op in ("createJiraIssue", "editJiraIssue", "transitionJiraIssue",
               "addOrEditJiraIssueComment", "searchJiraIssuesUsingJql", "getJiraIssue"):
        assert "jira_mirror_journal.py" in scripts_for("PostToolUse", prefix + op), op
    for event in ("PostToolUse", "PostToolUseFailure"):
        assert "jira_transition_sentinel_finalize.py" in scripts_for(
            event, prefix + "transitionJiraIssue"), event


def test_jira_gates_do_not_fire_for_unrelated_servers():
    assert "jira_transition_verify_gate.py" not in scripts_for(
        "PreToolUse", "mcp__github__transitionJiraIssue")


@pytest.mark.parametrize("tool", ["Write", "Edit", "NotebookEdit"])
def test_file_edits_meet_the_guardrail_guard_and_the_claim_gate(tool):
    fired = scripts_for("PreToolUse", tool)
    assert "guardrail_guard.py" in fired and "claim_gate.py" in fired


def test_the_aws_guard_fires_for_bash_and_aws_mcp_tools():
    assert "aws_guard.py" in scripts_for("PreToolUse", "Bash")
    assert "aws_guard.py" in scripts_for(
        "PreToolUse", "mcp__plugin_aws-core_aws-mcp__aws___run_script")
    assert "aws_guard.py" in scripts_for(
        "PreToolUse", "mcp__plugin_aws-serverless_aws-serverless-mcp__sam_deploy")


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_spawning_an_agent_meets_the_spawn_guard(tool):
    assert "agent_spawn_guard.py" in scripts_for("PreToolUse", tool)


def test_session_start_and_untrusted_content_hooks_are_wired():
    assert "session_selftest.py" in scripts_for("SessionStart", "")
    for tool in ("WebFetch", "WebSearch", "mcp__plugin_atlassian_atlassian__getJiraIssue"):
        assert "untrusted_content_flag.py" in scripts_for("PostToolUse", tool)


def test_every_matcher_is_a_valid_regex():
    for entries in HOOKS.values():
        for entry in entries:
            if entry.get("matcher"):
                re.compile(entry["matcher"])


# --- plugins, model, env ----------------------------------------------------

def test_every_enabled_plugin_is_in_the_manifest_from_the_same_marketplace():
    manifest = json.load(open(os.path.join(REPO, ".claude", "capabilities.json")))
    listed = {(p["name"], p["marketplace"]) for prof in manifest["profiles"].values()
              for p in prof.get("plugins", [])}
    optional = {("superpowers", "claude-plugins-official")}
    names = []
    for key, on in SETTINGS["enabledPlugins"].items():
        name, marketplace = key.split("@")
        names.append(name)
        assert on is True
        assert (name, marketplace) in listed | optional, key
    assert len(names) == len(set(names)), "a plugin is enabled from two marketplaces"


def test_every_required_manifest_plugin_is_enabled():
    manifest = json.load(open(os.path.join(REPO, ".claude", "capabilities.json")))
    enabled = {k.split("@")[0] for k in SETTINGS["enabledPlugins"]}
    for profile, spec in manifest["profiles"].items():
        for plugin in spec.get("plugins", []):
            if plugin["required"]:
                assert plugin["name"] in enabled, "{} needs {}".format(profile, plugin["name"])


def test_no_project_wide_model_pin_and_no_third_party_marketplace():
    assert "model" not in SETTINGS
    assert "extraKnownMarketplaces" not in SETTINGS


def test_neither_auto_nor_bypass_mode_is_requested_from_project_settings():
    """Both are ignored from a project file; asking for them would only mislead."""
    assert SETTINGS["permissions"].get("defaultMode") not in ("auto", "bypassPermissions")


def test_a_command_cannot_ask_to_leave_the_sandbox():
    """The sandbox is the boundary the hooks are not. With this unset, any
    worker could request an unsandboxed command, and a broad allow rule in a
    local settings file would grant it without a prompt."""
    import json
    import os
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sandbox = json.load(open(os.path.join(repo, ".claude", "settings.json")))["sandbox"]
    assert sandbox["enabled"] is True and sandbox["allowUnsandboxedCommands"] is False
