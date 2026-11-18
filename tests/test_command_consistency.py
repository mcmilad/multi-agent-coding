"""The two commands restate parts of fullstack-agent.md. Quoted values drift;
these tests make the drift fail loudly instead of silently misleading a lead."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel)) as fh:
        return fh.read()


PLAN = "commands/plan-it.md"
SHIP = "commands/ship-it.md"
LEAD = ".claude/agents/fullstack-agent.md"


def test_both_commands_exist():
    assert os.path.exists(os.path.join(ROOT, PLAN))
    assert os.path.exists(os.path.join(ROOT, SHIP))


def test_neither_command_dispatches_the_lead_as_a_subagent():
    for rel in (PLAN, SHIP):
        body = read(rel)
        assert "Agent(fullstack-agent)" in body, \
            "%s must name the anti-pattern explicitly" % rel
        assert re.search(r"do not dispatch", body, re.I), \
            "%s must forbid dispatching the lead as a subagent" % rel


def _team_composition_caps(lead):
    """Parse the Concurrency cap column of the Team Composition table, per role.

    Ties the test to the actual table row instead of a hardcoded tuple, so a
    changed cap in fullstack-agent.md is what the comparison is against — not
    a number baked into the test that happens to match today.

    Greedy `.*` anchored to end-of-line (`\\s*\\|\\s*$`, re.MULTILINE), not a
    non-greedy `.*?`. Concurrency cap is the LAST column in this table, so
    the match must bind to the last bold `**N**` cell on the row, not the
    first. A non-greedy match binds to whichever cell happens to be bolded
    first — wrong in general, and dangerous in one specific case: if an
    earlier cell's bold value happens to equal ship-it.md's (stale) quoted
    cap while the real, last-column cap has drifted, the non-greedy version
    returns the coincidental early value and the cross-file comparison
    passes silently on exactly the drift it exists to catch. See AGENT-107.
    """
    caps = {}
    for role in ("frontend", "backend", "coding", "devops", "review"):
        agent = role + "-agent"
        m = re.search(r"\|\s*`%s`.*\*\*(\d+)\*\*\s*\|\s*$" % re.escape(agent),
                      lead, re.MULTILINE)
        assert m, "no Concurrency cap row for `%s` in the Team Composition table" % agent
        caps[role] = int(m.group(1))
    return caps


def _ship_it_caps(ship):
    m = re.search(r"frontend\s+(\d+),\s*backend\s+(\d+),\s*coding\s+(\d+),\s*devops\s+(\d+),"
                  r"\s*review\s+(\d+)", ship)
    assert m, "ship-it.md's caps line (\"frontend N, backend N, coding N, ...\") not found"
    return dict(zip(("frontend", "backend", "coding", "devops", "review"),
                    (int(g) for g in m.groups())))


def test_pool_caps_quoted_in_ship_it_match_the_lead_agent():
    lead_caps = _team_composition_caps(read(LEAD))
    ship_caps = _ship_it_caps(read(SHIP))
    assert lead_caps == ship_caps, \
        "ship-it's quoted caps %r must match fullstack-agent.md's Team Composition table %r" % (
            ship_caps, lead_caps)


_CODING_ROW = (
    "| `coding-agent` | Work that is neither frontend nor backend — scripts, glue, tooling, "
    "the harness itself — handles `[coding]` tasks | `min(widest [coding] group, 6)` | **6** |"
)


def test_the_caps_parser_binds_to_the_last_bold_cell_not_the_first():
    # A plain hijack: an earlier cell on the row is bolded to a different
    # digit than the true (last-column) cap. The parser must still return
    # the true cap -- binding to the first bold run would misattribute this
    # to a "ship-it drifted" failure when the row's own earlier cell is what
    # changed.
    lead = read(LEAD)
    hijacked = lead.replace(_CODING_ROW,
        "| `coding-agent` | **3** | `min(widest [coding] group, 6)` | **6** |")
    assert hijacked != lead, "swap did not match -- test setup is wrong"
    assert _team_composition_caps(hijacked)["coding"] == 6


def test_the_caps_guard_catches_a_drift_masked_by_an_earlier_bold_digit():
    # The scenario the guard exists for, and the one the pre-fix non-greedy
    # regex missed: an earlier cell's bold value happens to equal ship-it's
    # (now stale) quoted cap, while the row's real, last-column cap has
    # drifted. Confirmed empirically before this fix: the old non-greedy
    # `.*?` regex returned the coincidental EARLIER value here (6, matching
    # ship-it's stale text) and the cross-file comparison passed silently
    # despite the real drift to 9. The fixed, greedy/end-anchored regex must
    # return the true last-column cap (9), so the comparison correctly
    # disagrees with ship-it's stale "coding 6" instead of passing by luck.
    lead = read(LEAD)
    drifted = lead.replace(_CODING_ROW,
        "| `coding-agent` | **6** | `min(widest [coding] group, 9)` | **9** |")
    assert drifted != lead, "swap did not match -- test setup is wrong"
    lead_caps = _team_composition_caps(drifted)
    ship_caps = _ship_it_caps(read(SHIP))   # still says "coding 6" -- stale
    assert lead_caps["coding"] == 9, \
        "must bind to the LAST bold cell (the true, drifted cap), not the coincidental earlier one"
    assert lead_caps != ship_caps, \
        "a real 6->9 drift must not compare equal just because an earlier cell also said 6"


def test_ship_it_forbids_the_global_teardown():
    ship = read(SHIP)
    assert 'Never `rm -rf "$STATE"/claims/<KEY>/`' in ship, \
        "the global teardown deletes live peers' locks; ship-it must forbid it"


def test_ship_it_does_not_open_or_close_sprints():
    ship = read(SHIP)
    assert "sprint-open" not in ship and "sprint-close" not in ship, \
        "an executor has no admin credential and must never run sprint lifecycle"


def test_plan_it_owns_the_single_sprint_open():
    plan = read(PLAN)
    assert re.search(r"open\w*\s+one\s+sprint", plan, re.I), \
        "plan-it must actually open the sprint, not merely mention the word 'sprint'"
    assert "Sprint lifecycle handshake" in plan, \
        "sprint opening must route through the documented handshake in fullstack-agent.md"
    assert not re.search(r"never\s+open\w*\s+(a|one)\s+sprint", plan, re.I), \
        "a sentence forbidding sprint use must not satisfy this test"


def test_ship_it_uses_the_correct_session_env_var():
    assert "CLAUDE_SESSION_ID" not in read(SHIP).replace("CLAUDE_CODE_SESSION_ID", "")


def test_lead_agent_documents_command_driven_entry():
    lead = read(LEAD)
    assert "Command-Driven Entry" in lead
    assert "/plan-it" in lead and "/ship-it" in lead


def test_lead_agent_names_the_precedence_between_the_two_models():
    lead = read(LEAD)
    assert re.search(r"`ship-it\.md`\s+governs", lead, re.I), \
        "a teammate reading both files needs to know which one wins"
    assert not re.search(r"`ship-it\.md`\s+never\s+governs", lead, re.I)
