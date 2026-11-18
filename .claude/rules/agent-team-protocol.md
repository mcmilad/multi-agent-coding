# Agent Team Protocol

Shared protocol for all agent team teammates. The team lead (`fullstack-agent`) coordinates; all other agents are teammates. This file is loaded as a global rule for every session — every teammate inherits it as priming, no `Skill` invocation required.

## Teammate Lifecycle

1. Receive delegation via `SendMessage` from the team lead with the spec path and sprint scope
2. Invoke the `jira-workflow` skill, then read `spec.md` and `design.md`, then what the team
   has learned: `python3 scripts/lessons.py show --role <your role>`
3. Self-claim an unclaimed issue for your role (status `To Do` + `role-<yours>` label + no
   `agent-*` label). **The claim is the `mkdir` lock, not the label** — `jira-workflow` has
   the full protocol, but these steps hold even with no skill loaded, and all of them
   happen *before* you edit any file:
   1. Take the lock. **Bootstrap the parent first** — a bare `mkdir` of the issue
      directory fails with `ENOENT` on a fresh `$HOME`, which every copy of this recipe
      reads as "you lost", so the whole pool loses and the board starves. Record `owner`
      and `heartbeat` inside it: the lead's stale-claim sweep matches the `heartbeat`
      **file** (`find … -name heartbeat`), so a lock without one is invisible to the only
      thing that recovers your issue if you die mid-work.

      ```bash
      STATE="$(git rev-parse --path-format=absolute --git-common-dir)/claude-harness"
      CLAIMS="$STATE/claims/<projectKey>"; mkdir -p "$CLAIMS"
      if mkdir "$CLAIMS/<ISSUE-KEY>" 2>/dev/null; then
        echo "<your-instance>" > "$CLAIMS/<ISSUE-KEY>/owner"
        date -u +%Y-%m-%dT%H:%M:%SZ > "$CLAIMS/<ISSUE-KEY>/heartbeat"
      else
        echo "LOST -- owned by $(cat "$CLAIMS/<ISSUE-KEY>/owner" 2>/dev/null)"
      fi
      ```

      Only the **second** `mkdir` failing means you lost: pick another issue, touch
      nothing. Never write `owner`/`heartbeat` unconditionally — on a lost race that
      overwrites the winner's own record.
   2. `getJiraIssue(..., fields=["labels","status","summary","description","issuelinks"])`
      — an explicit `fields` list **replaces** the defaults, so it must include `labels`.
   3. `editJiraIssue` adding your `agent-*` label, then `transitionJiraIssue` to
      **`In Progress`**, then comment `Claimed by <instance>.`
   4. Re-read and confirm — **fail open**: an absent label with no competing `agent-*` is
      an unconfirmed write, not a loss. You hold the lock; re-apply and continue.
4. Implement exactly what the issue describes, touching only its `Files:`
5. Verify with `python3 scripts/verify_issue.py <ISSUE-KEY>` — it
   runs the issue's `Run:` command and records the evidence the gate needs — then comment
   the result
6. Transition to `In Review` and stop there. Closing (`In Review` -> `Done`) belongs to the
   review synthesizer, and the gate blocks any other worker that tries it
7. Release the lock (`rm -rf "$STATE"/claims/<projectKey>/<ISSUE-KEY>`), notify the
   lead via `SendMessage`, and claim the next unclaimed issue for your role

## Communication Rules

- **Direct to teammates**: Interface clarifications, dependency questions, sharing outputs they need
- **To the lead**: Blockers needing decisions, completion reports, scope/spec questions
- Tools: `SendMessage` (any teammate); the Atlassian MCP for all issue state

## Completion Reporting

Three steps, in order:
1. Comment the verification output and a one-line summary on the issue
2. Transition the issue to `In Review`
3. `SendMessage` the lead (and any teammate depending on your output)

The board is the record. There is no file to update.

## Shared lessons

Mistakes are recorded once and read by everyone after, via `scripts/lessons.py`
(`.claude/lessons/README.md` has the format). Three moments, no more:

- **Reviewer:** one lesson per Important finding — the general mistake, not the issue's details.
- **Worker:** when a block, a failed verify or a fix round surprised you.
- **Lead, at Epic close:** `digest` and `report`; promote a lesson seen three times into the
  agent file or rule it belongs in, retire wrong ones.

`add --roles <roles> --tags <tag> --source <ISSUE-KEY> --when "<situation>" --do "<instead>"`;
a repeat strengthens the existing lesson. Lessons are advice: a rule, spec or issue wins.

## Blocker Reporting

Set the impediment flag (`config.fields.flagged` = `Impediment`), comment the specific
blocker and what would unblock it, then `SendMessage` the lead. If the same blocker
persists after two attempts, the lead escalates to the user.

## Verification Gate (All Teammates)

Before transitioning ANY issue to `In Review`:
1. Confirm the interface/output contract matches the issue exactly
2. Confirm you only modified files listed in the issue's `Files:`
3. Run `python3 scripts/verify_issue.py <ISSUE-KEY>`. It executes the
   issue's `Run:` command and, only if that exits 0, records evidence bound to the current
   content of the issue's files (machine-enforced; see hook 2 below)
4. Transition. If you edit one of the issue's files after verifying, verify again — the
   evidence is stale and the gate will say so

If verification fails and you cannot fix it in scope, flag the issue as an impediment
with the specific failure.

## Enforced Hooks (Automated Guardrails)

Hooks in `.claude/settings.json` enforce this protocol; scripts live at `.claude/hooks/`.
`spec_gate.py` is documented in `spec-and-jira-required.md`. Every decision is logged to
`~/.claude/logs/team-hooks.jsonl`. Observational hooks **fail open** (a hook error never
blocks you); the verification gate and the AWS guard **fail closed** once they know the
action is one they police, because there a silent pass is the worse failure.

**What the hooks are for.** They catch mistakes: a transition without verification, an edit
to a file someone else holds, an AWS change aimed at the wrong account. They are not what
stops a determined agent, and are not built to be. That is the job of four boundaries no
agent can write: the Bash sandbox, branch protection and CI on the pull request, Jira's
own permissions, and AWS credentials scoped to the sandbox account. The last three are the
operator's to set up and may not be in place yet; where CI is not yet a required check,
your evidence is the only record there is, which is the reason it has to be true. Treat a
hook as a colleague's reminder, and never as something to get past.

Hooks know who is calling: a tool call from a spawned worker carries the instance name the
lead assigned (`coding-2`, `review-1`), and a call from the main session carries none, which
identifies the lead. Your role is read from your name; nothing you type into a tool argument
changes it.

Coordination state — the Jira mirror, claim locks, verification evidence, run locks — lives
in the repository's own git directory, shared by every worktree of the repository and by no
other checkout:

```bash
STATE="$(git rev-parse --path-format=absolute --git-common-dir)/claude-harness"
```

### 1. Issue format check (`PreToolUse` on `createJiraIssue` and `editJiraIssue`)
Issue creation is **blocked** unless the summary has a `[frontend|backend|coding|devops|sa|review]`
tag, the description has `Spec:` / `Files:` / `Acceptance:` / `Run:`, and `role-*` +
`spec-*` labels are present and agree with the tag. `Files:` entries are repo-relative
paths, directories or globs; absolute and `..` paths are rejected, and an entry that
overlaps another open issue in the same spec and group is rejected. A `Run:` containing a
backslash must be wrapped in backticks. An **edit** is blocked if a worker tries to rewrite
a description or add a bypass label — `Files:` and `Run:` are the lead's to author.
- **Bypass**: the `skip-format-check` label, honoured **only when the lead applies it**.
  A real Epic (no role tag, no role label) is exempt.

### 2. Verification gate (`PreToolUse` on `transitionJiraIssue`) — ACTION REQUIRED
A transition to `In Review` or `Done` is **blocked** unless all of these hold:

- the mirror knows the issue (read it with `getJiraIssue` first if the gate says it does not);
- an evidence record exists, written by `scripts/verify_issue.py`, whose command is the
  issue's `Run:`, whose exit code is 0, and whose file hash matches the issue's files as
  they are now. An empty file, a hand-written note, a record for another command and a
  stale record are each refused;
- every project check that applies (`agentChecks` in `.claude/gates.json`: lint on every
  run; the full suite, marked `"when": "review"`, only on `--review`) is recorded as passing.
  `scripts/verify_issue.py` runs them after the issue's `Run:`, in your sandboxed shell, so
  put the tests your change needs in the issue's `Run:`. **The hook runs no project tool itself** — it is
  outside the sandbox — so this is your evidence, and CI on the pull request re-runs it;
- for `Done`: the evidence is review evidence (`scripts/verify_issue.py <ISSUE-KEY> --review`) and
  the caller is a reviewer or the lead. Any other worker moving an issue to `Done` is blocked.

One record permits one transition: it is consumed on success and restored automatically if
the transition fails, so the reviewer produces its own before closing.
- **Bypass**: the `skip-verify` label, honoured **only when the lead applied it**.

### 3. Mirror journal (`PostToolUse` on the Jira tools)
Records every successful Jira write to `$STATE/jira-mirror/<projectKey>.jsonl`, and syncs
every issue it sees in a **read** (`getJiraIssue`, a JQL search) — so the mirror heals after
a hand edit on the board, and one search of the project rebuilds it. A failed call is never
recorded as a success. Observational only; it exists because a hook subprocess holds no
Jira credentials. **Only this hook writes the mirror.** Your shell and your file tools are
both denied it; if it is wrong, read the issue again, and if that does not mend it tell the
lead.

### 4. Idle work-check (`TeammateIdle`)
Before you go idle, the hook checks mirror state for unclaimed issues carrying your
role label. If any exist you are kept working and nudged to claim one or confirm to the
lead you are done. After 2 nudges for the same set it lets you idle (loop-safe).

The nudge for `role-review` deliberately omits the claim recipe: reviewers are
partitioned by the lead's handoff, and the sprint's single `role-review` card belongs to
the synthesizer.

### 5. Claim gate (`PreToolUse` on `Write` / `Edit`) — ACTION REQUIRED
A write is **blocked** if an issue's `Files:` covers that path (exactly, as a directory, or
by glob) and you do not hold its claim:

- no lock, or `In Progress` with no lock: blocked once per issue, because that judgement
  rests on the mirror, which can lag. Take the `mkdir` lock and transition before your
  first edit and you will never meet it;
- **locked by someone else: blocked every time.** The lock records its owner and the hook
  knows your name. Pick another issue; only the lead releases a claim;
- your own lock: allowed, and your heartbeat is refreshed by the write.

Files in a git worktree of this repository are gated at their repo-relative paths. Harness
files under `.claude/`, the spec directory and dependency directories never count.
- **Bypass**: `CLAUDE_CLAIM_GATE=off`, set by the operator when launching the session.

### 6. Evidence finalizer (`PostToolUse` and `PostToolUseFailure` on `transitionJiraIssue`)
Phase two of the verification gate. The gate moves your evidence to `.inflight` *before*
the call (so a concurrent second transition is blocked); this hook deletes it on success or
**restores it** if the call did not affirmatively succeed, including a rejection Jira
returns inside the response. Observational; it never blocks.

### 7. AWS guard (`PreToolUse` on `Bash` and AWS MCP tools)
Reads pass. A call that changes AWS is allowed only from the lead or a devops worker, with
its region stated explicitly and inside the declared sandbox, using credentials the hook
itself resolves to the sandbox account. Anything else is blocked and names an approval
marker for a human to create. See `AWS-security-guidelines.md` → "Production Safeguards".

### 8. Untrusted-content flag (`PostToolUse` on web and MCP reads)
Adds a line of context when a result contains text shaped like instructions. It never
blocks. See `untrusted-content.md`.

### 9. Session self-test (`SessionStart`)
Reports, at session start, anything unhealthy about the harness itself. Never blocks;
`python3 scripts/doctor.py` is the full preflight.

### 10. Guardrail guard (`PreToolUse` on `Write` / `Edit`)
The harness's own controls — `.claude/hooks/`, `.claude/settings*.json`, the agent
definitions, rules and skills (`.claude/agents/`, `.claude/rules/`, `.claude/skills/`), the
gate, capability and Jira configuration, CI workflows, `scripts/harness_admin.py`, and git's
own `.git/config` and `.git/hooks/` — cannot be
edited in an ordinary session, by anyone. If the harness itself is what you were asked to
change, stop and tell the operator: that needs a session they launched with
`CLAUDE_HARNESS_MAINTENANCE=1`. Operator records under `~/.claude/harness/` are never
writable by an agent. Do not look for another way to make the change.

### 11. Jira execute guard (`PreToolUse` on the Atlassian generic runners)
`createJiraIssue`, `editJiraIssue` and `transitionJiraIssue` must be called through their
own tools. Sent through `executeWrite` or `executeDestructive` they would skip the gates,
so they are blocked there; bulk and destructive issue operations are the lead's.

### 12. Spawn guard (`PreToolUse` on `Agent`)
Only the lead spawns an agent whose name or type carries a role (`frontend-*`, `backend-*`,
`coding-*`, `devops-*`, `review-*`, `sa-*`, or a role agent type). A worker may spawn neutral
helpers (an explorer, a simplifier) named after no role; they get no privileges.

## Handling Ambiguity

- **Missing details**: Check `spec.md` and `design.md` first. If not there, `SendMessage` to lead or relevant teammate
- **Multiple valid approaches**: Pick the simplest that satisfies acceptance criteria
- **Out-of-scope issues**: Note in completion report; don't fix
- **Conflicting requirements**: Flag the issue as an impediment (see Blocker Reporting), never silently pick one interpretation
- **Dependency on another teammate**: `SendMessage` to them directly, then flag the issue as an impediment if not ready

## Coordination Under Unreliable Signals (Learned — All Teammates)

Message delivery, idle pings, and the Jira/Atlassian MCP are all **laggy and occasionally lossy** in practice. Whole sessions have been derailed by treating these transient signals as ground truth. These rules are load-bearing:

- **Ground truth is Jira, then the disk.** The authoritative state of the work is the
  issue's status and labels in Jira, then verification evidence, then the actual
  `git diff` — NOT the last message you received. Message delivery lags and reorders.
  Before acting on ANY status claim (yours or a peer's), confirm it against Jira. If
  Jira is unreachable, stall and escalate; do not invent state.
- **Delivery lag ≠ death.** Messages routinely arrive delayed, batched, and out of order. "No message in N minutes" or "no OS process visible" is **not** evidence a teammate is stalled or dead — it is evidence the channel is quiet. Teammates running long verification passes (a multi-minute plugin review, an uncached test suite, a `terraform plan`) look identical to a dead one over the wire. Do not conclude a peer has failed from silence alone.
- **Ignore stale replays silently.** If you receive a stale assignment (or re-assignment) message for an issue that is already `Done`, or for which verification evidence is already present on disk (proof that verification ran, even before the transition lands and consumes it), or for a key that `getJiraIssue` reports as "not found", treat it as a stale roll-forward artifact: take no action and do **not** re-run verification or re-report. Reply at most once if a peer needs confirmation. Re-verifying completed work on every replay is a documented time sink that starves the pool.
- **Claim atomically, one owner per issue — via the lock, not the label.** `mkdir "$STATE"/claims/<projectKey>/<ISSUE-KEY>` is a POSIX atomic test-and-set and is the *only* step that decides ownership — take it with the full snippet in *Teammate Lifecycle* step 3.1, never bare: without the `mkdir -p` of the parent it `ENOENT`s on a fresh `$HOME`, and without `owner`/`heartbeat` the lock is invisible to the lead's sweep. It cannot be done with a `getJiraIssue` read: `editJiraIssue` replaces the labels array, so a loser's write erases the winner's label and exactly one survives — which means "if two `agent-*` labels are present" is a condition the API can never produce, and a rule built on it never fires. Transitions cannot arbitrate either; they are global, so both racers' move to `In Progress` succeeds. Once you hold the lock, the label and the transition are bookkeeping: apply them, and if a re-read does not show your label, re-apply it rather than surrendering the issue. Never edit a file outside your claimed issue's declared paths — peers run concurrently and will clobber.
- **Globally-unique instance names.** With multiple specs/teams possibly active, a bare role name (e.g. `review-2`) can misroute to a same-named instance on a different spec. Use the names the lead assigned and address peers by their exact instance name.

## Shutdown (Implicit Team — Auto-Cleanup)

There is **one implicit team per session**; the standalone `TeamCreate`/`TeamDelete` tools no longer exist. Teammates are background `Agent` spawns addressable by `name`, and the team is **torn down automatically when the session ends** — there is no `TeamDelete` step and no member-drain gate to satisfy.

**Teammate side.** A teammate's process ends when its work is done and it goes idle, or when the lead sends a `{type: "shutdown_request"}` — a *legacy* mechanism still supported by `SendMessage`, used only when the lead wants to stop a still-running background teammate early. On receiving one, finish current work, ensure the issue's status and comments are current in Jira, then reply `{type: "shutdown_response", request_id: <echoed>, approve: true}`. Approving terminates your process — do it only once your work is durably recorded.

**Lead side.** Do **not** hand-roll a `TeamDelete` teardown — it no longer exists, and there is no `~/.claude/teams/<team>/config.json` member list to drain. When all work is complete: confirm every issue is `In Review`/`Done`, then either let idle background teammates terminate on their own, or — if any are still running and you want them stopped now — `SendMessage` each a `{type: "shutdown_request"}` and wait for its `approve: true`. Finally, sweep this run's residue (session auto-cleanup touches neither path):

```bash
STATE="$(git rev-parse --path-format=absolute --git-common-dir)/claude-harness"
rm -f  "$STATE"/verified/<projectKey>/*.verified   # spent attestations
rm -rf "$STATE"/claims/<projectKey>/               # released claim locks
```

**Only `*.verified`, never the whole directory.** That directory also holds `.inflight`
slots — evidence records consumed by a transition that is still in the air. Deleting one mid-flight
destroys an attestation a live teammate earned: the finalizer then finds nothing to restore
and the retry is blocked with "no evidence record", which reads as a verification failure rather
than the teardown that actually caused it. Run this only once every teammate has stopped.
