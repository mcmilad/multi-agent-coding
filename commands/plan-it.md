# Plan It — Requirements to a Ready Backlog

Owns the planning half of the build loop: requirements, spec, design, decomposition, and a
complete Jira backlog. Ends the moment the board is ready for executors. It does not spawn
teammates and does not write production code.

## Input

Project idea, or an existing spec slug to resume planning: $ARGUMENTS

## Execution Position

Run **inline, as the top-level agent**. Do NOT dispatch `Agent(fullstack-agent)` — a
subagent cannot spawn teammates, so delegating the lead role here would strand `/ship-it`
later. You adopt the lead role yourself.

## Before Anything Else

1. Read `.claude/agents/fullstack-agent.md` — you are acting as that agent for this command.
2. Invoke the `spec-workflow` and `jira-workflow` skills via the `Skill` tool.
3. Read `.claude/jira-config.json` for `cloudId`, `projectKey`, and field ids. Never
   hardcode an id.

## Process

1. **Requirements.** Ask clarifying questions one at a time, adapted to the idea — not a
   fixed list. Cover users, core vs. nice-to-have, scale, integrations, non-functional
   targets, deployment environment, edge cases, and MVP boundary. Skip what the idea has
   already answered. Write `.claude/specs/<slug>/requirements.md`.

   This is not `/brainstorm`. That command stays the standalone ideation entry point for
   ideas not headed to a build; do not invoke it.

2. **Research and design.** Write `spec.md`, `design.md`, and `decisions.md` under
   `.claude/specs/<slug>/`, using `docs/specs/templates/` as starting points.
   `design.md` MUST keep its Security Considerations section.

3. **Decompose.** Produce groups that satisfy the decomposition contract:
   - No two **simultaneously-ready** issues declare the same path in `Files:`. Two issues
     are simultaneously ready unless one transitively blocks the other. This is the
     blocks-graph restatement of the older same-sprint rule — under one sprint per spec
     the old phrasing protects nothing.
   - Every `Files:` is non-empty and repo-relative.
   - Every cross-group dependency is a `blocks` link, never prose.
   - Exactly one `[review]` issue per group.

   Maximise the number of issues that can be ready at once; open a new group only for a
   real data or interface dependency. Where the work has a natural partition, assign
   `lane-<name>` labels so `--lane` has something to match; where it does not, assign none.

4. **Get approval for the decomposition before touching Jira.** Issues are expensive to
   unwind: the format hook blocks malformed ones and there is no bulk edit.

5. **Open one sprint** for the whole spec, via the Sprint lifecycle handshake in
   `fullstack-agent.md`. If `JIRA_API_TOKEN` is absent from your environment — the normal
   case on the recommended path — send the operator the exact command and wait for the
   sprint id. Record the Epic key and sprint id in `.claude/specs/<slug>/jira-run.json`.

6. **Author the backlog.** Create the Epic once, then **every issue in every group**, then
   the `blocks` links. Each issue carries the `[frontend|backend|coding|devops|sa|review]` summary tag,
   `Spec:` / `Files:` / `Acceptance:` / `Run:` sections, and `role-*` + `spec-*` +
   `group-*` labels. The whole backlog goes in now, not group by group: with N executors
   there is no single writer available later to author group 2.

7. **Audit.** Re-read the authored issues with JQL — never your own record of what you
   meant to create — and pipe them to the audit:

   ```bash
   python3 scripts/board_triage.py --audit <<'JSON'
   [{"key":"AGENT-1","role":"coding","group":1,"files":["src/a.py"],"blocked_by":[],"labels":["role-coding","spec-x","group-1"]},{"key":"AGENT-2","role":"review","group":1,"files":[],"blocked_by":[],"labels":["role-review","spec-x","group-1"]}]
   JSON
   ```

   Exit 3 means a contract violation: fix it here. An executor cannot.

8. **Print the Handoff Card** as the last thing you output:

   ```
   HANDOFF — <slug>
     Spec      .claude/specs/<slug>/
     Epic      AGENT-nn        Sprint  <id>
     Issues    12 total | group-1 5, group-2 5, group-3 2
     Roles     coding 8, devops 2, review 2
     Lanes     none
     Executors 2 suggested (widest simultaneously-ready set is 5 coding issues)

     Launch each executor in its own terminal, then run /ship-it in it:
       claude --name <slug>-1
       claude --name <slug>-2
     Then in each:  /ship-it <slug>
   ```

   The `--name` flags are not decoration. An agent cannot rename its own session, so this
   is the only point at which executor sessions become addressable to each other.

## Constraints

- Do not spawn teammates and do not write production code. Both belong to `/ship-it`.
- Do not run two `/plan-it` sessions on one slug — they would be two writers on one
  backlog, and nothing detects it.
