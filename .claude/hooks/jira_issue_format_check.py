#!/usr/bin/env python3
"""PreToolUse hook on createJiraIssue -- enforce the task issue shape.

Replaces the old TaskCreated format check. Exit 2 prevents the malformed issue
from being created at all, which is strictly better than the rollback-after-
creation the task store allowed.

Required shape for a Task in the agent project:
    Summary:     [coding|frontend|backend|devops|sa|review] <verb> <what>
    Description: Spec: / Files: / Acceptance: / Run: sections
    Labels:      one role-*, one spec-*

Epics are exempt -- they describe a spec, not a unit of work.
Bypass: the skip-format-check label, for coordination or research issues.

Scope: only the configured agent project. The site holds real client work; a
guardrail that blocked issue creation there would be a serious defect.

FAIL OPEN: unparseable payload, missing config, or any internal error allows.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import (  # noqa: E402
    read_payload, allow, block, audit, tool_input_of,
    create_labels, issue_type_name, jira_op, as_dict, caller_identity, edit_labels, ROLES,
)
import jira_mirror  # noqa: E402
import jira_mirror_journal  # noqa: E402  (parse_files -- one parser, two hooks)

EVENT = "PreToolUse"
CREATE = "mcp__plugin_atlassian_atlassian__createJiraIssue"
SUMMARY_TAG = re.compile(r"^\s*\[(%s)\]\s*\S" % "|".join(ROLES), re.I)
REQUIRED_SECTIONS = ("Spec:", "Files:", "Acceptance:", "Run:")

# Statuses at which an issue has stopped writing. Must agree with claim_gate.py.
TERMINAL_STATUSES = ("In Review", "Done")


def _scope_of(labels):
    """(spec-*, group-*) for an issue, either possibly None."""
    spec = next((l for l in labels if l.startswith("spec-")), None)
    group = next((l for l in labels if l.startswith("group-")), None)
    return spec, group


def files_overlap(project, labels, description):
    """(other_issue_key, overlapping_paths) if this create collides, else None.

    fullstack-agent.md calls sprint-wide Files: disjointness "the sole guarantee
    against conflicts under the shared-tree pool model" and nothing enforced it.
    Since no atomic claim primitive is reachable through Jira, this is the last
    layer between a claim race and clobbered work.

    Scope is spec + group, because the mirror has no sprint field. An issue
    without a spec label is not compared at all -- a missing label is unknown,
    not a match, and blocking on it would reject valid creates.

    TERMINAL_STATUSES must match claim_gate's notion of "landed", or the two
    guardrails contradict: this hook would refuse to create a follow-up issue for
    files whose only other declarer is at In Review, while claim_gate happily
    lets those same files be edited. The disjointness rule exists to stop two
    CONCURRENT writers, and an issue at In Review has finished writing.
    """
    spec, group = _scope_of(labels)
    if not spec:
        return None
    mine = set(jira_mirror_journal.parse_files(description) or [])
    if not mine:
        return None
    try:
        state = jira_mirror.load_state(project)
    except Exception:
        return None
    for key, issue in sorted(state.items()):
        other_labels = [str(l) for l in (issue.get("labels") or [])]
        other_spec, other_group = _scope_of(other_labels)
        if other_spec != spec or other_group != group:
            continue
        if str(issue.get("status") or "") in TERMINAL_STATUSES:
            continue
        # Normalised on both sides: `mine` by parse_files, the journalled paths
        # here, because the journal is never migrated and still holds entries
        # recorded before the journaller normalised them.
        # By coverage, not string equality: a directory or glob entry collides
        # with every path beneath it (see jira_mirror.entries_overlap).
        clash = {m for m in mine
                 for f in (issue.get("files") or [])
                 if jira_mirror.entries_overlap(m, f)}
        if clash:
            return key, clash
    return None


BYPASS_LABELS = ("skip-format-check", "skip-verify")
_RUN_LINE = re.compile(r"^\s*Run:\s*(.+?)\s*$", re.M)
_FILES_LINE = re.compile(r"^\s*Files:\s*(.+)$", re.M)


def description_problems(description):
    """Shape problems in a Task description, as messages for the author."""
    problems = []
    missing_sections = [s for s in REQUIRED_SECTIONS if s not in description]
    if missing_sections:
        problems.append("description is missing: {}".format(", ".join(missing_sections)))
        return problems

    # A bare-substring `Files:` check accepted shapes the journaller's anchored
    # parser cannot read (a bolded label, a bullet list, the word mid-sentence).
    # Those creates passed while journalling no paths at all, silently blinding
    # both the overlap check and claim_gate. Require a form both agree on.
    if not jira_mirror_journal.parse_files(description):
        problems.append(
            "Files: is present but no paths could be parsed from it. Put it on its "
            "own line as `Files: a/b.py, c/d.py` -- comma-separated, no bullets and "
            "no markdown emphasis on the label. The paths are journalled and drive "
            "the overlap check and the claim gate, so an unparseable list silently "
            "disables both."
        )
    else:
        match = _FILES_LINE.search(description)
        for raw in (match.group(1).split(",") if match else []):
            reason = jira_mirror.unsafe_path_reason(raw)
            if reason and raw.strip():
                problems.append("Files: entry '{}' {}".format(raw.strip(), reason))

    run = _RUN_LINE.search(description)
    if run:
        command = run.group(1)
        wrapped = len(command) >= 2 and command[0] == command[-1] == "`"
        if "\\" in command and not wrapped:
            problems.append(
                "Run: contains a backslash but is not wrapped in backticks. Jira stores "
                "descriptions as markdown and drops backslashes, which silently changes "
                "the command that verification will run. Write it as Run: `<command>`.")
    return problems


def check_create(p, tool_input, cfg, project):
    if (tool_input.get("projectKey") or "") != project:
        allow(EVENT, p, reason="issue targets another project -- not policed")

    who = caller_identity(p)
    labels, _ = create_labels(tool_input)
    summary = str(tool_input.get("summary") or "")
    tag_match = SUMMARY_TAG.match(summary)
    role_labels = [l for l in labels if l.startswith("role-")]

    # An Epic describes a spec, not a unit of work, so it carries no task shape.
    # But "Epic" is just a value the author types: an issue that calls itself an
    # Epic while carrying a role tag or a role label is a Task trying to skip
    # the check, and is policed as one.
    if issue_type_name(tool_input).lower() == "epic" and not tag_match and not role_labels:
        allow(EVENT, p, reason="epics are exempt from the task shape")

    # as_dict, not `or {}`: additional_fields commonly arrives as a JSON string.
    raw_additional_fields = tool_input.get("additional_fields")
    additional_fields_unreadable = (
        raw_additional_fields is not None
        and not isinstance(raw_additional_fields, dict)
    )

    problems = []

    if "skip-format-check" in labels:
        if who["is_lead"]:
            allow(EVENT, p, reason="bypass label applied by the lead")
        problems.append(
            "the skip-format-check label is only honoured when the lead applies it "
            "(you are '{}')".format(who["name"] or "an unnamed worker"))
    if "skip-verify" in labels and not who["is_lead"]:
        problems.append(
            "the skip-verify label can only be applied by the lead (you are '{}')".format(
                who["name"] or "an unnamed worker"))

    description = tool_input.get("description")
    description = description if isinstance(description, str) else ""

    if additional_fields_unreadable:
        # Distinct from "no role-* label" / "no spec-* label": those read as
        # if no labels were supplied at all, when the real defect is that
        # additional_fields arrived as a JSON string rather than an object.
        problems.append(
            "additional_fields was a {} (\"{}\"), not a JSON object -- it must be "
            "a JSON object (e.g. {{\"labels\": [...]}}), not a JSON-encoded "
            "string; as a result no labels could be read from it".format(
                type(raw_additional_fields).__name__, raw_additional_fields)
        )

    if not tag_match:
        problems.append(
            "summary must start with a role tag -- one of {}".format(
                " ".join("[%s]" % r for r in ROLES))
        )

    problems.extend(description_problems(description))

    if not role_labels:
        problems.append("no role-* label (e.g. role-coding)")
    if not any(l.startswith("spec-") for l in labels):
        problems.append("no spec-* label (e.g. spec-auth-api)")

    if tag_match and role_labels:
        tagged = "role-" + tag_match.group(1).lower()
        if tagged not in role_labels:
            problems.append(
                "summary tag and role label disagree: summary says {}, labels say {}".format(
                    tagged, ", ".join(role_labels))
            )

    overlap = files_overlap(project, labels, description)
    if overlap:
        problems.append(
            "Files: overlaps {} in the same spec+group, which already declares {}. "
            "No two issues in one scope may write the same file -- this is the only "
            "guarantee against concurrent teammates clobbering each other. Split the "
            "work so the paths are disjoint, or sequence the two issues into "
            "different groups.".format(overlap[0], ", ".join(sorted(overlap[1])))
        )

    if problems:
        block(EVENT, p, (
            "Jira issue creation blocked by the format check:\n  - {}\n\n"
            "Required shape:\n"
            "  Summary:     [coding|frontend|backend|devops|sa|review] <verb> <what>\n"
            "  Description: Spec: <path>\\nFiles: <paths>\\nAcceptance: <criteria>\\nRun: `<command>`\n"
            "  Labels:      spec-<slug>, role-<role>\n\n"
            "For a coordination or research issue that genuinely has no verification, "
            "the lead may add the skip-format-check label."
        ).format("\n  - ".join(problems)))

    allow(EVENT, p, reason="issue format ok")


def check_edit(p, tool_input, cfg, project):
    """Police the two things an edit can do to defeat the other gates.

    1. Apply a bypass label. `skip-verify` turns the verification gate off for
       an issue, so a worker applying it to its own issue is a self-granted
       pass. Re-sending a bypass label the issue already carries is fine --
       editJiraIssue replaces the whole labels array, so every edit must.
    2. Rewrite the description. `Files:` and `Run:` are what the claim gate and
       the verification gate enforce; a worker that can change them can point
       verification at `true`. Only the lead authors them. The new text is also
       checked for the same shape a create is.
    """
    key = jira_mirror.normalize_issue_key(tool_input.get("issueIdOrKey"))
    if jira_mirror.issue_project(key) != project:
        allow(EVENT, p, reason="edit targets another project -- not policed")

    who = caller_identity(p)
    worker = who["name"] or "an unnamed worker"
    known = jira_mirror.load_state(project).get(key) or {}
    problems = []

    new_labels = edit_labels(tool_input)
    if new_labels is not None and not who["is_lead"]:
        already = set(known.get("labels") or [])
        added = [l for l in new_labels if l in BYPASS_LABELS and l not in already]
        if added:
            problems.append(
                "only the lead may apply {} (you are '{}'). Ask the lead if this issue "
                "genuinely needs it.".format(" / ".join(added), worker))

    fields = as_dict(tool_input.get("fields"))
    description = fields.get("description")
    if isinstance(description, str):
        if not who["is_lead"]:
            problems.append(
                "only the lead may change an issue's description (you are '{}'). Files: "
                "and Run: are what the gates enforce; report what needs changing as a "
                "comment and message the lead.".format(worker))
        labels_now = new_labels if new_labels is not None else (known.get("labels") or [])
        is_task = bool(SUMMARY_TAG.match(str(fields.get("summary") or known.get("summary") or ""))
                       or any(str(l).startswith("role-") for l in labels_now))
        bypassed = ("skip-format-check" in labels_now
                    and jira_mirror.bypass_author(known, "skip-format-check") == "lead")
        if is_task and not bypassed:
            problems.extend(description_problems(description))

    if problems:
        block(EVENT, p, "Jira issue edit blocked by the format check:\n  - {}".format(
            "\n  - ".join(problems)))
    allow(EVENT, p, reason="issue edit ok")


def main():
    p = read_payload()
    op = jira_op(p.get("tool_name"))
    if op not in ("createJiraIssue", "editJiraIssue"):
        allow()

    # A bare `as_dict(p.get("tool_input"))` reduces a JSON-string tool_input to
    # {}, which then names no project, so the scope check waves it through as
    # "another project" and the whole format check is disabled by a payload the
    # model merely stringified. Decoding it means the issue is policed on its
    # real contents.
    tool_input, unreadable = tool_input_of(p)
    if unreadable:
        # Scope is unknowable without the project. The site holds real client
        # work and blocking a create there would be a serious defect, so fail
        # open -- but record the true cause.
        allow(EVENT, p, reason=(
            "format check did not run: {} -- target project unknowable, "
            "fail-open".format(unreadable)))
    cfg = jira_mirror.load_config()
    project = cfg.get("projectKey")
    if not project:
        allow(EVENT, p, reason="no projectKey configured -- fail-open")

    if op == "createJiraIssue":
        check_create(p, tool_input, cfg, project)
    check_edit(p, tool_input, cfg, project)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL OPEN
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
