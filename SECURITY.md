# Security

This document provides a security overview of the Claude Code Multi-Agent Development Sample, including a threat model, AI security controls, and risk assessment.

> **Note**: This repository is a sample configuration — not a production application. The security analysis below documents risks inherent in multi-agent AI workflows and the mitigations built into this configuration.

## What Enforces, and What the Hooks Are For

The Python hooks in `.claude/hooks/` catch honest mistakes: a transition nobody verified, an edit to a file another agent holds, an AWS change aimed at the wrong account. They are guidance with an audit trail, not a security boundary: a hook is a script reading a tree an agent can write. Enforcement comes from four boundaries no agent can write, which the operator sets up (see the README's [Safety model](README.md#safety-model)):

1. **The Bash sandbox** configured in `.claude/settings.json` (filesystem and network allowlists, credential denials, no unsandboxed escape)
2. **GitHub**: branch protection on `main` requiring CI and review, and `CODEOWNERS` for the harness files
3. **Jira permissions**: the agents' account cannot transition issues to `Done` on its own
4. **AWS credentials** that exist only in a sandbox account

## Threat Model

### 1. Agent Isolation and Trust Boundaries

**Threat:** Agents execute in the same Claude Code session with shared file system access. An agent operating outside defined scope due to configuration error or logic fault may perform unauthorized file system modifications beyond its assigned scope.

**Mitigations** (require user configuration and ongoing verification):
- Agent team protocol (`.claude/rules/agent-team-protocol.md`) restricts each agent to the files listed in its Jira issue, and the claim gate (`claim_gate.py`) blocks an edit to a file another agent's issue holds — users must keep issue `Files:` lists accurate
- Verification gate requires agents to confirm they only modified assigned files before marking tasks complete — users must enforce this gate in their workflow
- `review-agent` independently verifies changes match task scope — users must include review-agent in their team composition
- Claude Code's permission system prompts the user before executing potentially dangerous operations — users must review and respond to these prompts

### 2. Credential Scope and AWS Access

**Threat:** Agents interacting with AWS services may perform unauthorized privilege escalation through IAM policy exploitation, access resources outside authorized scope, or execute unauthorized modifications to production infrastructure.

**Mitigations** (require user configuration and ongoing verification):
- AWS security guidelines (`.claude/rules/AWS-security-guidelines.md`) mandate ReadOnly/least-privilege credentials for read operations — users must configure appropriate AWS profiles and IAM policies
- Agents must assume resources are production unless proven otherwise — users must verify agents follow this convention
- The AWS guard (`aws_guard.py`) allows changes only from the lead or a devops agent, with an explicit region, inside the sandbox account the operator declares with `scripts/harness_admin.py`; anything else is blocked until the operator approves that exact command from their own terminal — users must not approve commands they have not read
- AWS Security Token Service (AWS STS) identity verification via `aws sts get-caller-identity` is required before any AWS operation — users must ensure credentials are correctly scoped
- Safety controls (termination protection, deletion protection, MFA delete) must never be disabled without user confirmation — users must review all safety control changes

### 3. File System Access

**Threat:** Agents have read/write access to the local file system and may perform unauthorized file system modifications to system configuration files, overwrite uncommitted work, or access sensitive data outside authorized scope.

**Mitigations** (require user configuration and ongoing verification):
- Claude Code's built-in permission system gates file operations — users must review permission prompts and not grant overly broad access
- Spec-driven workflow constrains agents to spec-defined file paths — users must define accurate file paths in specs
- Agent team protocol requires agents to stay within task scope — users must verify compliance during review
- The `.claude/rules/execution-hygiene.md` rule requires agents to run commands without bypassing confirmation prompts — users must keep the rule available in their configuration

### 4. MCP Server Trust

**Threat:** MCP servers are external services that agents communicate with. MCP server compromise may result in data manipulation, prompt injection attacks, or unauthorized data exfiltration.

**Mitigations** (require user configuration and ongoing verification):
- All configured MCP servers are either official AWS services or well-known public services — users must vet any additional MCP servers before adding them
- MCP servers run as local processes outside the Bash sandbox. The AWS MCP server can call AWS APIs; its changing calls pass through the AWS guard like CLI commands. Users must vet every server they add and prefer per-machine registration (`claude mcp add --scope local`) over a committed `.mcp.json`, whose commands an agent could edit
- Claude Code flags suspected prompt injection in tool results — users must review flagged results and act accordingly
- Users can review and approve/deny MCP server tool calls via the permission system — users must actively monitor and respond to these prompts

### 5. Plugin Security

**Threat:** Plugins execute code within the Claude Code session and may access data outside authorized scope boundaries.

**Mitigations** (require user configuration and ongoing verification):
- All configured plugins come from vetted marketplaces — the official Claude Code marketplace (`claude-plugins-official`). Users must review plugin permissions before enabling and vet any additional marketplaces before adding them
- Plugins operate within Claude Code's permission framework — users must maintain appropriate permission settings
- The `pr-review-toolkit` plugin provides specialized security and quality review — users must include this plugin in their configuration and act on its findings

### 6. Enforcement Hook Execution

**Threat:** The hooks (`.claude/hooks/*.py`) run as local Python processes with the user's privileges, outside the Bash sandbox and outside the tool-call permission prompts. Their input is a JSON payload carrying agent-influenceable fields (tool arguments, Jira text, file paths). Hook logic that consumed these fields unsafely could create a code-execution or file-modification path outside the sandbox.

**Design properties** (covered by the test suite):
- **Two failure modes, chosen per hook.** Observational hooks fail open: an internal error allows the action, so a hook bug cannot trap a session. The verification gate and the AWS guard fail closed once they know an action is one they police, because there a silent pass is the worse failure. Every decision, including a hook error, is logged to `~/.claude/logs/team-hooks.jsonl`, and `scripts/doctor.py` reports recent hook errors.
- **A fixed, small set of programs.** Hooks never run a shell, evaluate code, or run a project tool such as a linter or test runner. The only processes they start are `git` (`ls-files`, `rev-parse`, with repository hooks, filesystem monitors and remote protocols disabled), `aws sts get-caller-identity` (to resolve which account the credentials belong to), and, in the session self-test, each hook script on empty input and the `claude` CLI to list plugins. A test fails if that list changes.
- **Untrusted path components are sanitised.** Issue keys, project keys, session and agent names are reduced to `[A-Za-z0-9._-]` before they become part of a state path, and state files are written atomically without following links, so a crafted value cannot reach outside the state directory.
- **The harness protects itself.** The guardrail guard blocks file-tool edits to hooks, settings, agent definitions, rules and skills, the gate and capability configuration, CI workflows and the admin script outside a maintenance session the operator launches; the sandbox denies the same paths to the shell.

**Mitigations** (require user configuration and ongoing verification):
- Users must review `.claude/hooks/*.py` before installing them and on every update, exactly as they would any code they run locally
- Hooks are wired through `$CLAUDE_PROJECT_DIR/.claude/hooks/...` in the bundled `.claude/settings.json`; keep that directory non-writable by untrusted processes
- The bypass labels (`skip-format-check`, `skip-verify`) are honoured only when the lead applies them; treat their presence on an issue as a reviewable signal, not boilerplate

### 7. Jira Admin Credential Scope

**Threat:** `JIRA_API_TOKEN`, used only by `scripts/jira_bootstrap.py`, acts with the operator's **full Jira permissions** — far beyond the Atlassian MCP's `read:jira-work`/`write:jira-work` OAuth grant that agents use at runtime. Every `Bash` tool call an agent runs is a child process of the shell that launched Claude Code, so a token exported into that shell is inherited by every agent subprocess in the session and readable via `env`, `printenv`, or any child process — a single second credential that, if mishandled, hands out far more than the MCP grant ever would.

**Mitigations** (require user configuration and ongoing verification):
- The one real confinement is *where the token is exported*: running `scripts/jira_bootstrap.py` (including the `sprint-open`/`sprint-close` admin-plane calls mid-run) from a terminal **separate** from the one that launches Claude Code keeps the token out of the agent session's environment entirely — see the README's [Jira setup](README.md#jira-setup) section; the shipped sandbox also denies agents the `JIRA_API_TOKEN` and `JIRA_EMAIL` variables
- Beyond that, confinement is a convention plus an instruction, not a mechanism: `fullstack-agent` is told never to hold, echo, or pass on the token, and to be the only actor that would ever run bootstrap — nothing in the hooks or the harness checks or enforces this
- If the token is ever exported into the Claude Code shell (or bootstrap is run through the lead instead of a separate terminal), users must treat it as exposed to every agent in the session and rotate/scope it accordingly

### 8. Untrusted Content from Jira

**Threat:** Issue descriptions and comments read via the Atlassian MCP may be authored by any third party with access to the Jira project, and flow directly into agent context whenever an agent reads an issue. This is the same class of risk as Threat §4 (MCP Server Trust) — untrusted external content reaching agent context as a prompt-injection vector — but the migration opens a new instance of it: Jira issue text is now a first-class, third-party-writable input to every teammate's working context, not just an MCP tool result.

**Mitigations:** the always-on rule `.claude/rules/untrusted-content.md` tells every agent to treat Jira text, web pages and tool output as data, never as instructions, and to report embedded instructions instead of following them. A hook (`untrusted_content_flag.py`) flags tool results containing instruction-shaped text; it never blocks, and unflagged content is still untrusted. An issue's `Files:` and `Run:` are honoured only because the lead authored them, and the format check blocks a worker from rewriting them.

### 9. Closing Work Without Review

**Threat:** Jira transitions in a team-managed project are global (any status to any status), so an implementer could try to move its own issue straight to `Done` and skip review.

**Mitigations:** the verification gate blocks a transition to `Done` unless the caller is a reviewer or the lead and the evidence was produced by a `--review` run, which includes the full test suite. Like every hook, this catches mistakes rather than a determined agent; the enforcing control is Jira permissions (the agents' account cannot close issues alone) and branch protection with CI on the pull request.

## AI Security Controls

### Input Validation

- Claude Code's built-in safety system validates user prompts and agent communications
- The system flags suspected prompt injection attempts in tool results and MCP responses
- Agent instructions include explicit constraints on scope and behavior (each agent's markdown definition)

### Output Filtering

- Claude Code applies content safety measures to all generated output
- `review-agent` independently verifies implementation correctness and security
- The spec-driven workflow requires explicit PASS from `review-agent` before work is accepted

### Rate Limiting and Abuse Prevention

- Claude Code sessions are governed by Anthropic's API rate limits and usage policies
- Review is diff-only against fixed requirements, with one follow-up round before findings are escalated to the user or moved to the backlog
- Same-blocker-twice rule forces escalation rather than infinite retry loops

### Monitoring and Logging

- All agent interactions occur within the Claude Code session and are visible to the user
- Backlog coordination is Jira (issues, transitions, comments); the audit trail is `~/.claude/logs/team-hooks.jsonl` (every enforcement hook decision) plus the Jira issue history itself
- `decisions.md` logs mid-flight architectural decisions
- Review verdicts are posted as comments on the sprint's `role-review` issue in Jira, one per review cycle

### Incident Response

1. **Suspicious agent behavior**: User can deny any tool call via Claude Code's permission system
2. **Unexpected AWS changes**: Production safety rules require confirmation for destructive operations; use `aws cloudformation describe-stack-events` to audit
3. **Compromised MCP server**: Remove it (`claude mcp remove <name>`, or disable the plugin that provides it) and restart the session
4. **Review failure loop**: A finding that survives the follow-up round is escalated to the user

## Bias and Fairness

This section addresses bias and fairness considerations for the multi-agent system. Because this repository is a **sample configuration** — not a production application that processes user data or makes consequential decisions — many traditional bias concerns do not directly apply. The agents generate infrastructure code and review it; they do not make decisions about people.

### Code Generation and Architectural Decisions

The agents rely on Claude models, which may reflect biases present in their training data. In practice, this could surface as:
- Defaulting to specific AWS services, patterns, or regions without considering alternatives
- Over-engineering or under-engineering based on patterns seen in training data

**Mitigation:** The spec-driven workflow requires explicit architectural decisions documented in `design.md` and `decisions.md`. The `sa-agent` provides Well-Architected reviews that challenge assumptions. All decisions are visible to the user for override.

### Task Delegation and Review Processes

Task delegation is deterministic — issues are assigned by the `role-*` label plus the matching `[role]` tag in the issue summary, enforced by the format-check hook at issue creation, not manually assigned by the team lead in a file. The `review-agent` applies the same review criteria to all code regardless of which agent produced it. There is no adaptive or learned behavior that could develop bias over time.

### Monitoring for Biased Outputs

- All agent outputs are visible in the Claude Code session
- `review-agent` independently evaluates all implementations against the same criteria
- `decisions.md` provides an audit trail of architectural choices for human review
- Persistent review disagreements are escalated to the user

### Human Oversight Mechanisms

- Claude Code's permission system requires user approval for potentially impactful operations
- The spec-driven workflow has explicit human review points (spec approval, diff review, deployment approval)
- Production safety rules gate all destructive AWS operations behind user confirmation
- Users can deny any tool call and override any agent decision

### Known Model Limitations

The Claude models used by these agents have known limitations regarding bias:
- Training data biases may influence code style, library choices, and architectural preferences
- Models may not equally represent all programming paradigms, languages, or cloud providers
- Generated code patterns may reflect biases toward certain community conventions over others
- Models have a knowledge cutoff and may not reflect the latest best practices

Users should treat agent outputs as recommendations subject to human judgment, not as authoritative decisions.

## Claude Code Responsibility Model

> **Important**: The security controls described in this document are required controls that users must actively implement, configure, and maintain. Failure to implement these controls may result in unauthorized access, data exposure, or unintended infrastructure modifications.

### Anthropic/Claude Code Platform Responsibilities

Anthropic is responsible for the following platform-level security controls:

- Claude Code platform security and infrastructure
- API authentication and authorization
- Model safety controls and content filtering
- Rate limiting and abuse prevention
- Prompt injection detection in tool results

### User Responsibilities

Users MUST implement and maintain the following security controls. Failure to implement these controls may result in unauthorized access, data exposure, or unintended infrastructure modifications:

- Secure agent configuration and rule definitions — users must define and maintain agent scope restrictions
- AWS credential management and least-privilege IAM policies — users must configure appropriate IAM roles and rotate credentials
- MCP server security review and approval — users must vet every MCP server before registering it
- File system access controls and sensitive data protection — users must review and respond to Claude Code permission prompts
- Production safety rule enforcement — users must not grant blanket approval for destructive operations
- Enforcement hook review — users must read `.claude/hooks/*.py` before installing them and on every update (they run as local subprocesses with the user's privileges, outside the tool-call permission prompts), and keep the installed hook directory non-writable by untrusted processes
- Ongoing verification that security controls remain correctly configured — users must periodically audit agent configurations and access policies

## AWS Shared Responsibility Model

When agents interact with AWS services via MCP servers or CLI commands, the standard [AWS Shared Responsibility Model](https://aws.amazon.com/compliance/shared-responsibility-model/) applies.

**Core Principle:** AWS is responsible for **security OF the cloud** (physical infrastructure, compute, storage, networking, and managed service infrastructure). Users are responsible for **security IN the cloud** (customer data, IAM policies, OS patching, network configuration, and encryption).

### Security OF the Cloud (AWS Responsibility)

- Physical security of data centers and global infrastructure
- Managed service infrastructure (compute, storage, networking, databases)
- Hypervisor and host OS security for managed services
- Service availability and durability guarantees

### Security IN the Cloud (User Responsibility)

- **Identity and access management**: Least-privilege IAM policies, credential rotation, MFA enforcement
- **Data protection**: Encryption key management, data classification, retention policies
- **OS and application patching**: EC2 instance OS updates, security groups, instance metadata controls
- **Network configuration**: VPC design, subnet isolation, security group rules, NACLs
- **Service-specific configuration**: S3 encryption and public access blocks, database authentication and audit logging
- **Monitoring and compliance**: AWS CloudTrail, AWS Config rules, Amazon GuardDuty, AWS Security Hub

## Implementation Guide

Implement security controls in phased priority order. See `.claude/agents/devops-agent.md` (Data Security) and `.claude/rules/AWS-security-guidelines.md` for the detailed S3 implementation pattern.

### Phase 1: Credential Scope (Threat Model §2)

Configure least-privilege IAM for agent operations:
```bash
aws iam create-policy --policy-name claude-code-readonly --policy-document file://readonly-policy.json
aws iam attach-user-policy --user-name claude-code-user --policy-arn arn:aws:iam::<account>:policy/claude-code-readonly
```
Verify: `aws iam simulate-principal-policy --policy-source-arn arn:aws:iam::<account>:user/claude-code-user --action-names s3:DeleteBucket ec2:TerminateInstances --resource-arns "*"` (expect: Deny for all write/delete operations)

### Phase 2: File System Access (Threat Model §3)

Configure Claude Code permission settings to restrict file operations:
- Keep `.claude/settings.local.json` free of broad allow rules (`Bash(python3 *)`, `Bash(curl *)`), so the sandbox and permission prompts stay in force
- Define allowed paths in agent task assignments via each Jira issue's `Files:` section
- Verify: `git diff --name-only` lists only files assigned in the agent's task scope (expect: 100% match with task assignments)
- Audit: review-agent checks modified files against task scope each review cycle

### Phase 3: MCP Server Trust (Threat Model §4)

Vet every MCP server before registering it:
- Review the server's source or documentation for data handling, and prefer servers from the service's own maintainer
- Pin versions (no `@latest`) and register per machine (`claude mcp add --scope local`) rather than in a committed `.mcp.json`
- List what is registered and remove anything unreviewed: `claude mcp list` (expect: only servers you have reviewed and approved)
- Check the configured profiles: `python3 scripts/doctor.py --profile serverless,frontend`

### Acceptance Criteria

Each phase must be verified before proceeding to the next:
- Phase 1: `aws sts get-caller-identity` returns a least-privilege role; `aws iam simulate-principal-policy` returns Deny for unused actions
- Phase 2: `review-agent` confirms all file modifications are within task scope
- Phase 3: Every server in `claude mcp list` has completed security review

## Risk Assessment

### Security Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Agent modifies files outside task scope | Low | Medium | Agent protocol file restrictions + review-agent verification |
| AWS credential misuse by agent | Low | High | Production safety rules + ReadOnly defaults + user confirmation gates |
| Hardcoded secrets in generated code | Medium | High | review-agent security checks + pr-review-toolkit plugin |
| MCP server returns manipulated data | Low | Medium | Prompt injection detection + user-visible tool results |
| Plugin accesses unintended data | Low | Low | Claude Code permission framework + marketplace-only plugins |
| Enforcement hook path traversal via crafted `projectKey`/`issueIdOrKey` or `team_name`/`teammate_name` | Low | Medium | `safe_path_component()` basename + allowlist sanitization confines writes/deletes to `$STATE/verified/` and `~/.claude/logs/idle-nudges/` |
| Tampered/crashing enforcement hook stops enforcing | Low | Medium | Guardrail guard + sandbox deny writes to hooks; hook errors logged to `team-hooks.jsonl` and reported by `scripts/doctor.py`; the four boundaries do not depend on hooks |
| `JIRA_API_TOKEN` exported into the Claude Code shell is inherited by every agent `Bash` subprocess | Medium | High | Separate-terminal export (README Jira setup) is the one real mechanism; otherwise convention + instruction only |
| Untrusted issue/comment content from Jira reaches agent context (prompt injection) | Medium | Medium | `untrusted-content` rule + flagging hook + Claude Code's prompt-injection detection (Threat §8) |
| An implementer closes its own issue without review | Low | Medium | Verification gate requires reviewer or lead plus review evidence for `Done`; Jira permissions enforce it (Threat §9) |

### Compliance Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Sensitive data in agent-generated code | Medium | High | review-agent checks + pr-review-toolkit plugin + AWS-security-guidelines rule |
| AWS service usage without proper authorization | Low | Medium | Production safety rules + credential verification before operations |
| Data handling by AI agents | Medium | Medium | All processing occurs locally in the user's Claude Code session |

### Operational Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Agent coordination failure | Medium | Low | Jira board + atomic claim locks + SendMessage protocol + blocker escalation |
| Uncontrolled AWS resource creation | Low | High | CDK diff review + user confirmation + cost-center tagging |
| Review cycle infinite loop | Low | Low | One follow-up round, then escalate or backlog |
| Agent generates incorrect IaC | Medium | Medium | cdk synth + cdk diff + review-agent + sa-agent Well-Architected review |
