#!/usr/bin/env python3
"""PreToolUse hook on Bash and AWS MCP tools -- assert the target before acting.

The agent prompts have always said "assume production, confirm before anything
destructive", but nothing enforced it, and the worst incidents on record came
from exactly that gap: a wrong-region `terraform apply`, a teardown that
destroyed nothing while resources kept billing, a missing `--region` that used
the shell's ambient one.

Every AWS-touching call is classified as a READ, a WRITE or DESTRUCTIVE:

  READ         always allowed.
  WRITE /      allowed only when ALL of these hold:
  DESTRUCTIVE    - the caller may change AWS at all: the lead (no agent
                   identity) or a devops worker;
                 - the region is stated explicitly and is a sandbox region;
                 - the credentials in use resolve, by an STS call this hook
                   makes itself, to the sandbox account.
               Anything else needs an approval marker a human creates for that
               exact command.

The sandbox is declared by the operator, outside the project tree, in
~/.claude/harness/<repo-id>/aws-sandbox.json (`scripts/harness_admin.py sandbox`):
    {"accountId": "123456789012", "regions": ["ap-southeast-2"]}
With no sandbox declared, every write needs an approval marker, which lives in
the same directory and which only the operator can create.

This is a guardrail against mistakes, not a security boundary: command text is
parsed, not executed, and a determined shell construction can hide a call from
it. Credentials scoped to the sandbox account are the boundary.

Bypass: none by environment variable. An approval marker is the only way past.
FAIL OPEN on an unparseable payload; FAIL CLOSED once a call is known to be a
write, because "could not verify the target" must not read as "target is fine".
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import (  # noqa: E402
    read_payload, allow, block, audit, as_dict, tool_input_of, caller_identity, human_dir,
    path_is_under,
)

EVENT = "PreToolUse"
READ, WRITE, DESTRUCTIVE = "read", "write", "destructive"
_RANK = {READ: 0, WRITE: 1, DESTRUCTIVE: 2}

SANDBOX_PATH_ENV = "CLAUDE_HARNESS_AWS_SANDBOX_PATH"
APPROVALS_DIR_ENV = "CLAUDE_HARNESS_APPROVALS_DIR"
AWS_BIN_ENV = "CLAUDE_HARNESS_AWS_BIN"
WRITER_ROLES = ("devops",)

# --- classification tables --------------------------------------------------

_AWS_READ_PREFIXES = (
    "describe-", "get-", "list-", "head-", "batch-get-", "lookup-", "search-",
    "view-", "estimate-", "validate-", "simulate-", "check-", "test-", "detect-",
    "scan", "query", "select-", "filter-", "preview-", "export-", "generate-",
    "decode-", "verify-", "wait", "help", "ls", "tail", "presign",
)
_AWS_DESTRUCTIVE_PREFIXES = (
    "delete-", "terminate-", "remove-", "purge-", "deregister-", "revoke-",
    "detach-", "disable-", "destroy-", "cancel-", "release-", "reset-",
    "disassociate-", "deactivate-", "stop-", "rb", "rm",
)
_AWS_GLOBAL_OPTS_WITH_VALUE = (
    "--profile", "--region", "--output", "--endpoint-url", "--query", "--color",
    "--ca-bundle", "--cli-read-timeout", "--cli-connect-timeout", "--cli-binary-format",
)
_TOOL_VERBS = {
    "cdk": {
        READ: ("synth", "synthesize", "diff", "ls", "list", "doctor", "context",
               "docs", "metadata", "notices", "acknowledge", "init", "version"),
        DESTRUCTIVE: ("destroy",),
    },
    "sam": {
        READ: ("build", "validate", "local", "init", "list", "logs", "traces",
               "docs", "pipeline", "remote"),
        DESTRUCTIVE: ("delete",),
    },
    "terraform": {
        READ: ("init", "plan", "validate", "fmt", "show", "output", "providers",
               "graph", "version", "console", "get", "login", "logout", "test",
               "workspace", "metadata"),
        DESTRUCTIVE: ("destroy",),
    },
}
_TOOL_VERBS["tofu"] = _TOOL_VERBS["terraform"]

_PASCAL_READ = ("Describe", "Get", "List", "Head", "BatchGet", "Lookup", "Search",
                "View", "Estimate", "Validate", "Simulate", "Check", "Detect",
                "Scan", "Query", "Select", "Filter", "Preview", "Export", "Decode",
                "Verify", "Generate")
_PASCAL_DESTRUCTIVE = ("Delete", "Terminate", "Remove", "Purge", "Deregister", "Revoke",
                       "Detach", "Disable", "Destroy", "Cancel", "Release", "Reset",
                       "Disassociate", "Deactivate", "Stop")

_MCP_WRITE_TOOLS = ("sam_deploy", "deploy_webapp", "update_webapp_frontend",
                    "configure_domain", "transact", "sam_init")
_MCP_SCRIPT_TOOLS = ("run_script", "call_aws")

_INLINE_REGION = re.compile(r"\bAWS_(?:DEFAULT_)?REGION=([\"']?)([a-z0-9-]+)\1")
_INLINE_PROFILE = re.compile(r"\bAWS_PROFILE=([\"']?)([A-Za-z0-9_.-]+)\1")
_OP_NAME = re.compile(r"operation_name[\"']?\s*[=:]\s*[\"']([A-Za-z0-9_]+)[\"']")
_OP_NAME_ANY = re.compile(r"operation_name[\"']?\s*[=:]")
_REGION_NAME = re.compile(r"region(?:_name)?[\"']?\s*[=:]\s*[\"']([a-z0-9-]+)[\"']")


def _worst(a, b):
    return a if _RANK[a] >= _RANK[b] else b


def _split(text):
    try:
        return shlex.split(text)
    except Exception:
        return text.split()


def classify_aws_cli(tokens):
    """(kind, region, profile) for the tokens following `aws`."""
    region = profile = None
    positional = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _AWS_GLOBAL_OPTS_WITH_VALUE and i + 1 < len(tokens):
            if tok == "--region":
                region = tokens[i + 1]
            elif tok == "--profile":
                profile = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("--region="):
            region = tok.split("=", 1)[1]
        elif tok.startswith("--profile="):
            profile = tok.split("=", 1)[1]
        elif not tok.startswith("-"):
            positional.append(tok)
        i += 1
    if len(positional) < 2:
        # `aws help`, `aws --version`, `aws configure` (local config only)
        return READ, region, profile
    service, operation = positional[0], positional[1].lower()
    if service in ("configure", "help", "sso", "login"):
        return READ, region, profile
    if service == "s3" and operation in ("cp", "mv", "sync", "mb", "website"):
        kind = DESTRUCTIVE if (operation == "sync" and "--delete" in tokens) else WRITE
        return kind, region, profile
    if operation.startswith(_AWS_DESTRUCTIVE_PREFIXES) or operation in ("rb", "rm"):
        return DESTRUCTIVE, region, profile
    if operation.startswith(_AWS_READ_PREFIXES):
        return READ, region, profile
    return WRITE, region, profile          # unknown verbs are treated as writes


def classify_tool(tool, tokens):
    """(kind, region, profile) for cdk / sam / terraform / tofu."""
    region = profile = None
    verbs = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--region", "--profile") and i + 1 < len(tokens):
            if tok == "--region":
                region = tokens[i + 1]
            else:
                profile = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("--region="):
            region = tok.split("=", 1)[1]
        elif tok.startswith("--profile="):
            profile = tok.split("=", 1)[1]
        elif not tok.startswith("-"):
            verbs.append(tok.lower())
        i += 1
    if not verbs:
        return READ, region, profile
    verb = verbs[0]
    table = _TOOL_VERBS[tool]
    if verb in table[DESTRUCTIVE]:
        return DESTRUCTIVE, region, profile
    if tool in ("terraform", "tofu"):
        if verb == "apply" and any(t in ("-destroy", "--destroy") for t in tokens):
            return DESTRUCTIVE, region, profile
        if verb == "state" and len(verbs) > 1 and verbs[1] in ("rm", "push"):
            return DESTRUCTIVE, region, profile
        if verb == "state" and len(verbs) > 1 and verbs[1] in ("list", "show", "pull"):
            return READ, region, profile
        if verb == "workspace" and len(verbs) > 1 and verbs[1] == "delete":
            return DESTRUCTIVE, region, profile
    if verb in table[READ]:
        return READ, region, profile
    return WRITE, region, profile


_OPERATORS = set(";&|()") | {"&&", "||", ";;", "|&"}
_WRAPPERS = ("sudo", "env", "time", "exec", "nohup", "command", "xargs", "npx", "uvx",
             "pnpm", "yarn", "bunx")
_SHELLS = ("bash", "sh", "zsh", "dash", "ksh")
_TOOLS = ("aws", "cdk", "aws-cdk", "sam", "terraform", "tofu")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SERVICE = re.compile(r"^[a-z][a-z0-9-]*$")


def simple_commands(command, depth=0):
    """Yield the token list of each simple command in a shell line.

    Tokenised with shlex so quoting is respected: `git commit -m "aws s3 rm x"`
    is one command whose first word is `git`, not an AWS call. Operators,
    subshells and command substitution start a new simple command, and the
    string handed to `bash -c` is parsed as a line of its own.
    """
    # A newline ends a command just as `;` does; a backslash-newline joins lines.
    command = command.replace("\\\n", " ").replace("\n", " ; ")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except Exception:
        tokens = command.split()
    current = []
    for tok in tokens:
        if tok in _OPERATORS or (tok and set(tok) <= set(";&|()")):
            if current:
                yield current
            current = []
            continue
        tok = tok.lstrip("`$").rstrip("`")
        if tok:
            current.append(tok)
    if current:
        yield current


# Wrapper options that consume the next token as their value. Skipping only the
# option left its value in command position: `env -u CLAUDECODE python3 x.py`
# read as a program called CLAUDECODE, and the real command went unjudged.
_WRAPPER_OPTS_WITH_VALUE = {
    "env": ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"),
    "sudo": ("-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "-D"),
    "xargs": ("-I", "-n", "-P", "-L", "-s", "-d", "-E"),
    "npx": ("-p", "--package", "-c", "--call"),
    "uvx": ("--from", "--with", "-p", "--python"),
}


def _command_position(tokens):
    """Drop env assignments and wrappers so tokens[0] is the program being run."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _ASSIGNMENT.match(tok):
            i += 1
            continue
        wrapper = os.path.basename(tok)
        if wrapper not in _WRAPPERS:
            break
        i += 1
        takes_value = _WRAPPER_OPTS_WITH_VALUE.get(wrapper, ())
        while i < len(tokens) and (tokens[i].startswith("-") or _ASSIGNMENT.match(tokens[i])):
            option = tokens[i]
            i += 1
            if option in takes_value and i < len(tokens):
                i += 1                      # the option's value, not the program
    return tokens[i:]


_HEREDOC = re.compile(r"^(?P<head>[^\n]*?)<<-?\s*(['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)\2"
                      r"(?P<tail>[^\n]*)\n(?P<body>.*?)\n[ \t]*(?P=tag)[ \t]*$",
                      re.M | re.S)


def strip_data_heredocs(command):
    """Remove heredoc bodies that are data, keep those fed to a shell.

    `python3 - <<'EOF' ... EOF` carries a program's input, not shell commands;
    scanning it produced false alarms on any source text that mentions `aws`.
    A heredoc whose command is a shell (`bash <<EOF`) is a script and stays.
    """
    def replace(match):
        words = _command_position(_split(match.group("head")))
        if words and os.path.basename(words[0]) in _SHELLS:
            return match.group(0)
        return "{} {}".format(match.group("head"), match.group("tail"))
    return _HEREDOC.sub(replace, command)


def classify_bash(command, depth=0):
    """Worst classification across every AWS-touching command in a shell line.

    Returns (kind, regions, profile, segments). `regions` holds the region each
    non-read command stated; None in it means one of them stated none.
    """
    command = strip_data_heredocs(command)
    kind, regions, profile, segments = READ, set(), None, []
    inline_region = _INLINE_REGION.search(command)
    inline_profile = _INLINE_PROFILE.search(command)
    for raw in simple_commands(command):
        tokens = _command_position(raw)
        if not tokens:
            continue
        program = os.path.basename(tokens[0])
        if program in _SHELLS and depth < 3:
            # `bash -c "<line>"`: the line is where the AWS call would be.
            for k, tok in enumerate(tokens[1:], start=1):
                if tok == "-c" and k + 1 < len(tokens):
                    sub = classify_bash(tokens[k + 1], depth + 1)
                    kind = _worst(kind, sub[0])
                    regions |= sub[1]
                    profile = sub[2] or profile
                    segments += sub[3]
                    break
            continue
        if program not in _TOOLS:
            continue
        tool = "cdk" if program == "aws-cdk" else program
        rest = tokens[1:]
        if tool == "aws":
            words = [t for t in rest if not t.startswith("-")]
            if words and not _SERVICE.match(words[0]):
                continue                      # not an invocation of the CLI
            seg_kind, seg_region, seg_profile = classify_aws_cli(rest)
        else:
            seg_kind, seg_region, seg_profile = classify_tool(tool, rest)
        segments.append((tool, seg_kind))
        kind = _worst(kind, seg_kind)
        if seg_kind != READ:
            regions.add(seg_region or (inline_region.group(2) if inline_region else None))
            profile = seg_profile or profile
    if profile is None and inline_profile:
        profile = inline_profile.group(2)
    return kind, regions, profile, segments


def _strings_of(value, depth=0):
    """Every string inside a tool input, joined: what a script actually says."""
    if isinstance(value, str):
        return value
    if depth > 6:
        return ""
    if isinstance(value, dict):
        return "\n".join(_strings_of(v, depth + 1) for v in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_strings_of(v, depth + 1) for v in value)
    return ""


def classify_mcp(tool_name, tool_input):
    """(kind, regions) for an AWS MCP tool call, or (None, None) if not AWS."""
    name = str(tool_name or "")
    if not name.startswith("mcp__") or "aws" not in name.lower():
        return None, None
    op = name.rsplit("__", 1)[-1]
    regions = set()
    if op in _MCP_SCRIPT_TOOLS:
        # The raw strings, not json.dumps(): JSON-encoding escapes every double
        # quote, so `operation_name="ListBuckets"` no longer matched, the call
        # fell through to "an operation we cannot read", and ordinary reads
        # were blocked as writes.
        code = _strings_of(tool_input)
        ops = _OP_NAME.findall(code)
        kind = READ
        for operation in ops:
            if operation.startswith(_PASCAL_DESTRUCTIVE):
                kind = _worst(kind, DESTRUCTIVE)
            elif not operation.startswith(_PASCAL_READ):
                kind = _worst(kind, WRITE)
        # Every operation must be a literal we can read. One literal read next to
        # an operation held in a variable is not a read: the variable is where a
        # destructive call would hide.
        named = len(_OP_NAME_ANY.findall(code))
        calls = len(re.findall(r"\bcall_boto3\s*\(", code))
        opaque = named > len(ops) or calls > len(ops)
        if opaque or (not ops and ("call_boto3" in code or op == "call_aws")):
            kind = _worst(kind, WRITE)   # an operation we cannot read is not a read
        regions = set(_REGION_NAME.findall(code)) or {None}
        return kind, regions
    if op in _MCP_WRITE_TOOLS:
        region = as_dict(tool_input).get("region")
        return WRITE, {region if isinstance(region, str) and region else None}
    return READ, regions


# --- target verification ----------------------------------------------------

def project_dir(payload):
    return os.environ.get("CLAUDE_PROJECT_DIR") or as_dict(payload).get("cwd") or os.getcwd()


def sandbox_path(payload=None):
    return os.environ.get(SANDBOX_PATH_ENV) or os.path.join(human_dir(), "aws-sandbox.json")


def load_sandbox(payload=None):
    """The declared sandbox, or None. A malformed file is treated as none.

    Declared by the operator in ~/.claude/harness/<repo-id>/aws-sandbox.json
    (`scripts/harness_admin.py sandbox`), not in the project tree: a file the
    agent could write would let it declare production to be the sandbox.
    """
    try:
        with open(sandbox_path(payload)) as fh:
            cfg = json.load(fh)
    except Exception:
        return None
    account = str(as_dict(cfg).get("accountId") or "").strip()
    regions = [r for r in (as_dict(cfg).get("regions") or []) if isinstance(r, str) and r]
    if not re.fullmatch(r"\d{12}", account) or not regions:
        return None
    return {"accountId": account, "regions": regions}


def current_account(profile=None):
    """The account the active credentials resolve to, by asking STS. None on failure.

    The hook asks for itself rather than trusting anything the agent could have
    written down: this is the "assert the target" half of assert-then-act.
    """
    argv = [os.environ.get(AWS_BIN_ENV) or "aws", "sts", "get-caller-identity",
            "--output", "json", "--no-cli-pager"]
    if profile:
        argv += ["--profile", profile]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            return None
        return str(json.loads(proc.stdout).get("Account") or "") or None
    except Exception:
        return None


def command_hash(text):
    return hashlib.sha256(" ".join(str(text).split()).encode("utf-8")).hexdigest()[:16]


def approvals_dir(payload=None):
    return os.environ.get(APPROVALS_DIR_ENV) or os.path.join(human_dir(), "approvals")


def approved(payload, text):
    return os.path.exists(os.path.join(approvals_dir(payload), command_hash(text) + ".approved"))


def touches_human_records(command):
    """Does a shell line reach for the approval markers or the sandbox declaration?

    Those record a human decision. They sit outside the project tree and the
    sandbox denies writes there, but a session without the sandbox has only
    this: a command that addresses their directory, or runs the operator's
    admin script, is refused whatever it would do.

    Judged on the commands a line actually runs, with heredoc data set aside:
    writing a source file or a test that merely mentions these names is
    ordinary work (and was blocked by a first version that searched the raw
    text).
    """
    text = strip_data_heredocs(str(command or ""))
    places = [p for p in {approvals_dir(), sandbox_path(), human_dir(),
                          os.path.join(os.path.expanduser("~"), ".claude", "harness")} if p]
    marker = (os.path.join(".claude", "harness") + os.sep).casefold()
    interpreters = _SHELLS + ("python", "python3", "env")
    for raw in simple_commands(text):
        tokens = _command_position(raw)
        if not tokens:
            continue
        program = os.path.basename(tokens[0])
        if program.casefold() == "harness_admin.py":
            return True
        runs_script = program in interpreters or program.startswith("python")
        skip_next = False
        for tok in tokens[1:]:
            if skip_next:
                skip_next = False
                continue
            if program == "git" and tok in ("-m", "--message"):
                skip_next = True     # a commit message is prose, not a path
                continue
            folded = tok.casefold()
            if marker in folded or folded.rstrip("/").endswith(marker.rstrip(os.sep)):
                return True
            # Symlinks resolved and case folded: `~/.claude/HARNESS/` is the same
            # directory on a case-insensitive disk.
            if ("/" in tok or tok.startswith("~")) and any(
                    path_is_under(tok, place, fold=True) for place in places):
                return True
            if runs_script and os.path.basename(folded) == "harness_admin.py":
                return True
            if program in _SHELLS and " " in tok and touches_human_records(tok):
                return True      # the line handed to `bash -c`
    return False


def _refuse(p, kind, text, why):
    # The hash, not the path: the operator approves with a command of their own,
    # and an agent has no business knowing where the marker would go.
    block(EVENT, p, (
        "[aws-guard] Blocked a {kind} AWS action: {why}\n\n"
        "Only the operator can approve this exact command, from their own terminal:\n"
        "  python3 scripts/harness_admin.py approve {digest}\n"
        "Do not try to approve it yourself or reword the command to avoid the check. "
        "Report what you intended to run and why, and wait."
    ).format(kind=kind.upper(), why=why, digest=command_hash(text)))


def main():
    p = read_payload()
    tool = p.get("tool_name") or ""
    tool_input, unreadable = tool_input_of(p)
    if unreadable:
        allow()

    if tool == "Bash":
        text = tool_input.get("command")
        if not isinstance(text, str) or not text.strip():
            allow()
        if touches_human_records(text):
            block(EVENT, p, (
                "[aws-guard] Blocked: this command names the approval markers or the "
                "sandbox declaration. Those record a human decision and are the "
                "operator's alone to create, read or change."))
        kind, regions, profile, segments = classify_bash(text)
        if not segments:
            allow()
    else:
        kind, regions = classify_mcp(tool, tool_input)
        if kind is None:
            allow()
        profile = None
        text = "{} {}".format(tool, json.dumps(tool_input, sort_keys=True))

    if kind == READ:
        allow(EVENT, p, reason="aws-guard: read")

    try:
        if approved(p, text):
            allow(EVENT, p, reason="aws-guard: {} approved by marker {}".format(
                kind, command_hash(text)))

        who = caller_identity(p)
        if not who["is_lead"] and who["role"] not in WRITER_ROLES:
            _refuse(p, kind, text, (
                "only a devops teammate may change AWS resources, and you are '{}'. "
                "Hand the change to devops, or ask the lead."
            ).format(who["name"] or "an unnamed worker"))

        sandbox = load_sandbox(p)
        if sandbox is None:
            _refuse(p, kind, text, (
                "no sandbox account is declared (the operator declares one with "
                "`scripts/harness_admin.py sandbox`), so every change to AWS needs "
                "explicit human approval."))

        unstated = None in regions
        outside = sorted(r for r in regions if r and r not in sandbox["regions"])
        if unstated:
            _refuse(p, kind, text, (
                "the region is not stated. Pass --region (or set AWS_REGION inline) on "
                "every AWS call that changes something; the shell's ambient region has "
                "caused a wrong-region apply before. Sandbox regions: {}."
            ).format(", ".join(sandbox["regions"])))
        if outside:
            _refuse(p, kind, text, (
                "region {} is not a sandbox region ({})."
            ).format(", ".join(outside), ", ".join(sandbox["regions"])))

        account = current_account(profile)
        if account is None:
            _refuse(p, kind, text, (
                "the active AWS credentials could not be resolved to an account "
                "(sts get-caller-identity failed), so the target cannot be confirmed."))
        if account != sandbox["accountId"]:
            _refuse(p, kind, text, (
                "the active credentials are for account {}, not the sandbox account {}. "
                "Treat it as production."
            ).format(account, sandbox["accountId"]))

        allow(EVENT, p, reason="aws-guard: {} in sandbox {} {}".format(
            kind, account, ",".join(sorted(r for r in regions if r))))
    except SystemExit:
        raise
    except Exception as e:
        # FAIL CLOSED: this is a known write whose target we failed to verify.
        audit(EVENT, {}, "block", reason="aws-guard error (fail-closed): {}".format(e))
        sys.stderr.write(
            "[aws-guard] Internal error while verifying the target of an AWS change; "
            "blocked rather than allowed unverified: {}\n".format(e))
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL OPEN: nothing was established about the call
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
