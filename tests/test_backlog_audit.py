import importlib.util
import io
import os
import sys


SPEC = importlib.util.spec_from_file_location(
    "backlog_audit",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scripts", "backlog_audit.py"),
)
ba = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ba)


def issue(key, files, blocked_by=(), role="coding", group=1):
    return {"key": key, "role": role, "group": group,
            "files": list(files), "blocked_by": list(blocked_by),
            "labels": ["role-" + role, "spec-x", "group-%d" % group]}


def rules(violations):
    return sorted(v["rule"] for v in violations)


# --- Rule 1: simultaneously-ready file overlap ---

def test_two_ready_issues_sharing_a_file_violate_rule_1():
    v = ba.audit([issue("A-1", ["src/x.py"]), issue("A-2", ["src/x.py"])])
    assert 1 in rules(v)
    assert set(v[0]["keys"]) == {"A-1", "A-2"}


def test_ready_issues_with_disjoint_files_are_clean():
    # D-009: a spec needs at least one review seat, so a genuinely-clean
    # fixture must include one -- this is a fixture correction to match the
    # corrected contract, not a weakened assertion; `== []` is unchanged.
    assert ba.audit([
        issue("A-1", ["src/x.py"]), issue("A-2", ["src/y.py"]),
        issue("A-9", ["docs/r.md"], role="review"),
    ]) == []


def test_a_blocks_link_makes_a_shared_file_legal():
    v = ba.audit([
        issue("A-1", ["src/x.py"]),
        issue("A-2", ["src/x.py"], blocked_by=["A-1"], group=2),
    ])
    assert 1 not in rules(v)


def test_a_transitive_block_also_makes_a_shared_file_legal():
    v = ba.audit([
        issue("A-1", ["src/x.py"]),
        issue("A-2", ["src/z.py"], blocked_by=["A-1"], group=2),
        issue("A-3", ["src/x.py"], blocked_by=["A-2"], group=3),
    ])
    assert 1 not in rules(v), "A-3 can never be ready while A-1 is"


def test_siblings_behind_one_barrier_still_conflict_with_each_other():
    v = ba.audit([
        issue("A-1", ["src/a.py"]),
        issue("A-2", ["src/x.py"], blocked_by=["A-1"], group=2),
        issue("A-3", ["src/x.py"], blocked_by=["A-1"], group=2),
    ])
    assert 1 in rules(v), "A-2 and A-3 unblock together and both write src/x.py"


def test_overlap_is_reported_once_per_pair():
    v = [x for x in ba.audit([
        issue("A-1", ["src/x.py", "src/y.py"]),
        issue("A-2", ["src/x.py", "src/y.py"]),
    ]) if x["rule"] == 1]
    assert len(v) == 1


# --- Rule 2: Files must be present and repo-relative ---

def test_empty_files_violates_rule_2():
    assert 2 in rules(ba.audit([issue("A-1", [])]))


def test_a_review_issue_with_no_files_is_exempt_from_rule_2():
    v = ba.audit([issue("A-1", ["src/a.py"]), issue("A-9", [], role="review")])
    assert 2 not in rules(v), "a review issue writes nothing; rule 2 protects writers"


def test_absolute_path_violates_rule_2():
    assert 2 in rules(ba.audit([issue("A-1", ["/etc/passwd"])]))


def test_parent_traversal_violates_rule_2():
    assert 2 in rules(ba.audit([issue("A-1", ["../outside.py"])]))


# --- Rule 3: a later group needs an inbound barrier ---

def test_a_group_2_issue_with_no_blocker_violates_rule_3():
    v = ba.audit([issue("A-1", ["src/a.py"]), issue("A-2", ["src/b.py"], group=2)])
    assert 3 in rules(v)


def test_a_group_2_issue_with_a_blocker_is_clean_on_rule_3():
    v = ba.audit([
        issue("A-1", ["src/a.py"]),
        issue("A-2", ["src/b.py"], blocked_by=["A-1"], group=2),
    ])
    assert 3 not in rules(v)


# --- Rule 4: exactly one review issue per group ---

def test_two_review_issues_in_one_group_violate_rule_4():
    v = ba.audit([
        issue("A-1", ["src/a.py"]),
        issue("A-8", ["src/a.py"], role="review"),
        issue("A-9", ["src/b.py"], role="review"),
    ])
    assert 4 in rules(v)


def test_one_review_issue_per_group_is_clean_on_rule_4():
    v = ba.audit([issue("A-1", ["src/a.py"]), issue("A-8", ["docs/r.md"], role="review")])
    assert 4 not in rules(v)


# --- CLI ---

def test_main_exits_3_on_violation(monkeypatch, capsys):
    payload = '[{"key":"A-1","role":"coding","group":1,"files":["x.py"],' \
              '"blocked_by":[],"labels":[]},' \
              '{"key":"A-2","role":"coding","group":1,"files":["x.py"],' \
              '"blocked_by":[],"labels":[]}]'
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert ba.main([]) == 3
    assert "A-1" in capsys.readouterr().out


def test_main_exits_0_on_a_clean_backlog(monkeypatch):
    # D-009: a fixture with no review seat at all is no longer "clean" -- a
    # review issue is required so this remains a genuine positive example;
    # the assertion (`== 0`) is unchanged.
    payload = '[{"key":"A-1","role":"coding","group":1,"files":["x.py"],' \
              '"blocked_by":[],"labels":[]},' \
              '{"key":"A-9","role":"review","group":1,"files":[],' \
              '"blocked_by":[],"labels":[]}]'
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert ba.main([]) == 0


def test_main_exits_2_on_unparseable_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert ba.main([]) == 2


def test_a_dependency_cycle_does_not_hang(monkeypatch):
    v = ba.audit([
        issue("A-1", ["src/x.py"], blocked_by=["A-2"]),
        issue("A-2", ["src/x.py"], blocked_by=["A-1"]),
    ])
    assert isinstance(v, list)


# --- Rule 0: malformed entries must surface, never vanish silently ---

def test_a_non_dict_entry_is_a_rule_0_violation_not_a_silent_drop():
    v = ba.audit(["not an issue object", issue("A-1", ["src/a.py"])])
    assert 0 in rules(v)


def test_a_dict_with_no_key_is_a_rule_0_violation():
    v = ba.audit([{"role": "coding", "files": ["src/a.py"]}, issue("A-1", ["src/b.py"])])
    assert 0 in rules(v)


def test_a_backlog_with_only_malformed_entries_is_never_reported_clean(monkeypatch):
    payload = '[{"role":"coding"}, "garbage", null]'
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert ba.main([]) == 3, "dropped entries must not be reported as a clean audit"


def test_malformed_entries_do_not_break_the_audit_of_valid_siblings():
    # D-009: the valid siblings need a review seat between them to be
    # genuinely clean on every rule but 0; fixture updated accordingly.
    v = ba.audit([
        {"no": "key here"},
        issue("A-1", ["src/x.py"]),
        issue("A-2", ["src/y.py"]),
        issue("A-9", ["docs/r.md"], role="review"),
    ])
    assert rules(v) == [0]  # A-1, A-2, A-9 are clean on every other rule


# --- files:/blocked_by: must be type-guarded, not just falsiness-guarded ---

def test_a_files_value_that_is_a_bare_string_is_not_iterated_per_character():
    v = ba.audit([{"key": "A-1", "role": "coding", "group": 1,
                   "files": "src/x.py", "blocked_by": []}])
    assert 2 in rules(v)
    detail = next(x for x in v if x["rule"] == 2)["detail"]
    assert "not a list" in detail, \
        "must name the real problem (wrong type), not report a bogus " \
        "'not repo-relative: /' from iterating the string character-by-character"


def test_a_blocked_by_value_that_is_a_bare_string_does_not_desync_the_closure():
    # "AGENT-1" iterated per character would seed bogus single-letter blocker
    # keys instead of the one real key, silently breaking the closure.
    v = ba.audit([
        issue("A-1", ["src/x.py"]),
        {"key": "A-2", "role": "coding", "group": 2,
         "files": ["src/y.py"], "blocked_by": "A-1"},
    ])
    assert 3 in rules(v), "a non-list blocked_by must be treated as no blocker, not crash or desync"


def test_a_blocked_by_bare_string_does_not_falsely_license_a_shared_file():
    # If "A-1" were iterated into {'A', '-', '1'}, none of those match a real
    # key, so A-2 would incorrectly look unblocked by A-1 and the shared-file
    # check below would incorrectly fire even though A-2 declares a blocker.
    v = ba.audit([
        issue("A-1", ["src/x.py"]),
        {"key": "A-2", "role": "coding", "group": 2,
         "files": ["src/z.py"], "blocked_by": "A-1"},
    ])
    # blocked_by "A-1" is malformed (not a list) so it is treated as NO
    # blocker -- which correctly trips rule 3 (group 2, no real barrier),
    # not a rule 1 false negative on an unrelated file.
    assert 1 not in rules(v)


# --- Rule 5 (NEW): dangling blocked_by references must not read as "unblocked" ---

def test_a_blocked_by_key_absent_from_the_audited_set_is_a_rule_5_violation():
    # Regression for the finding: before the fix, a blocker naming a key
    # that JQL never returned (e.g. truncated past the 50-result default)
    # silently closed to "no blocker", so the referencing issue read as
    # unblocked with no signal that the audit's view was incomplete.
    v = ba.audit([
        issue("A-1", ["src/x.py"], role="review"),
        issue("A-2", ["src/y.py"], blocked_by=["A-999-NOT-IN-SET"], group=2),
    ])
    assert 5 in rules(v)
    detail = next(x for x in v if x["rule"] == 5)["detail"]
    assert "A-999-NOT-IN-SET" in detail


def test_a_dangling_blocker_does_not_falsely_certify_a_truncated_view_clean():
    # The acute risk: a real Rule-1 file overlap hidden behind a blocker
    # that was truncated out of the audited set must not be reported clean.
    v = ba.audit([
        issue("A-1", ["src/x.py"], role="review"),
        issue("A-2", ["src/shared.py"], blocked_by=["A-3-NOT-PRESENT"], group=2),
    ])
    assert 5 in rules(v), "a dangling reference must surface, not be silently absorbed"


def test_a_blocked_by_key_present_in_the_set_is_not_a_rule_5_violation():
    v = ba.audit([
        issue("A-1", ["src/x.py"]),
        issue("A-2", ["src/y.py"], blocked_by=["A-1"], group=2, role="review"),
    ])
    assert 5 not in rules(v)


# --- Rule 4 (D-009): enforced in BOTH directions ---

def test_a_spec_with_zero_review_issues_violates_rule_4():
    # Before the D-009 fix, "at least one per spec" was entirely
    # unenforced -- this is the counter-example the decision itself names.
    v = ba.audit([issue("A-1", ["src/a.py"]), issue("A-2", ["src/b.py"])])
    assert 4 in rules(v)
    detail = next(x for x in v if x["rule"] == 4)["detail"]
    assert "no [review] issue" in detail


def test_a_spec_with_exactly_one_review_issue_is_clean_on_rule_4():
    v = ba.audit([issue("A-1", ["src/a.py"]), issue("A-9", ["docs/r.md"], role="review")])
    assert 4 not in rules(v)


def test_duplicate_review_seats_in_one_group_still_violate_rule_4():
    # The "at most one per group" half must still work after adding the
    # "at least one per spec" half alongside it.
    v = ba.audit([
        issue("A-1", ["src/a.py"]),
        issue("A-8", ["docs/a.md"], role="review"),
        issue("A-9", ["docs/b.md"], role="review"),
    ])
    assert 4 in rules(v)


def test_an_empty_backlog_does_not_trip_the_at_least_one_review_rule():
    # echo '[]' | board_triage.py --audit must still exit 0 -- there is
    # nothing to review when there is nothing to build.
    assert ba.audit([]) == []


# --- Rule 2: repo-relative check must normalize the whole path, not just the prefix ---

def test_traversal_hidden_after_a_legitimate_looking_prefix_violates_rule_2():
    # A prefix-only check (old: f.startswith("..")) misses this: the string
    # starts with "src/", not "..", but normalizes to "../etc/passwd".
    v = ba.audit([issue("A-1", ["src/../../etc/passwd"])])
    assert 2 in rules(v)


def test_deeply_nested_traversal_violates_rule_2():
    v = ba.audit([issue("A-1", ["a/b/../../../../../etc/passwd"])])
    assert 2 in rules(v)


def test_a_harmless_leading_dot_slash_is_still_accepted():
    assert ba.audit([
        issue("A-1", ["./ok.py"]), issue("A-9", ["docs/r.md"], role="review"),
    ]) == []


# --- Suggestion: first_group anchoring ---

def test_a_backlog_whose_lowest_group_is_not_1_is_flagged():
    # If the true group 1 vanished entirely (e.g. every one of its issues
    # was malformed and dropped by rule 0), the lowest group actually
    # present must not be silently treated as the unblocked baseline.
    v = ba.audit([
        issue("A-2", ["src/a.py"], group=2, blocked_by=["A-1"]),
        issue("A-9", ["docs/r.md"], role="review", group=2),
    ])
    assert 3 in rules(v)


def test_a_normal_backlog_starting_at_group_1_is_not_flagged_for_that():
    v = ba.audit([issue("A-1", ["src/a.py"]), issue("A-9", ["docs/r.md"], role="review")])
    assert 3 not in rules(v)


# --- Suggestion: main()'s argv docstring stays informative ---

def test_main_still_documents_why_argv_is_accepted_but_unused():
    assert ba.main.__doc__ and "argv" in ba.main.__doc__
