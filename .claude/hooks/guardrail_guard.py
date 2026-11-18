#!/usr/bin/env python3
"""PreToolUse hook on Write / Edit / NotebookEdit -- agents do not edit their own guardrails.

Every control in this harness is a file: the hooks, the settings that wire them,
the gate and capability configuration, the AWS sandbox declaration, the approval
markers a human creates, and the Jira config that says which statuses are gated.
An agent that can edit those can switch any of them off, so by default it
cannot.

Changing the harness is still legitimate work -- it is how the harness improves
-- but it is the operator's decision, made when the session is launched:

    CLAUDE_HARNESS_MAINTENANCE=1 claude

A hook runs with the environment of the Claude Code process, not of the agent's
shell, so an agent cannot set this for itself. In a maintenance session the
protected files may be edited (and the audit log records each one); the claim
gate, the verification gate and every other hook still apply.

Shell writes to most of these paths are separately denied by the Bash sandbox
(`.claude` settings, hooks, agents, skills, commands are sandbox-protected, and
settings.json adds the rest), so this hook covers the file-editing tools.

Approval markers are never editable by an agent, in any mode: they exist to
record a human decision.

FAIL CLOSED for a protected path; FAIL OPEN when nothing could be parsed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import (  # noqa: E402
    read_payload, allow, block, audit, as_dict, git_common_dir, human_dir,
    canonical_path, path_is_under, respell_under, state_root, real_path, read_small,
)

EVENT = "PreToolUse"
WATCHED = ("Write", "Edit", "NotebookEdit", "MultiEdit")
MAINTENANCE_ENV = "CLAUDE_HARNESS_MAINTENANCE"

# Repo-relative, lower case. A trailing slash protects everything beneath the
# directory. Agent definitions, rules and skills are here because they are
# controls too: an agent file's frontmatter is what denies a reviewer its
# editing tools, and a rule is what every session is told to obey.
PROTECTED = (
    ".claude/hooks/",
    ".claude/agents/",
    ".claude/rules/",
    ".claude/skills/",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/gates.json",
    ".claude/capabilities.json",
    ".claude/aws-sandbox.json",
    ".claude/jira-config.json",
    ".github/workflows/",
    "scripts/harness_admin.py",
    # git reads these and runs what they name (hooks, `core.fsmonitor`,
    # `core.sshCommand`), for the operator's own git as much as for a hook's.
    ".git/config",
    ".git/commondir",
    ".git/hooks/",
)
HUMAN_ONLY = (".claude/approvals/",)


def in_human_dir(path):
    """Is `path` inside the directory that holds operator decisions?

    ~/.claude/harness/ holds AWS approval markers and the sandbox declaration
    for every repository on this machine. No agent writes there, in any mode.
    Compared with symlinks resolved and case folded: `~/.claude/HARNESS/` is the
    same directory on a case-insensitive disk, and a guard that missed it let an
    agent write its own approval.
    """
    roots = (human_dir(), os.path.join(os.path.expanduser("~"), ".claude", "harness"))
    return any(path_is_under(path, root, fold=True) for root in roots)


def mirror_dir():
    return os.path.join(state_root(), "jira-mirror")


def in_mirror_dir(path):
    """Is `path` the Jira mirror? Only the journal hook writes it.

    The verification gate reads the mirror for an issue's `Run:` command, its
    files, and who applied a bypass label. One appended line claiming the lead
    applied `skip-verify` used to open the gate (review cycle 4), so no agent
    writes it with a file tool, in any mode; the sandbox denies the shell.
    """
    return path_is_under(path, mirror_dir(), fold=True)


def maintenance():
    return os.environ.get(MAINTENANCE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def repo_relative(path, payload):
    """Path relative to the checkout or worktree that contains it, or None.

    The WHOLE path is resolved first, final component included, so a symlink to
    a guardrail file is judged as the file it points at. A worktree of this
    repository carries its own copy of the guardrail files; those copies become
    the real ones when the branch merges, so they are protected too.
    """
    path = canonical_path(path, fold=False)
    project = canonical_path(os.environ.get("CLAUDE_PROJECT_DIR")
                             or as_dict(payload).get("cwd") or os.getcwd(), fold=False)
    project_common = git_common_dir(project)
    d = os.path.dirname(path)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            common = git_common_dir(d)
            if common and project_common and (
                    common.casefold() == project_common.casefold()
                    or _same_file(common, project_common)):
                return os.path.relpath(path, d)
            return None
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    if path_is_under(path, project, fold=True):
        return path[len(project.rstrip(os.sep)) + 1:]
    return None


def _same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


MAX_WORKTREES = 256


def covered(rel, entries):
    """The protected entry that covers `rel`, or None. Case is always folded:
    treating two spellings as one file is the safe error for a guard."""
    rel = rel.replace(os.sep, "/").casefold()
    for entry in entries:
        if entry.endswith("/"):
            if rel.startswith(entry) or rel == entry.rstrip("/"):
                return entry
        elif rel == entry:
            return entry
    return None


def _project_roots(payload, path=None):
    """Every checkout of this repository: this one, the main one, each linked
    worktree git has registered, and the one `path` itself sits in.

    Each worktree carries its own copy of the guardrail files, and those copies
    are protected too (see repo_relative), so a hard link has to be checked
    against all of them, not just the copies in the session's own checkout.
    """
    start = (os.environ.get("CLAUDE_PROJECT_DIR") or as_dict(payload).get("cwd")
             or os.getcwd())
    roots = [real_path(start)]

    def add(root):
        # One spelling, and one entry, per directory. The registry below and
        # the path being judged are both the worker's to write: a root that is
        # a directory already listed, under another spelling, must not get in,
        # or the path would be respelled away from the spelling the protected
        # list is compared in (review cycle 5: that allowed every guardrail
        # file under its /System/Volumes/Data spelling).
        root = real_path(root)
        for known in roots:
            root = respell_under(root, known)
        for known in roots:
            try:
                if root == known or os.path.samefile(root, known):
                    return
            except OSError:
                continue
        roots.append(root)

    common = git_common_dir(start)
    if common:
        add(os.path.dirname(common))
        registry = os.path.join(common, "worktrees")
        try:
            names = sorted(os.listdir(registry))[:MAX_WORKTREES]
        except OSError:
            names = []
        for name in names:
            # Never a plain open(): a FIFO left here hung the guard on every
            # write, and this hook has no time limit of its own.
            dot_git = (read_small(os.path.join(registry, name, "gitdir")) or "").split(
                "\n", 1)[0].strip()
            if dot_git and os.path.isabs(dot_git):
                add(os.path.dirname(dot_git))
    if path and common:
        d = os.path.dirname(real_path(path))
        while True:
            if os.path.exists(os.path.join(d, ".git")):
                theirs = git_common_dir(d)
                try:
                    if theirs and os.path.samefile(theirs, common):
                        add(d)
                except OSError:
                    pass
                break
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return roots


def _files_under(path):
    if os.path.isfile(path):
        yield path
        return
    for folder, _dirs, names in os.walk(path):
        for name in names:
            yield os.path.join(folder, name)


def hard_link_alias(path, payload):
    """The protected file `path` is another NAME for, or None.

    A hard link is a second directory entry for the same file. There is nothing
    for realpath to resolve, so `docs/notes.py` linked to `.claude/hooks/x.py`
    compared as an ordinary file while every byte written to it landed in the
    hook (review cycle 3). Identity is the inode, so that is what is compared --
    only for a file with more than one link, which is rare enough that walking
    the protected directories then costs nothing in the normal case.

    Returns a path to judge in place of the one given: the protected file's own.
    """
    try:
        st = os.stat(path)               # follows symlinks: the file actually written
    except OSError:
        return None                      # does not exist yet: it aliases nothing
    import stat as stat_module
    if not stat_module.S_ISREG(st.st_mode) or st.st_nlink < 2:
        return None
    wanted = (st.st_dev, st.st_ino)
    candidates = [human_dir(), os.path.join(os.path.expanduser("~"), ".claude", "harness")]
    candidates.append(mirror_dir())
    for root in _project_roots(payload, path):
        for entry in PROTECTED + HUMAN_ONLY:
            candidates.append(os.path.join(root, entry.rstrip("/")))
    here = canonical_path(path, fold=True)
    for candidate in candidates:
        for found in _files_under(candidate):
            try:
                other = os.stat(found)
            except OSError:
                continue
            if (other.st_dev, other.st_ino) == wanted and canonical_path(found, fold=True) != here:
                return found
    return None


def main():
    p = read_payload()
    if p.get("tool_name") not in WATCHED:
        allow()
    tool_input = as_dict(p.get("tool_input"))
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not isinstance(path, str) or not path:
        allow()
    path = hard_link_alias(path, p) or path
    # One spelling per directory before anything is compared. unfirm() removes
    # the macOS data-volume prefix; respell_under() then catches any other
    # second spelling by inode. The places that are judged by their own path --
    # operator records, the mirror -- go last, so nothing respells away from them.
    path = real_path(os.path.expanduser(path))
    for root in (_project_roots(p, path)
                 + [human_dir(), os.path.join(os.path.expanduser("~"), ".claude", "harness"),
                    mirror_dir()]):
        path = respell_under(path, root)
    if in_mirror_dir(path):
        audit(EVENT, {"file_path": path}, "block", "the Jira mirror is written by hooks only")
        block(EVENT, {}, (
            "[guardrail-guard] Blocked writing {}\n\nThis is the Jira mirror the "
            "verification gate reads. Only the journal hook writes it, from what Jira "
            "returned. If it is wrong, read the issue again with getJiraIssue -- the read "
            "re-syncs it -- or ask the operator to run scripts/mirror_resync.py."
        ).format(path))
    if in_human_dir(path):
        audit(EVENT, {"file_path": path}, "block", "operator records are human-only")
        block(EVENT, {}, (
            "[guardrail-guard] Blocked writing {}\n\nThis directory records operator "
            "decisions -- AWS approvals and the sandbox declaration. An agent never "
            "writes there, in any mode. Report what you intended to do and why, and wait "
            "for the operator."
        ).format(path))
    rel = repo_relative(path, p)
    if rel is None:
        allow()

    if covered(rel, HUMAN_ONLY):
        audit(EVENT, {"file_path": rel}, "block", "approval markers are human-only")
        block(EVENT, {}, (
            "[guardrail-guard] Blocked writing {}\n\nApproval markers record a human "
            "decision. An agent never creates, edits or removes one, in any mode. Report "
            "what you intended to do and why, and wait for the operator."
        ).format(rel))

    hit = covered(rel, PROTECTED)
    if not hit:
        allow()
    if maintenance():
        allow(EVENT, {"file_path": rel}, reason="guardrail file edited in a maintenance session")
    audit(EVENT, {"file_path": rel}, "block", "guardrail file outside a maintenance session")
    block(EVENT, {}, (
        "[guardrail-guard] Blocked writing {}\n\nThis file is part of the harness's own "
        "guardrails ({}), and agents do not edit those in an ordinary session -- an agent "
        "that could would be able to switch off any check it met.\n\nIf the harness itself "
        "is what you were asked to change, stop and tell the operator: changing it needs a "
        "session launched with {}=1. Do not look for another way to make the change."
    ).format(rel, hit, MAINTENANCE_ENV))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # nothing was established about the call
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
