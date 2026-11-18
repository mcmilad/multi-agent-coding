"""aws_guard: reads pass; a change needs a stated region and a verified sandbox account."""
import json
import os
import stat
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOKS = os.path.join(REPO, ".claude", "hooks")
HOOK = os.path.join(HOOKS, "aws_guard.py")
sys.path.insert(0, HOOKS)
import aws_guard  # noqa: E402

SANDBOX = {"accountId": "111122223333", "regions": ["ap-southeast-2"]}
AWS_MCP = "mcp__plugin_aws-core_aws-mcp__aws___run_script"


def setup(tmp_path, sandbox=SANDBOX, account="111122223333", sts_fails=False):
    """Environment with a declared sandbox and a fake `aws` that answers STS."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HOME=str(tmp_path / "home"), CLAUDE_PROJECT_DIR=str(project),
               CLAUDE_HARNESS_APPROVALS_DIR=str(tmp_path / "approvals"))
    sandbox_path = tmp_path / "aws-sandbox.json"
    if sandbox is not None:
        sandbox_path.write_text(sandbox if isinstance(sandbox, str) else json.dumps(sandbox))
    env["CLAUDE_HARNESS_AWS_SANDBOX_PATH"] = str(sandbox_path)
    fake = tmp_path / "fake-aws"
    calls = tmp_path / "aws-calls.log"
    body = "exit 255" if sts_fails else "echo '{{\"Account\": \"{}\", \"Arn\": \"x\"}}'".format(account)
    fake.write_text("#!/bin/sh\necho \"$@\" >> '{}'\n{}\n".format(calls, body))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env["CLAUDE_HARNESS_AWS_BIN"] = str(fake)
    return env, calls


def run(env, command=None, agent=None, tool="Bash", tool_input=None):
    payload = {"tool_name": tool,
               "tool_input": tool_input if tool_input is not None else {"command": command}}
    if agent:
        payload.update(agent_id="a{}-01".format(agent), agent_type=agent)
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


# --- classification ---------------------------------------------------------

@pytest.mark.parametrize("command", [
    "aws s3 ls", "aws ec2 describe-instances --region us-east-1",
    "aws sts get-caller-identity", "aws dynamodb scan --table-name t",
    "terraform plan -out tf.plan", "cdk synth", "npx --yes cdk diff", "sam build",
    "sam local invoke Fn", "terraform state list", "aws --version", "aws configure list",
    'git commit -m "aws s3 rm x; cdk destroy"', 'grep -rn "aws " docs | head',
    "echo sam deploy", "ls -la", "pytest -q",
])
def test_reads_and_non_aws_commands_classify_as_read(command):
    assert aws_guard.classify_bash(command)[0] == aws_guard.READ, command


@pytest.mark.parametrize("command", [
    "aws s3 cp a s3://b/a", "aws lambda update-function-code --function-name f",
    "aws iam create-role --role-name r", "aws lambda invoke --function-name f out.json",
    "cdk deploy --require-approval never", "sam deploy --no-confirm-changeset",
    "terraform apply -auto-approve", "aws cloudformation deploy --stack-name s",
    "X=1 sudo aws iam create-user --user-name u", "cd infra && npx --yes cdk deploy",
    "aws frobnicate do-something",
])
def test_changes_classify_as_write(command):
    assert aws_guard.classify_bash(command)[0] == aws_guard.WRITE, command


@pytest.mark.parametrize("command", [
    "aws s3 rm s3://b/k", "aws s3 rb s3://b --force", "aws ec2 terminate-instances --instance-ids i-1",
    "aws dynamodb delete-table --table-name t", "aws s3 sync . s3://b --delete",
    "cdk destroy --force", "sam delete --no-prompts", "terraform destroy -auto-approve",
    "terraform apply -destroy", "terraform state rm aws_s3_bucket.b",
    'bash -c "aws dynamodb delete-table --table-name t"',
    "ls; `aws ec2 terminate-instances --instance-ids i-1`",
    "true && aws kms disable-key --key-id k",
])
def test_destructive_commands_classify_as_destructive(command):
    assert aws_guard.classify_bash(command)[0] == aws_guard.DESTRUCTIVE, command


def test_the_worst_command_in_a_line_decides():
    kind, regions, _profile, segments = aws_guard.classify_bash(
        "aws s3 ls && aws s3 cp a s3://b --region ap-southeast-2; aws s3 rm s3://b/a")
    assert kind == aws_guard.DESTRUCTIVE and len(segments) == 3
    assert regions == {"ap-southeast-2", None}


def test_region_and_profile_are_read_from_flags_and_inline_env():
    _k, regions, profile, _s = aws_guard.classify_bash(
        "aws s3 cp a s3://b --region eu-west-1 --profile sbx")
    assert regions == {"eu-west-1"} and profile == "sbx"
    _k, regions, profile, _s = aws_guard.classify_bash(
        "AWS_PROFILE=sbx AWS_REGION=ap-southeast-2 terraform apply -auto-approve")
    assert regions == {"ap-southeast-2"} and profile == "sbx"


def test_mcp_scripts_are_classified_by_the_operations_they_name():
    read = {"code": "await call_boto3(service_name='s3', operation_name='ListBuckets')"}
    write = {"code": "await call_boto3(service_name='s3', operation_name='PutObject', "
                     "region_name='ap-southeast-2')"}
    destroy = {"code": "await call_boto3(service_name='ec2', operation_name='TerminateInstances')"}
    opaque = {"code": "op = build(); await call_boto3(service_name='s3', operation_name=op)"}
    plain = {"code": "result = {'x': 1}\nresult"}
    assert aws_guard.classify_mcp(AWS_MCP, read)[0] == aws_guard.READ
    # I3: double-quoted names, as scripts are normally written. JSON-encoding the
    # input escaped the quotes and every such read was classified as a write.
    dq_read = {"code": 'await call_boto3(service_name="s3", operation_name="ListBuckets")'}
    dq_write = {"code": 'r = await call_boto3(service_name="s3", operation_name="PutObject",\n'
                        '                     region_name="ap-southeast-2")'}
    assert aws_guard.classify_mcp(AWS_MCP, dq_read)[0] == aws_guard.READ
    assert aws_guard.classify_mcp(AWS_MCP, dq_write) == (aws_guard.WRITE, {"ap-southeast-2"})
    dict_form = {"code": 'await call_boto3(**{"service_name": "s3", "operation_name": "GetObject"})'}
    assert aws_guard.classify_mcp(AWS_MCP, dict_form)[0] == aws_guard.READ
    kind, regions = aws_guard.classify_mcp(AWS_MCP, write)
    assert kind == aws_guard.WRITE and regions == {"ap-southeast-2"}
    assert aws_guard.classify_mcp(AWS_MCP, destroy)[0] == aws_guard.DESTRUCTIVE
    assert aws_guard.classify_mcp(AWS_MCP, opaque)[0] == aws_guard.WRITE
    assert aws_guard.classify_mcp(AWS_MCP, plain)[0] == aws_guard.READ
    assert aws_guard.classify_mcp("mcp__github__run_script", write) == (None, None)
    assert aws_guard.classify_mcp(
        "mcp__plugin_aws-serverless_aws-serverless-mcp__sam_deploy", {})[0] == aws_guard.WRITE
    assert aws_guard.classify_mcp(
        "mcp__plugin_aws-core_aws-mcp__aws___search_documentation", {})[0] == aws_guard.READ


# --- decisions --------------------------------------------------------------

def test_a_read_is_allowed_without_touching_sts(tmp_path):
    env, calls = setup(tmp_path)
    assert run(env, "aws s3 ls").returncode == 0
    assert not calls.exists(), "a read must not cost an STS call"


def test_a_write_in_the_sandbox_is_allowed_for_the_lead(tmp_path):
    env, calls = setup(tmp_path)
    proc = run(env, "aws s3 cp a s3://b/a --region ap-southeast-2")
    assert proc.returncode == 0, proc.stderr
    assert "sts get-caller-identity" in calls.read_text()


def test_a_write_in_the_sandbox_is_allowed_for_devops(tmp_path):
    env, _ = setup(tmp_path)
    assert run(env, "sam deploy --region ap-southeast-2", agent="devops-1").returncode == 0


def test_a_teardown_in_the_sandbox_is_allowed(tmp_path):
    env, _ = setup(tmp_path)
    assert run(env, "AWS_REGION=ap-southeast-2 cdk destroy --force",
               agent="devops-1").returncode == 0


@pytest.mark.parametrize("worker", ["coding-2", "review-1", "sa-1", "payload-probe"])
def test_non_devops_workers_cannot_change_aws(tmp_path, worker):
    env, calls = setup(tmp_path)
    proc = run(env, "aws s3 cp a s3://b/a --region ap-southeast-2", agent=worker)
    assert proc.returncode == 2
    assert "only a devops teammate" in proc.stderr and worker in proc.stderr
    assert not calls.exists()


def test_a_write_with_no_stated_region_is_blocked(tmp_path):
    env, _ = setup(tmp_path)
    proc = run(env, "aws s3 cp a s3://b/a")
    assert proc.returncode == 2 and "region is not stated" in proc.stderr


def test_a_write_to_another_region_is_blocked(tmp_path):
    env, _ = setup(tmp_path)
    proc = run(env, "terraform apply -auto-approve", agent="devops-1")
    assert proc.returncode == 2
    proc = run(env, "AWS_REGION=us-east-1 terraform apply -auto-approve", agent="devops-1")
    assert proc.returncode == 2 and "us-east-1 is not a sandbox region" in proc.stderr


def test_a_write_with_another_accounts_credentials_is_blocked(tmp_path):
    env, _ = setup(tmp_path, account="999988887777")
    proc = run(env, "aws s3 cp a s3://b/a --region ap-southeast-2", agent="devops-1")
    assert proc.returncode == 2
    assert "999988887777" in proc.stderr and "production" in proc.stderr


def test_a_write_is_blocked_when_the_account_cannot_be_resolved(tmp_path):
    env, _ = setup(tmp_path, sts_fails=True)
    proc = run(env, "aws s3 cp a s3://b/a --region ap-southeast-2")
    assert proc.returncode == 2 and "could not be resolved" in proc.stderr


def test_the_profile_on_the_command_is_the_one_checked(tmp_path):
    env, calls = setup(tmp_path)
    run(env, "aws s3 cp a s3://b/a --region ap-southeast-2 --profile sbx")
    assert "--profile sbx" in calls.read_text()


def test_with_no_sandbox_declared_every_write_needs_approval(tmp_path):
    env, _ = setup(tmp_path, sandbox=None)
    proc = run(env, "aws s3 cp a s3://b/a --region ap-southeast-2")
    assert proc.returncode == 2 and "no sandbox account is declared" in proc.stderr
    assert run(env, "aws s3 ls").returncode == 0


@pytest.mark.parametrize("bad", ["{not json", json.dumps({"accountId": "12", "regions": ["x"]}),
                                 json.dumps({"accountId": "111122223333", "regions": []})])
def test_a_malformed_sandbox_file_is_treated_as_no_sandbox(tmp_path, bad):
    env, _ = setup(tmp_path, sandbox=bad)
    assert run(env, "aws s3 cp a s3://b/a --region ap-southeast-2").returncode == 2


def _digest(stderr):
    import re
    return re.search(r"harness_admin\.py approve ([0-9a-f]{16})", stderr).group(1)


def test_an_approval_marker_allows_exactly_that_command(tmp_path):
    env, _ = setup(tmp_path, account="999988887777")
    command = "aws s3 rm s3://prod-bucket/key --region us-east-1"
    proc = run(env, command)
    assert proc.returncode == 2
    digest = _digest(proc.stderr)
    assert digest == aws_guard.command_hash(command)
    approvals = tmp_path / "approvals"
    approvals.mkdir(exist_ok=True)
    (approvals / (digest + ".approved")).write_text("approved\n")
    assert run(env, command).returncode == 0
    assert run(env, command + " --recursive").returncode == 2, "approval is per command"


def test_the_block_gives_the_operator_an_id_and_the_agent_no_path(tmp_path):
    """I4: the message used to print the marker's path, and the agent it had
    just blocked could create the file with one `touch`."""
    env, _ = setup(tmp_path, sandbox=None)
    proc = run(env, "cdk deploy --region ap-southeast-2")
    assert "Do not try to approve it yourself" in proc.stderr
    assert "harness_admin.py approve" in proc.stderr
    assert str(tmp_path / "approvals") not in proc.stderr and ".approved" not in proc.stderr


def _human_env(tmp_path, **kw):
    env, calls = setup(tmp_path, **kw)
    env.pop("CLAUDE_HARNESS_APPROVALS_DIR", None)
    env.pop("CLAUDE_HARNESS_AWS_SANDBOX_PATH", None)
    env["CLAUDE_HARNESS_HUMAN_DIR"] = str(tmp_path / "human")
    return env


@pytest.mark.parametrize("template", [
    "touch {human}/approvals/0123456789abcdef.approved",
    "echo ok > {human}/approvals/0123456789abcdef.approved",
    "cat {human}/aws-sandbox.json",
    "ls ~/.claude/harness/",
    "python3 scripts/harness_admin.py approve 0123456789abcdef",
    "env X=1 python3 ./scripts/harness_admin.py show",
    "./scripts/harness_admin.py approve 0123456789abcdef",
    "bash -c 'python3 scripts/harness_admin.py approve 0123456789abcdef'",
    "true && cp x {human}/aws-sandbox.json",
    # review cycle 2, N1: the wrapper's option value was taken for the program
    "env -u CLAUDECODE python3 scripts/harness_admin.py approve 0123456789abcdef",
    "env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT ./scripts/harness_admin.py show",
    "env -i PATH=/usr/bin python3 scripts/Harness_Admin.py approve 0123456789abcdef",
    "sudo -u root python3 scripts/harness_admin.py show",
    # review cycle 2, R2: another spelling of the same directory
    "touch ~/.claude/HARNESS/x/approvals/0123456789abcdef.approved",
])
def test_a_command_that_reaches_for_the_human_records_is_refused(tmp_path, template):
    env = _human_env(tmp_path)
    command = template.format(human=tmp_path / "human")
    for agent in (None, "devops-1", "coding-1"):
        proc = run(env, command, agent=agent)
        assert proc.returncode == 2, (command, agent)
        assert "operator's alone" in proc.stderr


@pytest.mark.parametrize("command", [
    "cat > scripts/harness_admin.py <<'EOF'\nprint('approve .approved aws-sandbox.json')\nEOF",
    "python3 - <<'EOF'\nopen('t.py','w').write('harness_admin.py approve')\nEOF",
    "grep -rn harness_admin tests/",
    "git add scripts/harness_admin.py && git commit -m 'add harness_admin.py approve flow'",
    "pytest tests/test_harness_admin.py -q",
    # N1: a commit message is prose
    "git commit -m 'approvals now live in ~/.claude/harness/<repo-id>/'",
    "git commit --message 'move records to ~/.claude/harness/'",
])
def test_working_on_the_admin_script_or_mentioning_it_is_not_refused(tmp_path, command):
    """The first version searched the raw text and blocked the commit that added it."""
    assert run(_human_env(tmp_path), command).returncode == 0, command


def test_a_self_made_marker_in_the_project_tree_approves_nothing(tmp_path):
    env = _human_env(tmp_path, account="999988887777")
    command = "aws s3 rm s3://prod-bucket/key --region us-east-1"
    project_marker = tmp_path / "project" / ".claude" / "approvals"
    project_marker.mkdir(parents=True)
    (project_marker / (aws_guard.command_hash(command) + ".approved")).write_text("x")
    assert run(env, command).returncode == 2


def test_a_sandbox_declaration_in_the_project_tree_is_ignored(tmp_path):
    env = _human_env(tmp_path, sandbox=None)
    (tmp_path / "project" / ".claude" / "aws-sandbox.json").write_text(json.dumps(SANDBOX))
    proc = run(env, "aws s3 cp a s3://b/a --region ap-southeast-2")
    assert proc.returncode == 2 and "no sandbox account is declared" in proc.stderr


def test_an_mcp_write_follows_the_same_rules(tmp_path):
    env, _ = setup(tmp_path)
    write = {"code": "await call_boto3(service_name='s3', operation_name='PutObject', "
                     "region_name='ap-southeast-2')"}
    assert run(env, tool=AWS_MCP, tool_input=write, agent="devops-1").returncode == 0
    assert run(env, tool=AWS_MCP, tool_input=write, agent="coding-1").returncode == 2
    no_region = {"code": "await call_boto3(service_name='s3', operation_name='PutObject')"}
    assert run(env, tool=AWS_MCP, tool_input=no_region, agent="devops-1").returncode == 2
    read = {"code": "await call_boto3(service_name='s3', operation_name='ListBuckets')"}
    assert run(env, tool=AWS_MCP, tool_input=read, agent="coding-1").returncode == 0
    dq_read = {"code": 'await call_boto3(service_name="s3", operation_name="ListBuckets")'}
    assert run(env, tool=AWS_MCP, tool_input=dq_read, agent="coding-1").returncode == 0


def test_other_tools_and_garbage_are_left_alone(tmp_path):
    env, _ = setup(tmp_path)
    assert run(env, tool="Write", tool_input={"file_path": "/x", "content": "aws s3 rm"}).returncode == 0
    assert run(env, tool="Bash", tool_input={"command": ""}).returncode == 0
    proc = subprocess.run([sys.executable, HOOK], input="{nope", capture_output=True,
                          text=True, env=env)
    assert proc.returncode == 0


def test_the_rule_file_documents_what_the_hook_enforces():
    text = open(os.path.join(REPO, ".claude", "rules", "AWS-security-guidelines.md")).read()
    assert "## Production Safeguards" in text
    for phrase in ("aws_guard.py", "harness_admin.py sandbox", "--region",
                   "Only devops changes AWS", "Never try to approve it yourself"):
        assert phrase in text, phrase


def test_a_data_heredoc_is_not_scanned_but_a_shell_heredoc_is():
    data = "python3 - <<'EOF'\nrun(env, 'x'); aws s3 rm s3://b/k\nprint(1)\nEOF\necho done"
    assert aws_guard.classify_bash(data)[0] == aws_guard.READ
    script = "bash <<EOF\naws s3 rm s3://b/k\nEOF"
    assert aws_guard.classify_bash(script)[0] == aws_guard.DESTRUCTIVE
    after = "cat > f.txt <<EOF\nnotes\nEOF\naws s3 rm s3://b/k"
    assert aws_guard.classify_bash(after)[0] == aws_guard.DESTRUCTIVE, \
        "a command after the heredoc is still a command"


def test_the_guard_is_wired_for_bash_and_aws_mcp_tools():
    import re
    settings = json.load(open(os.path.join(REPO, ".claude", "settings.json")))
    matchers = [h["matcher"] for h in settings["hooks"]["PreToolUse"]
                if any("aws_guard.py" in x["command"] for x in h["hooks"])]
    assert "Bash" in matchers
    mcp = [m for m in matchers if m != "Bash"]
    assert mcp and all(re.search(m, AWS_MCP) for m in mcp)
    assert any(re.search(m, "mcp__plugin_aws-serverless_aws-serverless-mcp__sam_deploy")
               for m in mcp)


# --- review cycle 2 ---------------------------------------------------------

@pytest.mark.parametrize("command,program", [
    ("env -u CLAUDECODE aws s3 rb s3://b --force", "aws"),
    ("env -u A -u B X=1 terraform destroy -auto-approve", "terraform"),
    ("sudo -u deploy aws s3 rm s3://b/k", "aws"),
    ("xargs -n 1 aws s3 rm", "aws"),
    ("env -i PATH=/usr/bin cdk destroy --force", "cdk"),
])
def test_a_wrappers_option_value_is_not_mistaken_for_the_program(command, program):
    """N1: `env -u NAME cmd` left NAME in command position and `cmd` unjudged."""
    kind, _regions, _profile, segments = aws_guard.classify_bash(command)
    assert kind == aws_guard.DESTRUCTIVE, command
    assert segments and segments[0][0] == program


def test_a_script_mixing_a_literal_read_with_an_operation_in_a_variable_is_a_write():
    """N2: one readable read used to make the whole script a read."""
    mixed = {"code": 'await call_boto3(service_name="s3", operation_name="ListBuckets")\n'
                     'op = "Delete" + "Bucket"\n'
                     'await call_boto3(service_name="s3", operation_name=op, region_name="x")'}
    assert aws_guard.classify_mcp(AWS_MCP, mixed)[0] == aws_guard.WRITE
    kwargs = {"code": 'await call_boto3(service_name="s3", operation_name="ListBuckets")\n'
                      'await call_boto3(**params)'}
    assert aws_guard.classify_mcp(AWS_MCP, kwargs)[0] == aws_guard.WRITE
    two_reads = {"code": 'await call_boto3(service_name="s3", operation_name="ListBuckets")\n'
                         'await call_boto3(service_name="ec2", operation_name="DescribeVpcs")'}
    assert aws_guard.classify_mcp(AWS_MCP, two_reads)[0] == aws_guard.READ
