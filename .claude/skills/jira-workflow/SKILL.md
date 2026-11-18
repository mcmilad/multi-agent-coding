---
name: jira-workflow
description: Claim, work, and close Jira issues as an agent-team teammate — claim protocol, issue shape, comment templates, and verification evidence. Load before claiming any work when the team is Jira-backed.
---

# Jira Workflow

Jira is the system of record for the agent team's backlog. There is no `tasks.md`.
All per-issue work goes through the Atlassian MCP.

Read `.claude/jira-config.json` first — it holds this site's `cloudId`, `projectKey`,
custom field IDs, status IDs, and transition IDs. **Never hardcode an ID**; they differ
per site.

## Issue Shape

Every Task issue in the agent project must carry:

```
Summary:     [frontend|backend|coding|devops|sa|review] <verb> <what>
Parent:      the spec Epic
Sprint:      the group's sprint (config fields.sprint)
Labels:      spec-<slug>, role-<role>, group-<n>
Description:
  Spec:       .claude/specs/<slug>/spec.md#<section>
  Files:      comma-separated paths this issue may write
  Acceptance: what must be true when it is done
  Run:        `the verification command`
```

This is machine-enforced. `createJiraIssue` is **blocked** if the summary has no role tag,
the description is missing any of `Spec:`, `Files:`, `Acceptance:`, `Run:`, or the
`role-*` / `spec-*` labels are absent. It is also blocked if the summary tag and the
`role-*` label disagree.

`Files:` entries may be exact paths, directories or globs, always relative to the
repository root. Wrap `Run:` in backticks — Jira stores descriptions as markdown and drops
backslashes from bare text, which silently changes the command.

Bypass with the `skip-format-check` label for a coordination or research issue that
genuinely has no verification; only the lead can apply it. Epics are exempt. Only the lead
may edit an issue's description after it is created.

## Claim Protocol

Up to twelve agents claim concurrently. **Jira cannot arbitrate that race**, so the
filesystem does.

Two verified facts force this design, and both contradict the obvious approach:

- **`editJiraIssue` has no compare-and-swap.** Its schema is
  `{cloudId, issueIdOrKey, fields, contentFormat, responseContentFormat}` — no version, no
  ETag, no `update` verb. Writing labels is read-modify-write, so a loser's write silently
  **erases** the winner's label and exactly one survives. Any rule of the form "if two
  `agent-*` labels are present, ..." is therefore unreachable and must not be written.
- **Transitions are global** (`isGlobal: true` on every id). Moving an issue to
  `In Progress` never fails, even from `In Progress`. **A transition cannot be a claim** —
  both racers succeed.

So: ownership is decided by an atomic `mkdir` on the shared filesystem. The Jira label and
the `In Progress` transition are the **board-visible mirror** of a decision already made,
never the thing being contended.

**1. Find** — unclaimed work is a *status*, not a label. JQL cannot wildcard labels, so
never try to express "has no `agent-*` label" in JQL:

```
project = <projectKey> AND sprint in openSprints()
  AND status = "To Do" AND labels = role-coding
  ORDER BY Rank ASC
```

**2. Lock** — `mkdir` is a POSIX atomic test-and-set: exactly one caller creates the
directory, every other gets an error. This is the actual claim.

```bash
STATE="$(git rev-parse --path-format=absolute --git-common-dir)/claude-harness"
CLAIMS="$STATE/claims/<projectKey>"
mkdir -p "$CLAIMS"
if mkdir "$CLAIMS/<ISSUE-KEY>" 2>/dev/null; then
  echo "<your-instance-name>" > "$CLAIMS/<ISSUE-KEY>/owner"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$CLAIMS/<ISSUE-KEY>/heartbeat"
  echo "CLAIMED"
else
  echo "LOST -- owned by $(cat "$CLAIMS/<ISSUE-KEY>/owner" 2>/dev/null || echo unknown)"
fi
```

`LOST` is **deterministic, not inferred**. Return to step 1 and pick another issue; do not
touch the board and do not touch the files.

**3. Read the issue — with an explicit, complete field list.**

```
getJiraIssue(issueIdOrKey,
             fields=["labels","status","summary","description","issuelinks"])
```

> **`fields` REPLACES the default set.** `fields=["issuelinks"]` returns issuelinks and
> **no labels** — and step 4 then writes labels back, which would erase `role-*`,
> `spec-*` and `group-*` from the issue permanently. Any explicit list **must** include
> `labels`.

Check `issuelinks` before proceeding. Skip the issue (releasing the lock with
`rm -rf`, **not** `rmdir` — step 2 put `owner` and `heartbeat` inside it, so `rmdir`
fails and silently orphans the issue) if it has an inward `is blocked by` whose blocker
is still `To Do` or
`In Progress`. A blocker at **`In Review` counts as satisfied** — its `Files:` are on disk
by then, and nothing reaches `Done` until the group closes, so waiting for `Done` would
deadlock every intra-group dependency. If the `issuelinks` key is **absent** from the
response, treat it as UNKNOWN and escalate to the lead — never as "no blockers".

**4. Mirror the claim onto the board** — label first, then transition, both *before* you
edit any file. This is what tells the lead the issue is taken.

```
editJiraIssue(fields = {labels: <labels from step 3> + ["agent-coding-2"]})
transitionJiraIssue(transitionId = <To Do -> In Progress>)
addOrEditJiraIssueComment(commentBody = "Claimed by coding-2.")
```

Resolve the transition id from `config.transitions` (or `listJiraIssueTransitions`) and
pass it as `transitionId`. A `transitionName` the gate cannot resolve to a status is
blocked, so use the id.

**5. Confirm — fail open, not closed.** Re-read with a **new `getJiraIssue`** (never read
labels off the `editJiraIssue` response: that is a write-time snapshot and cannot show a
later overwrite). Then:

| What you see | What it means | What to do |
|---|---|---|
| My `agent-*` label present | Claim confirmed | Work it |
| My label absent, **another** `agent-*` present | A peer's write erased mine | You still hold the lock, so this is a stale peer. Comment the conflict, re-apply your label, and continue |
| My label absent, **no** `agent-*` at all | Unconfirmed write, not a loss | Re-apply the label once, then re-read. Still absent → escalate |

The lock in step 2 already decided ownership, so an absent label is a **bookkeeping**
failure, not an ownership one. Never abandon an issue you hold the lock on just because a
read came back stale — message delivery and the MCP are both laggy, and treating that as a
loss manufactures orphans.

**6. Work** — only the files listed in `Files:`. Peers run concurrently; editing outside
your declared paths clobbers them. Refresh the heartbeat at each verification step:

```bash
STATE="$(git rev-parse --path-format=absolute --git-common-dir)/claude-harness"
date -u +%Y-%m-%dT%H:%M:%SZ > $STATE/claims/<projectKey>/<ISSUE-KEY>/heartbeat
```

**7. Release** — after the issue reaches `In Review`, drop the lock so the key is reusable:

```bash
STATE="$(git rev-parse --path-format=absolute --git-common-dir)/claude-harness"
rm -rf "$STATE"/claims/<projectKey>/<ISSUE-KEY>
```

### Ground truth for claims is the lock; for state it is Jira

A deliberate split. Ownership is a mutual-exclusion question the filesystem can answer and
Jira cannot. Status, labels, comments and the verdict remain Jira's. The lock assumes a
shared `$HOME` — it does not span hosts, and under `isolation: worktree` it only works if
`$HOME` is shared.

### Recovering an abandoned claim

An issue stuck in `In Progress` is invisible to the Find JQL *and* to the idle work-check,
so nothing reclaims it automatically. Recovery is the lead's, and it is
**investigate-first**:

1. The lead's sweep surfaces candidates — `status = "In Progress"` with a `heartbeat`
   older than **60 minutes** (well clear of the ~29-minute legitimate verification pass
   that has caused a bad takeover before). **A stale heartbeat is a reason to look, never
   evidence of death.**
2. The lead then runs the full liveness protocol in `fullstack-agent.md` — direct
   `SendMessage` with a bounded reply window, check the disk for partial output and
   evidence records — before doing anything.
3. Only on positive evidence of death, the lead performs an explicit **logged release**:

```bash
STATE="$(git rev-parse --path-format=absolute --git-common-dir)/claude-harness"
rm -rf "$STATE"/claims/<projectKey>/<ISSUE-KEY>       # free the lock
```
```
editJiraIssue(fields.labels = <labels minus the dead agent-* label>)
transitionJiraIssue(transitionId = <back to To Do>)
addOrEditJiraIssueComment("Released from <instance>: <the positive evidence>. Reclaimable.")
```

Recovery is **respawn-and-reclaim**, never lead takeover. Without the release the issue is
unreachable: a fresh instance cannot see it (wrong status) and a same-named respawn would
pass every ownership check trivially, so two live workers would edit the same files with
nothing detecting it.

## Verification Evidence

Before transitioning to `In Review`, verify the issue with the harness's own script:

```bash
python3 scripts/verify_issue.py <ISSUE-KEY>
```

It reads the issue's `Run:` command and `Files:` from the local mirror, executes the
command, and — only if it exits 0 — records evidence bound to the current content of the
issue's files. The transition is **blocked** without it, and the gate refuses anything
else: an empty file, a note you wrote yourself, a record for a different command, a failed
run, or a record made before you last edited one of the issue's files. Edit, then verify,
then transition, in that order.

After the `Run:` command the script runs the project's own checks (`agentChecks` in
`.claude/gates.json` — lint on every run, the full suite only with `--review`) and records
each result; the gate requires
every one to be recorded as passing. The hook runs none of them itself: it is outside the
sandbox and executes no project tool. The record is yours, and CI on the pull request
re-runs the same checks where no agent can write the result.

One record permits **one** transition. It is consumed on success and put back if the
transition fails, so the reviewer closing the issue later produces its own (see "Closing").

If the script says the issue is unknown to the mirror, read it with `getJiraIssue`
(default view) and run the script again — the read is recorded automatically.

Bypass: the `skip-verify` label, for issues with no runnable verification. It is honoured
**only when the lead applied it**; applying it yourself is blocked.

## Comment Protocol

Four comments per issue. Enough that the card tells the whole story; not so many that
the signal drowns.

**On claim:**
```
Claimed by coding-2.
```

**On verification:**
```
Verification: `pytest tests/auth` PASSED
14 passed in 2.1s
```

**On completion:**
```
Done. Added POST /login with token issuance via AuthToken.
Files: src/auth/login.py, tests/auth/test_login.py
```

**On blocker** — see below.

Review findings are comments on the issue they concern. The synthesizer's PASS/FAIL
verdict is a comment on the sprint's `role-review` issue.

## Blocked

Do not invent a Blocked status — the project may not have one. Use Jira's native
impediment flag, whose field id is `config.fields.flagged`:

```
editJiraIssue(fields = {<flagged field>: [{"value": "Impediment"}]})
addOrEditJiraIssueComment("BLOCKED: <specific blocker>. Needs: <what would unblock>.")
```

Then `SendMessage` the lead. Clear the flag by setting the field to `null` when unblocked.

## Closing

An implementer must **not** close its own issue: transition to `In Review` and stop
there. Only the review synthesizer moves `In Review` -> `Done`, and only on a PASS
verdict. **This is enforced**: the gate knows who is calling, and a `Done` transition from
a coding, devops or architect worker is blocked whatever evidence it holds. (The lead is
allowed, because a session with no agent identity cannot be told from a solo run — which
is why the lead must never post a verdict or close on its own judgement.)

**The closer produces its own evidence.** `Done` needs *review* evidence, and the
implementer's record was consumed when the issue reached `In Review`:

```bash
python3 scripts/verify_issue.py <ISSUE-KEY> --review
```

This re-runs the issue's `Run:` command independently, as the reviewer, against the files
as they are at the verdict. Run it after the PASS verdict is posted and before the
transition. Without it the close is blocked.

A `Done` transition with no synthesizer verdict comment is a review-gate violation; report
it to the lead the moment you see one.

## Label Vocabulary

| Label | Meaning | Hook-enforced? |
|---|---|---|
| `spec-<slug>` | Which spec this belongs to | Yes |
| `role-frontend` \| `role-backend` \| `role-coding` \| `role-devops` \| `role-sa` \| `role-review` | Which pool may claim it | Yes |
| `agent-<instance>` | Who claimed it — drives board swimlanes. The claim itself is the lock | Mirror of the lock |
| `group-<n>` | Which parallel group; survives sprint closure | No (convention only) |
| `skip-format-check` | Exempt from the create-time shape check | Yes — lead only |
| `skip-verify` | Exempt from the verification gate | Yes — lead only |

## When Jira Is Unreachable

Stall and escalate to the lead. Do not invent state, do not work an issue you could not
claim, and do not mark anything complete out of band. There is no `tasks.md` fallback by
design — a half-tracked run is worse than a paused one.
