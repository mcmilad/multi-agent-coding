"""Health checks for the harness itself.

Every guardrail here fails open by design, which means a broken one is silent:
a hook that cannot import, a Jira config that gates nothing, a missing MCP
server and a credential leaking into the environment all look exactly like a
healthy run until something goes wrong. These checks make that state visible
before a run starts.

Used by `scripts/doctor.py` (the full preflight) and by `session_selftest.py`
(the fast subset, at session start). Standard library only. Nothing here raises;
a check that cannot run reports that as its result.

Each check returns a list of Result(status, name, message):
  ok    the thing is as it should be
  warn  a run can proceed, with reduced protection or capability
  fail  a run should not start
"""
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence  # noqa: E402
import jira_mirror  # noqa: E402
import team_hook_common as common  # noqa: E402

Result = collections.namedtuple("Result", "status name message")
OK, WARN, FAIL = "ok", "warn", "fail"

CLAUDE_BIN_ENV = "CLAUDE_HARNESS_CLAUDE_BIN"
MIRROR_STALE_SECONDS = 24 * 3600
AUDIT_TAIL_BYTES = 512 * 1024

# Names that mean "this variable holds a credential". `_TOKEN` only as a suffix,
# so a size limit such as MAX_MCP_OUTPUT_TOKENS is not one.
_CREDENTIAL_NAME = re.compile(
    r"(^|_)TOKEN$|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|CREDENTIAL", re.I)
# Present in an ordinary Claude Code session and not a leak of the user's own.
_CREDENTIAL_NAME_ALLOW = re.compile(
    r"^(CLAUDE_CODE_|ANTHROPIC_|GITHUB_TOKEN$|GH_TOKEN$|CLAUDE_HARNESS_)", re.I)
_SECRET_VALUE = re.compile(r"^(ATATT[A-Za-z0-9_\-=]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,})")


def project_dir():
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, "missing"
    except Exception as exc:
        return None, "unreadable ({})".format(exc)


# --- individual checks ------------------------------------------------------

def check_settings(root=None):
    """Settings load, wire only hooks that exist, and inject no credential."""
    root = root or project_dir()
    path = os.path.join(root, ".claude", "settings.json")
    settings, problem = _read_json(path)
    if problem:
        return [Result(FAIL, "settings", ".claude/settings.json is {}".format(problem))]
    out = []
    bad_env = sorted(k for k in (settings.get("env") or {}) if _CREDENTIAL_NAME.search(k))
    if bad_env:
        out.append(Result(FAIL, "settings.env", (
            "settings `env` is inherited by every agent shell and carries credential-named "
            "variables: {}".format(", ".join(bad_env)))))
    missing = []
    for entries in (settings.get("hooks") or {}).values():
        for entry in entries or []:
            for hook in entry.get("hooks") or []:
                for script in re.findall(r"/\.claude/hooks/([A-Za-z0-9_]+\.py)",
                                         str(hook.get("command") or "")):
                    if not os.path.exists(os.path.join(root, ".claude", "hooks", script)):
                        missing.append(script)
    if missing:
        out.append(Result(FAIL, "settings.hooks", "wired hooks that do not exist: {}".format(
            ", ".join(sorted(set(missing))))))
    deny = (settings.get("permissions") or {}).get("deny") or []
    if not deny:
        out.append(Result(WARN, "settings.permissions", (
            "no permissions.deny rules: nothing stops an agent editing the hooks or "
            "reading secret files")))
    if not (settings.get("sandbox") or {}).get("enabled"):
        out.append(Result(WARN, "settings.sandbox", "the Bash sandbox is not enabled"))
    return out or [Result(OK, "settings", "settings load; hooks exist; no credential in env")]


def check_environment(environ=None):
    """No long-lived credential sitting in the session environment."""
    environ = os.environ if environ is None else environ
    leaked, suspicious = [], []
    for name, value in environ.items():
        if not value:
            continue
        if _SECRET_VALUE.match(str(value)):
            leaked.append(name)
        elif _CREDENTIAL_NAME.search(name) and not _CREDENTIAL_NAME_ALLOW.match(name):
            suspicious.append(name)
    out = []
    if leaked:
        out.append(Result(FAIL, "environment", (
            "token-shaped values are in the session environment, where every agent "
            "shell can read them: {} (values not shown)".format(", ".join(sorted(leaked))))))
    if suspicious:
        out.append(Result(WARN, "environment", (
            "credential-named variables are set in the session environment: {} "
            "(values not shown). Prefer a profile or SSO over exported keys."
        ).format(", ".join(sorted(suspicious)))))
    return out or [Result(OK, "environment", "no credential-shaped variable in the environment")]


def check_jira_config():
    """The Jira config exists and actually gates something."""
    cfg = jira_mirror.load_config()
    if not cfg:
        return [Result(FAIL, "jira-config", (
            ".claude/jira-config.json is missing or unreadable: every Jira hook fails "
            "open without it. Run scripts/jira_bootstrap.py discover."))]
    out = []
    if not cfg.get("projectKey"):
        out.append(Result(FAIL, "jira-config", "no projectKey: no hook can scope itself"))
    gated = cfg.get("gatedStatuses")
    if not isinstance(gated, list) or not gated:
        out.append(Result(FAIL, "jira-config.gates", (
            "gatedStatuses is empty: the verification gate guards nothing")))
    else:
        reachable = set((cfg.get("transitions") or {}).values())
        unreachable = [s for s in gated if s not in reachable]
        if unreachable or cfg.get("missingGatedTransitions"):
            out.append(Result(FAIL, "jira-config.gates", (
                "no transition id maps to gated status(es) {}: transitions into them "
                "resolve to 'unknown' and are not gated. Re-run discover."
            ).format(", ".join(unreachable or cfg.get("missingGatedTransitions")))))
        if "In Review" not in gated:
            out.append(Result(WARN, "jira-config.gates", (
                "'In Review' is not a gated status: implementers can only hand off "
                "straight to Done")))
    return out or [Result(OK, "jira-config", "project {} gates {}".format(
        cfg.get("projectKey"), ", ".join(cfg.get("gatedStatuses"))))]


def check_gates(root=None):
    """The project checks load. A file that still lists hook-run `gates` would
    block every transition, so it is a failure here, before a run starts."""
    root = root or project_dir()
    checks, problem = evidence.load_agent_checks(root)
    if problem:
        return [Result(FAIL, "gates", problem)]
    if not checks:
        return [Result(WARN, "gates", (
            "no agentChecks are configured ({}): verification runs only each issue's own "
            "Run: command, not the project's lint and tests").format(
                evidence.gates_path(root)))]
    return [Result(OK, "gates", "agent checks: {} (CI is the independent re-run)".format(
        ", ".join(c["name"] for c in checks)))]


def check_state():
    """The coordination-state directory is writable and the mirror is current."""
    root = common.state_root()
    out = []
    try:
        os.makedirs(root, exist_ok=True)
        probe = os.path.join(root, ".selfcheck.{}".format(os.getpid()))
        if not common.write_private(probe, "ok", within=root):
            raise OSError("write refused")
        os.remove(probe)
    except Exception as exc:
        return [Result(FAIL, "state", "state directory {} is not writable: {}".format(root, exc))]
    project = jira_mirror.load_config().get("projectKey")
    if project:
        path = jira_mirror.mirror_path(project)
        if not os.path.exists(path):
            out.append(Result(WARN, "state.mirror", (
                "no local mirror for project {}: the hooks know no issue. Run one JQL "
                "search of the project through the Atlassian MCP to build it.").format(project)))
        elif time.time() - os.path.getmtime(path) > MIRROR_STALE_SECONDS:
            out.append(Result(WARN, "state.mirror", (
                "the local mirror for {} was last updated over a day ago; a JQL search "
                "of the project refreshes it").format(project)))
    return out or [Result(OK, "state", "state directory writable; mirror current")]


def check_audit_log():
    """Hooks that failed open recently. A broken guardrail looks like a quiet one."""
    path = common.log_path()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - AUDIT_TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return [Result(OK, "audit-log", "no audit log yet")]
    reasons = collections.Counter()
    for line in tail:
        try:
            record = json.loads(line)
        except Exception:
            continue
        reason = str(record.get("reason") or "")
        if "hook error" in reason or "error (fail-" in reason:
            reasons[re.sub(r"\s+", " ", reason)[:120]] += 1
    if not reasons:
        return [Result(OK, "audit-log", "no hook errors in the recent audit log")]
    top = "; ".join("{}x {}".format(n, r) for r, n in reasons.most_common(3))
    return [Result(WARN, "audit-log", (
        "{} hook error(s) in the recent audit log -- a hook that errors is a guardrail "
        "that is not running: {}").format(sum(reasons.values()), top))]


def check_hooks(root=None, timeout=15):
    """Every hook entry point starts, and exits cleanly on an empty payload."""
    root = root or project_dir()
    hooks_dir = os.path.join(root, ".claude", "hooks")
    broken = []
    try:
        scripts = sorted(f for f in os.listdir(hooks_dir) if f.endswith(".py"))
    except Exception as exc:
        return [Result(FAIL, "hooks", "cannot list {}: {}".format(hooks_dir, exc))]
    probed = 0
    for script in scripts:
        path = os.path.join(hooks_dir, script)
        try:
            source = open(path).read()
        except Exception:
            continue
        # Built from two pieces so this library is not itself mistaken for an
        # entry point by anything that scans hook sources for the guard.
        if ("__name__ == " + '"__main__"') not in source:
            continue
        probed += 1
        args = [sys.executable, path]
        if script == "spec_gate.py":
            args.append("prompt")
        try:
            proc = subprocess.run(args, input="{}", capture_output=True, text=True,
                                  timeout=timeout, env=dict(os.environ))
            if proc.returncode != 0 or "Traceback" in (proc.stderr or ""):
                last = (proc.stderr or "").strip().splitlines()[-1:] or ["exit {}".format(
                    proc.returncode)]
                broken.append("{}: {}".format(script, last[0][:120]))
        except subprocess.TimeoutExpired:
            broken.append("{}: no answer within {}s".format(script, timeout))
        except Exception as exc:
            broken.append("{}: {}".format(script, exc))
    if broken:
        return [Result(FAIL, "hooks", "hook(s) that do not run: {}".format("; ".join(broken)))]
    return [Result(OK, "hooks", "{} hook entry points start and answer".format(probed))]


def check_sandbox_account(root=None):
    root = root or project_dir()
    path = os.environ.get("CLAUDE_HARNESS_AWS_SANDBOX_PATH") or os.path.join(
        common.human_dir(), "aws-sandbox.json")
    if os.path.exists(os.path.join(root, ".claude", "aws-sandbox.json")):
        return [Result(FAIL, "aws-sandbox", (
            "a sandbox declaration sits in the project tree (.claude/aws-sandbox.json), "
            "where an agent can write it; it is ignored. Declare it with "
            "`scripts/harness_admin.py sandbox` instead and delete that file"))]
    if not os.path.exists(path):
        return [Result(WARN, "aws-sandbox", (
            "no AWS sandbox account is declared (scripts/harness_admin.py sandbox): every "
            "AWS change will wait for human approval"))]
    cfg, problem = _read_json(path)
    account = str((cfg or {}).get("accountId") or "")
    if problem or not re.fullmatch(r"\d{12}", account) or not (cfg or {}).get("regions"):
        return [Result(FAIL, "aws-sandbox", (
            "the AWS sandbox declaration is malformed; it needs a 12-digit accountId and a "
            "non-empty regions list. Re-run scripts/harness_admin.py sandbox"))]
    return [Result(OK, "aws-sandbox", "sandbox account declared for {}".format(
        ", ".join(cfg["regions"])))]


# --- capabilities -----------------------------------------------------------

def load_capabilities(root=None):
    root = root or project_dir()
    path = os.environ.get("CLAUDE_HARNESS_CAPABILITIES_PATH") or os.path.join(
        root, ".claude", "capabilities.json")
    cfg, problem = _read_json(path)
    if problem or not isinstance((cfg or {}).get("profiles"), dict):
        return None, "capabilities manifest {} is {}".format(path, problem or "malformed")
    return cfg, None


def _claude(args, timeout=90):
    """stdout of a `claude` CLI call, or None if it could not be run."""
    try:
        proc = subprocess.run([os.environ.get(CLAUDE_BIN_ENV) or "claude"] + args,
                              capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


def installed_plugins():
    """{name: {"marketplaces": set, "versions": set, "enabled": bool}} or None."""
    out = _claude(["plugin", "list"])
    if out is None:
        return None
    plugins, current = {}, None
    for line in out.splitlines():
        head = re.match(r"\s*\S\s+([A-Za-z0-9_.-]+)@([A-Za-z0-9_.-]+)\s*$", line)
        if head:
            current = plugins.setdefault(head.group(1), {
                "marketplaces": set(), "versions": set(), "enabled": False})
            current["marketplaces"].add(head.group(2))
            continue
        if current is None:
            continue
        version = re.match(r"\s*Version:\s*(\S+)", line)
        if version:
            current["versions"].add(version.group(1))
        status = re.match(r"\s*Status:\s*(.+)$", line)
        if status and re.search(r"enabled|loaded", status.group(1)) and "disabled" not in status.group(1):
            current["enabled"] = True
    return plugins


def connected_servers():
    """{server name: state} from `claude mcp list`, or None. state: connected|auth|failed."""
    out = _claude(["mcp", "list"])
    if out is None:
        return None
    servers = {}
    for line in out.splitlines():
        match = re.match(r"\s*([^\s:][^:]*(?::[^:\s]+)*?):\s+\S.*\s-\s+(.+)$", line)
        if not match:
            continue
        verdict = match.group(2)
        state = ("connected" if "Connected" in verdict and "failed" not in verdict.lower()
                 else "auth" if "authentication" in verdict.lower() else "failed")
        servers[match.group(1).strip()] = state
    return servers


def check_capabilities(profiles=("core",), root=None, live=True):
    """Plugins, MCP servers and CLIs the selected profiles need."""
    cfg, problem = load_capabilities(root)
    if problem:
        return [Result(FAIL, "capabilities", problem)]
    unknown = [p for p in profiles if p not in cfg["profiles"]]
    if unknown:
        return [Result(FAIL, "capabilities", "unknown profile(s): {}. Known: {}".format(
            ", ".join(unknown), ", ".join(sorted(cfg["profiles"]))))]
    out = []
    plugins = installed_plugins() if live else None
    servers = connected_servers() if live else None
    if live and (plugins is None or servers is None):
        out.append(Result(WARN, "capabilities", (
            "the `claude` CLI could not be queried; plugins and MCP servers were not checked")))
    # An item several profiles name is required if ANY of them requires it;
    # judging it by whichever profile happened to come first under-reports.
    required = set()
    for profile in profiles:
        for kind, key in (("clis", "name"), ("plugins", "name"), ("mcpServers", "match")):
            for item in cfg["profiles"][profile].get(kind) or []:
                if item.get("required"):
                    required.add((kind, item[key]))
    seen = set()
    for profile in profiles:
        spec = dict(cfg["profiles"][profile])
        for kind, key in (("clis", "name"), ("plugins", "name"), ("mcpServers", "match")):
            spec[kind] = [dict(item, required=(kind, item[key]) in required)
                          for item in spec.get(kind) or []]
        for cli in spec.get("clis") or []:
            if ("cli", cli["name"]) in seen:
                continue
            seen.add(("cli", cli["name"]))
            if shutil.which(cli["name"]) is None:
                out.append(Result(FAIL if cli.get("required") else WARN, "cli." + cli["name"], (
                    "`{}` is not installed ({} profile{})").format(
                        cli["name"], profile,
                        ": " + cli["why"] if cli.get("why") else "")))
        for plugin in spec.get("plugins") or []:
            if ("plugin", plugin["name"]) in seen or plugins is None:
                continue
            seen.add(("plugin", plugin["name"]))
            level = FAIL if plugin.get("required") else WARN
            have = plugins.get(plugin["name"])
            label = "plugin." + plugin["name"]
            if not have or not have["enabled"]:
                out.append(Result(level, label, "plugin {} is not installed and enabled "
                                  "({} profile: {})".format(plugin["name"], profile,
                                                            plugin.get("why", ""))))
                continue
            if plugin.get("version") is None:
                out.append(Result(WARN, label, "plugin {} has no pinned version in the "
                                  "manifest".format(plugin["name"])))
            elif plugin["version"] not in have["versions"]:
                out.append(Result(WARN, label, "plugin {} is at {} but the manifest was "
                                  "verified against {}".format(
                                      plugin["name"], ", ".join(sorted(have["versions"])),
                                      plugin["version"])))
            if len(have["marketplaces"]) > 1:
                out.append(Result(WARN, label, "plugin {} is installed from more than one "
                                  "marketplace: {}".format(
                                      plugin["name"], ", ".join(sorted(have["marketplaces"])))))
        for server in spec.get("mcpServers") or []:
            if ("mcp", server["match"]) in seen or servers is None:
                continue
            seen.add(("mcp", server["match"]))
            level = FAIL if server.get("required") else WARN
            states = [s for name, s in servers.items() if server["match"] in name.lower()]
            label = "mcp." + server["match"]
            if "connected" in states:
                continue
            if "auth" in states:
                out.append(Result(level, label, "MCP server '{}' needs authentication "
                                  "(run /mcp){}".format(server["match"], _why(server))))
            elif states:
                out.append(Result(level, label, "MCP server '{}' is configured but failed "
                                  "to connect{}".format(server["match"], _why(server))))
            else:
                out.append(Result(level, label, "no MCP server matching '{}' is configured"
                                  "{}".format(server["match"], _why(server))))
    return out or [Result(OK, "capabilities", "profiles {} are satisfied".format(
        ", ".join(profiles)))]


def _why(entry):
    return " -- " + entry["why"] if entry.get("why") else ""


# --- suites -----------------------------------------------------------------

def fast_checks():
    """Cheap, no subprocess to the `claude` CLI: safe to run at every session start."""
    results = []
    for check in (check_settings, check_environment, check_jira_config, check_gates,
                  check_state, check_audit_log):
        try:
            results.extend(check())
        except Exception as exc:
            results.append(Result(WARN, check.__name__, "check could not run: {}".format(exc)))
    return results


def full_checks(profiles=("core",), live=True):
    results = fast_checks()
    for check in (check_hooks, check_sandbox_account):
        try:
            results.extend(check())
        except Exception as exc:
            results.append(Result(WARN, check.__name__, "check could not run: {}".format(exc)))
    try:
        results.extend(check_capabilities(profiles, live=live))
    except Exception as exc:
        results.append(Result(WARN, "capabilities", "check could not run: {}".format(exc)))
    return results


def worst(results):
    if any(r.status == FAIL for r in results):
        return FAIL
    return WARN if any(r.status == WARN for r in results) else OK
