"""Audit a Jira backlog against the decomposition contract.

The contract (authored by /plan-it; see commands/plan-it.md):

  1. No two SIMULTANEOUSLY-READY issues declare the same path in Files:.
  2. Every issue's Files: is non-empty and repo-relative (role-review exempt --
     a review issue writes nothing, so there is no path for the claim gate to bind).
  3. Every issue in a later group has an inbound blocks link.
  4. At most one [review] issue per group, and at least one per spec (D-009 --
     the spec originally said "exactly one per group"; the implementation's
     "at most one" was right and the spec clause was wrong, but a spec with
     ZERO review seats must still be rejected, and previously was not).

There are two unnumbered rules beyond the four-clause contract:

  0. "Malformed issue entry": a list entry that isn't a dict, or is a dict
     with no non-empty 'key', is reported as a violation rather than
     silently dropped. Without it, `main()` would report `N issues, contract
     clean` while never having actually checked some of those N -- a
     confident silence that is worse than a loud one, given every entry
     here is model-authored JSON piped in from a JQL re-read.
  5. "Dangling blocked_by reference": a `blocked_by` naming a key absent
     from the audited set is reported rather than silently treated as "no
     blocker". `searchJiraIssuesUsingJql` defaults to 50 results, so a spec
     over 50 issues can silently truncate its own re-read; without this
     rule, a real Rule-1 file overlap gated behind a truncated-away blocker
     would be invisible, and the audit would report "contract clean" on a
     partial view.

Rule 1 restates the older "no two issues in the same SPRINT may write the same
file". Under one-sprint-per-spec that phrasing is vacuous, so readiness is
computed from the blocks graph instead: two issues can be ready at once unless
one transitively blocks the other.

Pure function of its input -- no filesystem, no network, no Jira. The caller
pipes in a fresh JQL re-read of the board; auditing the author's own record of
what it meant to create would validate the model against itself.

Standard library only.
"""
import json
import os
import sys


def _repo_relative(f):
    """True if `f` is a safe repo-relative path.

    A prefix-only check (`f.startswith("..")`) only catches traversal that is
    the FIRST segment. `src/../../etc/passwd` and
    `a/b/../../../../../etc/passwd` both start with something other than
    "..", so a prefix check waves them through, and the claim gate resolves
    a write to an issue by path -- exactly what clause 2 exists to protect.
    `os.path.normpath` collapses the whole path first, which is what
    determines where the write actually lands, then a single startswith(
    "..") check (plus an absolute-path check, since normpath does not turn
    an absolute path relative) catches every case while still accepting a
    harmless leading "./".
    """
    if not isinstance(f, str) or f.startswith("/"):
        return False
    normalized = os.path.normpath(f)
    return not (normalized.startswith("..") or os.path.isabs(normalized))


def _as_list(value):
    """Type-guard a value expected to be a JSON array. Coalesces anything else
    to an empty list, checking *type* rather than truthiness.

    `value or []` only guards missing/empty values (None, [], ""). A truthy
    non-list -- e.g. a model emitting "files": "src/x.py" as a bare string
    instead of a one-element array, or "blocked_by": "AGENT-1" -- passes
    straight through and then gets iterated character-by-character
    downstream (`set("src/x.py")` yields {'s','r','c',...}), which is wrong
    in a way that produces plausible-looking nonsense rather than an error.
    Every field here is model-authored JSON piped in from a JQL re-read, so
    its shape is never guaranteed; see team_hook_common.as_dict() for the
    same discipline applied to hook payloads.
    """
    return value if isinstance(value, list) else []


def _ancestors(issues):
    """key -> set of every key that transitively blocks it.

    Iterates to a fixed point rather than recursing, which also makes a cyclic
    graph terminate: a cycle simply stops growing the sets, so a malformed
    backlog returns a (possibly empty) list of findings rather than crashing
    with a stack overflow.
    """
    direct = {i["key"]: set(_as_list(i.get("blocked_by"))) for i in issues}
    closure = {k: set(v) for k, v in direct.items()}
    changed = True
    while changed:
        changed = False
        for key, blockers in closure.items():
            grown = set(blockers)
            for b in blockers:
                grown |= closure.get(b, set())
            if grown != blockers:
                closure[key] = grown
                changed = True
    return closure


def simultaneously_ready(issues):
    """Unordered pairs of keys that can be unblocked at the same moment."""
    closure = _ancestors(issues)
    keys = [i["key"] for i in issues]
    pairs = []
    for idx, a in enumerate(keys):
        for b in keys[idx + 1:]:
            if a in closure.get(b, set()) or b in closure.get(a, set()):
                continue  # one gates the other; they can never be ready together
            pairs.append((a, b))
    return pairs


def audit(issues):
    """Return a sorted list of contract violations. Empty means clean."""
    violations = []
    valid = []
    for idx, i in enumerate(issues):
        if isinstance(i, dict) and i.get("key"):
            valid.append(i)
            continue
        # A malformed entry must surface as a violation, not vanish. Silently
        # filtering it out of `issues` and letting the caller report the
        # ORIGINAL length would let "N issues, contract clean" claim entries
        # were checked when they were never actually audited -- confident
        # silence about a coverage gap that is exactly the failure mode this
        # tool exists to catch.
        label = i.get("key") if isinstance(i, dict) else None
        violations.append({
            "rule": 0,
            "keys": [str(label)] if label else ["<entry %d>" % idx],
            "detail": "not a valid issue object (must be a dict with a non-empty "
                      "'key'); dropped from every other rule's audit",
        })
    issues = valid
    by_key = {i["key"]: i for i in issues}

    # Rule 1 -- file overlap between issues that can be ready together.
    for a, b in simultaneously_ready(issues):
        shared = sorted(set(_as_list(by_key[a].get("files"))) &
                        set(_as_list(by_key[b].get("files"))))
        if shared:
            violations.append({
                "rule": 1,
                "keys": sorted([a, b]),
                "detail": "both can be ready at once and both write: %s" % ", ".join(shared),
            })

    # Rule 2 -- Files: present and repo-relative, for issues that write.
    for i in issues:
        if (i.get("role") or "").lower() == "review":
            continue   # writes nothing; the claim gate has no path to bind
        raw_files = i.get("files")
        if not isinstance(raw_files, list):
            violations.append({"rule": 2, "keys": [i["key"]],
                               "detail": "Files: is not a list (got %s: %r); the claim "
                                         "gate cannot bind a write to this issue"
                                         % (type(raw_files).__name__, raw_files)})
            continue
        if not raw_files:
            violations.append({"rule": 2, "keys": [i["key"]],
                               "detail": "Files: is empty; the claim gate cannot bind a write to this issue"})
            continue
        bad = [f for f in raw_files if not _repo_relative(f)]
        if bad:
            violations.append({"rule": 2, "keys": [i["key"]],
                               "detail": "not repo-relative: %s" % ", ".join(map(str, bad))})

    # Rule 3 -- a later group needs a barrier, or its group number is a fiction.
    first_group = min([i.get("group") or 1 for i in issues], default=1)
    if issues and first_group != 1:
        # `first_group` is derived from whatever the lowest group number
        # PRESENT happens to be, not the literal group 1 -- so if the whole
        # true first group got filtered out (e.g. every one of its issues was
        # malformed), the lowest surviving group is silently treated as the
        # unblocked baseline instead of a later group that needs a barrier.
        # Anchoring on a literal 1 everywhere would instead require a
        # barrier on every issue whenever a spec legitimately starts
        # numbering elsewhere, which is the wrong failure mode; flagging the
        # mismatch is the cheap, non-invasive middle ground.
        violations.append({
            "rule": 3, "keys": ["<lowest group %s>" % first_group],
            "detail": "lowest group present is %s, not 1; rule 3 cannot tell whether "
                      "this is genuinely the first group or its barrier record went "
                      "missing" % first_group,
        })
    for i in issues:
        if (i.get("group") or 1) > first_group and not _as_list(i.get("blocked_by")):
            violations.append({"rule": 3, "keys": [i["key"]],
                               "detail": "in group %s with no inbound blocks link, so it is "
                                         "ready immediately" % i.get("group")})

    # Rule 5 -- every blocked_by reference must resolve within the audited
    # set. A dangling reference means the audit is working from a partial
    # view -- e.g. searchJiraIssuesUsingJql's 50-result default truncating a
    # spec with more than 50 issues -- and Rule 1 silently treats a missing
    # blocker as no blocker at all, which can hide a real file overlap that
    # a fuller view would have caught. "contract clean" must not be reported
    # on a set the audit could not actually see all of.
    for i in issues:
        dangling = sorted(set(_as_list(i.get("blocked_by"))) - set(by_key))
        if dangling:
            violations.append({
                "rule": 5, "keys": [i["key"]],
                "detail": "blocked_by references key(s) not present in the audited set: "
                          "%s -- the audit is working from a partial view and cannot "
                          "certify this issue" % ", ".join(dangling),
            })

    # Rule 4 -- per D-009: at most one review seat per group, and at least
    # one review seat across the whole spec (a spec with none has no seat
    # that could ever post a verdict).
    groups = {}
    for i in issues:
        if (i.get("role") or "").lower() == "review":
            groups.setdefault(i.get("group") or 1, []).append(i["key"])
    for group, keys in sorted(groups.items()):
        if len(keys) > 1:
            violations.append({"rule": 4, "keys": sorted(keys),
                               "detail": "group %s has %d review issues; the synthesizer "
                                         "seat must be unique per group" % (group, len(keys))})
    if issues and not groups:
        violations.append({
            "rule": 4, "keys": [],
            "detail": "no [review] issue anywhere in the audited set; the spec has no "
                      "seat that could ever post a verdict (D-009: at least one review "
                      "seat per spec)",
        })

    return sorted(violations, key=lambda v: (v["rule"], v["keys"]))


RULE_NAMES = {
    0: "malformed issue entry",
    1: "simultaneously-ready file overlap",
    2: "Files: missing or not repo-relative",
    3: "later group with no inbound barrier",
    4: "review seat count wrong (at most 1/group, at least 1/spec)",
    5: "dangling blocked_by reference",
}


def main(argv=None):
    """CLI entry point: read a JSON issue array from stdin, print, and exit.

    `argv` is accepted but unused -- there are no flags to parse. It exists so
    the signature matches every other script's `main(argv=None)` convention in
    this repo and so board_triage.py can call `backlog_audit.main([])`
    uniformly when `--audit` is passed, without a special case for this module.

    Exit codes: 0 clean, 2 unparseable/malformed stdin, 3 contract violation.
    """
    try:
        issues = json.load(sys.stdin)
    except Exception as exc:
        print("AUDIT  cannot parse stdin as JSON: %s" % exc, file=sys.stderr)
        return 2
    if not isinstance(issues, list):
        print("AUDIT  expected a JSON array of issues", file=sys.stderr)
        return 2

    violations = audit(issues)
    if not violations:
        print("AUDIT  %d issues, contract clean" % len(issues))
        return 0
    print("AUDIT  %d violation(s) across %d issues" % (len(violations), len(issues)))
    for v in violations:
        print("  rule %d  %-18s %s -- %s" % (
            v["rule"], RULE_NAMES.get(v["rule"], ""), ",".join(v["keys"]), v["detail"]))
    return 3


if __name__ == "__main__":
    sys.exit(main())
