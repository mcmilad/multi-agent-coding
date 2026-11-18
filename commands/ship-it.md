# Ship It — Execute the Board

Owns the build half of the loop: read the board, grow a teammate pool, run build and
review to completion. Safe to run from several Claude Code sessions at once against one
board.

## Input

Spec slug, plus optional flags: $ARGUMENTS

- `<slug>` — required. The spec to execute.
- `--lane <name>` — restrict this session to issues labelled `lane-<name>`.
- `--pool coding=N,devops=N,review=N` — pin an exact pool and disable growth.

## Execution Position

Run **inline, as the top-level agent**. Do NOT dispatch `Agent(fullstack-agent)` — a
subagent cannot spawn teammates and the whole command would fail at its first spawn.

## Before Anything Else

1. Read `.claude/jira-config.json` and `.claude/specs/<slug>/jira-run.json`. No
   `jira-run.json` or no Epic means planning has not happened: stop and tell the user to
   run `/plan-it <slug>` first. Do not author a backlog from here.
2. Read `.claude/agents/fullstack-agent.md` — you are acting as that agent.
3. Invoke the `spec-workflow` and `jira-workflow` skills via the `Skill` tool.

## Process

1. **Register and read the disk.**

   ```bash
   python3 scripts/board_triage.py --key <KEY> --spec <slug> --register
   ```

   Note the `TOKEN`. Record your session's display name; if it is a directory-derived
   default rather than one the operator set with `claude --name`, warn that peers cannot
   address you reliably and fall back to the token.

2. **Probe cross-session messaging.** Try `ToolSearch select:ListAgents`. Present means
   lead-to-lead relay is available. Absent means it is off — degrade to board-only and say
   once: *"cross-session messaging unavailable; coordinating through the board. Enable by
   clearing `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` and restarting."* Never treat its
   absence as an error.

3. **Read the board.** `project = <KEY> AND labels = spec-<slug> ORDER BY status`,
   requesting `labels`, `status`, `issuelinks`, `summary`, `description`. An explicit
   `fields` list replaces the defaults, so name every field you need.

4. **Compute readiness.** An issue is ready when all three hold:
   - status is `To Do`
   - it has no open `is blocked by` link (blockers all `Done`)
   - no claim lock exists for it on disk (from the `CLAIMS` section of step 1)

   With `--lane`, filter to `lane-<name>` first. A lane label never affects readiness for
   anyone else — it is a filter you opt into, not a barrier.

5. **Spawn the floor.** For each role with ready work, spawn `min(2, ready_for_role)`
   instances. This is the first tool call of the build phase — not a `Write`, not a
   `Bash`. If it errors because you are nested, you are a subagent: stop and report,
   do not fall back to building single-threaded.

   Names are token-prefixed: `coding-<token>-1`, `devops-<token>-1`, `review-<token>-1`.
   Bare `coding-1` from two sessions is one Jira label and one message address.

6. **Grow lazily.** At each poll, for each role, add exactly one instance when all three
   hold:
   - every instance of that role **in this session** is busy, and
   - unclaimed ready work for that role remains, and
   - this session's count for that role is below the cap — frontend 3, backend 3, coding 6, devops 2, review 4.

   Do not compute a share of the queue. Peer counts are read at different moments by
   different sessions, so three executors starting together would each divide by a
   different number and collectively over-provision. Lazy growth converges from any start
   ordering because the claim `mkdir` is atomic and you only grow while saturated.

7. **Brief the pool.** Each spawn prompt carries: the spec path, the instance identity,
   the required-skills preamble (`spec-workflow`, `jira-workflow`), the self-claim
   instruction, and these two rules:
   - *Your lead is your only route to another session. Teammates in other sessions are not
     addressable. Post anything a peer session needs as an issue comment.*
   - The commit mutex, below.

8. **Run the loop** — Phase 2 steps 9 through 13a of `fullstack-agent.md`, unchanged.
   Monitor with JQL, not memory. Do not run a teammate's `Run:` command for it. Do not
   post the review verdict yourself.

9. **Cross-session review.** Whichever session's reviewer claims a group's single
   `[review]` issue synthesizes that whole group, including work another session wrote —
   from the diff and the issue comments. An analyst that cannot address the synthesizer
   posts findings as comments on the issue. Messaging is at most a nudge that comments are
   waiting; it never carries the findings.

10. **Advance barriers by observation.** As issues reach `Done`, blocked issues become
    ready. Never open or close a sprint — that needs the admin credential, which you do
    not have and must not ask for.

11. **Finalize, if you are last out.** When `project = <KEY> AND labels = spec-<slug> AND
    status != Done` returns nothing, take the finalize lock:

    ```bash
    STATE="$(git rev-parse --path-format=absolute --git-common-dir)/claude-harness"
    LOCKS="$STATE/locks/<KEY>"; mkdir -p "$LOCKS"
    if mkdir "$LOCKS/finalize" 2>/dev/null; then
      echo "<token>" > "$LOCKS/finalize/owner"
      date -u +%Y-%m-%dT%H:%M:%SZ > "$LOCKS/finalize/heartbeat"
      echo "WON"
    else
      echo "LOST -- owned by $(cat "$LOCKS/finalize/owner" 2>/dev/null)"
    fi
    ```

    On WON: invoke the `documentation` skill and complete Phase 4, refreshing the heartbeat
    as you go, then Phase 5, then release the lock on success:

    ```bash
    rm -rf "$LOCKS/finalize"
    ```

    Every spec on this board shares one project key, so a `finalize` lock left behind by a
    completed run makes the *next* `/ship-it` read LOST and skip Phase 4 entirely, for up to
    an hour, with no error — release on success is what prevents that. On LOST: report and
    exit. A lock whose heartbeat exceeds 60 minutes may be taken over — that stale-takeover
    path is the recovery mechanism for a dead holder, not the normal way this lock is
    supposed to clear, and documentation is idempotent so a rare double-run is the far
    cheaper failure than a permanently stranded Phase 4.

12. **Tear down your own residue only.**

    ```bash
    STATE="$(git rev-parse --path-format=absolute --git-common-dir)/claude-harness"
    python3 scripts/board_triage.py --key <KEY> --release
    rm -f  "$STATE"/verified/<KEY>/<only issues your instances closed>.verified
    rm -rf "$STATE"/claims/<KEY>/<only issues your instances held>
    ```

    Never `rm -rf "$STATE"/claims/<KEY>/` and never glob the whole verified
    directory. `fullstack-agent.md` Phase 5 step 17 does exactly that, and it is correct
    only for a single session: run here with a peer live, it deletes locks a working
    teammate holds and destroys attestations it earned, and the peer's retry then fails
    with "no evidence record" — which reads as a verification failure rather than the teardown
    that caused it.

## The Commit Mutex

Disjoint file sets still contend on `.git/index.lock`. Every commit goes through:

```bash
STATE="$(git rev-parse --path-format=absolute --git-common-dir)/claude-harness"
LOCKS="$STATE/locks/<PROJECT>"; mkdir -p "$LOCKS"
HELD=0
for _ in $(seq 1 60); do
  if mkdir "$LOCKS/git-commit" 2>/dev/null; then
    HELD=1
    echo "<instance>" > "$LOCKS/git-commit/owner"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$LOCKS/git-commit/heartbeat"
    break
  fi
  sleep 2
done
trap '[ "$HELD" = 1 ] && rm -rf "$LOCKS/git-commit"' EXIT
git add -- <only this issue's Files:>
git commit -m "..."
```

Bounded retry, and the `trap` releases on any exit path **that this session actually holds
the lock on**. If the loop expires the commit proceeds anyway — fail-open, consistent with
every other guardrail here.

Two details are load-bearing, and an earlier draft got both wrong:

- **The `HELD` guard.** Registering the `trap` unconditionally means a session that *failed*
  to acquire still releases on exit — destroying a live peer's mutex. That is strictly worse
  than having no mutex: B gives up, commits anyway, frees A's lock mid-commit, and C then
  acquires and commits concurrently with A. The mutex would itself cause the
  `.git/index.lock` contention it exists to prevent — a session destroying a live peer's
  state, the same threat class as the global-teardown rule above.
- **`rm -rf`, not `rmdir`, and write `owner`/`heartbeat`.** The lock directory must carry the
  same contents `executor_registry.acquire_lock()` writes, and be released the same way. A
  bare `rmdir` cannot remove a directory holding those files: it exits 1, `2>/dev/null`
  swallows the error, and the lock is then held forever with nothing reporting why. One named
  lock cannot have two incompatible release semantics depending on which code path took it.

The mutex serialises the commit call only. It changes nothing about which files an agent
edits — that is still governed entirely by the decomposition contract.

## Precedence

Jira first, then the disk, then messages. A message from a peer lead is a prompt to check
the board, never authorisation to act. This is the multi-session restatement of the
ground-truth rule in `agent-team-protocol.md`.
