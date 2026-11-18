import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, ".claude", "hooks"))

import jira_mirror  # noqa: E402


def test_load_config_returns_empty_dict_when_file_missing(tmp_path):
    assert jira_mirror.load_config(str(tmp_path / "nope.json")) == {}


def test_load_config_returns_empty_dict_on_invalid_json(tmp_path):
    bad = tmp_path / "jira-config.json"
    bad.write_text("{not json")
    assert jira_mirror.load_config(str(bad)) == {}


def test_load_config_reads_discovered_ids(tmp_path):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({"projectKey": "AGENT", "fields": {"sprint": "customfield_10020"}}))
    loaded = jira_mirror.load_config(str(cfg))
    assert loaded["projectKey"] == "AGENT"
    assert loaded["fields"]["sprint"] == "customfield_10020"


def test_issue_project_extracts_key_prefix():
    assert jira_mirror.issue_project("AGENT-14") == "AGENT"
    assert jira_mirror.issue_project("SCRUM-2") == "SCRUM"


def test_issue_project_returns_none_for_numeric_id():
    assert jira_mirror.issue_project("10073") is None


def test_agent_label_finds_claimant():
    assert jira_mirror.agent_label(["role-coding", "agent-coding-2"]) == "agent-coding-2"


def test_agent_label_returns_none_when_unclaimed():
    assert jira_mirror.agent_label(["role-coding", "group-1"]) is None


def test_append_then_load_state_reconstructs_issue(tmp_path, monkeypatch):
    monkeypatch.setattr(jira_mirror, "MIRROR_DIR", str(tmp_path))
    jira_mirror.append_event("AGENT", {
        "op": "create", "key": "AGENT-14",
        "summary": "[coding] impl login", "labels": ["role-coding", "spec-auth"],
        "status": "To Do",
    })
    state = jira_mirror.load_state("AGENT")
    assert state["AGENT-14"]["summary"] == "[coding] impl login"
    assert state["AGENT-14"]["status"] == "To Do"
    assert "role-coding" in state["AGENT-14"]["labels"]


def test_load_state_folds_later_events_over_earlier(tmp_path, monkeypatch):
    monkeypatch.setattr(jira_mirror, "MIRROR_DIR", str(tmp_path))
    jira_mirror.append_event("AGENT", {
        "op": "create", "key": "AGENT-14", "summary": "[coding] impl login",
        "labels": ["role-coding"], "status": "To Do",
    })
    jira_mirror.append_event("AGENT", {
        "op": "edit", "key": "AGENT-14", "labels": ["role-coding", "agent-coding-2"],
    })
    jira_mirror.append_event("AGENT", {
        "op": "transition", "key": "AGENT-14", "status": "In Progress",
    })
    issue = jira_mirror.load_state("AGENT")["AGENT-14"]
    assert issue["status"] == "In Progress"
    assert issue["labels"] == ["role-coding", "agent-coding-2"]
    assert issue["summary"] == "[coding] impl login", "absent fields must not clobber known ones"


def test_load_state_skips_corrupt_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(jira_mirror, "MIRROR_DIR", str(tmp_path))
    jira_mirror.append_event("AGENT", {"op": "create", "key": "AGENT-1", "status": "To Do"})
    with open(jira_mirror.mirror_path("AGENT"), "a") as fh:
        fh.write("{{{ truncated\n")
    jira_mirror.append_event("AGENT", {"op": "create", "key": "AGENT-2", "status": "To Do"})
    state = jira_mirror.load_state("AGENT")
    assert set(state) == {"AGENT-1", "AGENT-2"}


def test_load_state_returns_empty_when_no_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(jira_mirror, "MIRROR_DIR", str(tmp_path))
    assert jira_mirror.load_state("NOPE") == {}


def test_agent_label_handles_non_iterable_input():
    """Regression: agent_label must not raise on truthy non-iterables."""
    assert jira_mirror.agent_label(42) is None
    assert jira_mirror.agent_label(True) is None
    assert jira_mirror.agent_label({"key": "value"}) is None


def test_folded_issues_do_not_share_one_defaults_list(tmp_path, monkeypatch):
    """Regression: dict(_DEFAULTS) is a SHALLOW copy.

    Every folded issue held the *same* labels list and the same files list, so
    one consumer doing issue["labels"].append() rewrote every other issue and
    poisoned the module-level default for the rest of the process -- every
    later-folded issue then arrived carrying a phantom agent-* claim. The dict
    literal this replaced built fresh lists per call.

    _DEFAULTS is monkeypatched with an equivalent dict so that a reverted fix
    poisons a throwaway copy instead of the real module state for the rest of
    the run.
    """
    monkeypatch.setattr(jira_mirror, "MIRROR_DIR", str(tmp_path))
    monkeypatch.setattr(
        jira_mirror, "_DEFAULTS",
        {"summary": "", "labels": [], "status": "", "files": []},
    )
    jira_mirror.append_event("AGENT", {"op": "create", "key": "AGENT-1", "status": "To Do"})
    jira_mirror.append_event("AGENT", {"op": "create", "key": "AGENT-2", "status": "To Do"})

    state = jira_mirror.load_state("AGENT")
    assert state["AGENT-1"]["labels"] is not state["AGENT-2"]["labels"]
    assert state["AGENT-1"]["files"] is not state["AGENT-2"]["files"]

    state["AGENT-1"]["labels"].append("agent-coding-1")
    assert state["AGENT-2"]["labels"] == [], "issues must not alias one list"
    assert jira_mirror._DEFAULTS["labels"] == [], "the module default must survive"
    assert jira_mirror.load_state("AGENT")["AGENT-2"]["labels"] == [], \
        "a later fold must still see the issue as unclaimed"


def test_normalize_path_makes_both_sides_of_a_files_comparison_agree():
    """claim_gate compares os.path.relpath() output against journalled paths.
    Unnormalised, './src/a.py' equals nothing and that one file drops out of
    both the overlap check and the claim gate, silently."""
    assert jira_mirror.normalize_path("./src/a.py") == "src/a.py"
    assert jira_mirror.normalize_path("src//a.py") == "src/a.py"
    assert jira_mirror.normalize_path("  src/a.py  ") == "src/a.py"
    assert jira_mirror.normalize_path("src/") == "src"
    assert jira_mirror.normalize_path("src/a.py") == "src/a.py", "no-op on a clean path"
    # Best-effort, never raises: unusable values normalise to '' and are dropped.
    assert jira_mirror.normalize_path("") == ""
    assert jira_mirror.normalize_path(None) == ""


def test_load_state_skips_valid_json_non_object_lines(tmp_path, monkeypatch):
    """Regression: load_state must skip valid JSON that isn't an object."""
    monkeypatch.setattr(jira_mirror, "MIRROR_DIR", str(tmp_path))
    jira_mirror.append_event("AGENT", {"op": "create", "key": "AGENT-1", "status": "To Do"})
    # Append valid JSON that isn't a dict
    with open(jira_mirror.mirror_path("AGENT"), "a") as fh:
        fh.write("42\n")  # valid JSON, not an object
        fh.write("null\n")  # valid JSON, not an object
        fh.write("[1, 2, 3]\n")  # valid JSON, not an object
    jira_mirror.append_event("AGENT", {"op": "create", "key": "AGENT-2", "status": "To Do"})
    state = jira_mirror.load_state("AGENT")
    assert set(state) == {"AGENT-1", "AGENT-2"}, "should skip non-object JSON lines"


# --- path semantics shared by the overlap check, claim gate and evidence ----

def test_an_entry_covers_itself_a_directory_and_a_glob():
    covers = jira_mirror.path_declared_by
    assert covers("src/a.py", "src/a.py")
    assert covers("src", "src/pkg/a.py") and covers("src/", "src/a.py")
    assert covers("./src//pkg", "src/pkg/a.py")
    assert covers("tests/**", "tests/unit/test_a.py")
    assert covers("tests/**/*.py", "tests/test_a.py")
    assert covers("docs/*.md", "docs/a.md")


def test_an_entry_does_not_cover_unrelated_paths():
    covers = jira_mirror.path_declared_by
    assert not covers("src", "srcfoo/a.py"), "a prefix is not a directory"
    assert not covers("src/a.py", "src/a.pyc")
    assert not covers("docs/*.md", "docs/sub/a.md"), "a single * does not cross directories"
    assert not covers("tests/**/*.py", "tests/data.json")
    assert not covers("", "src/a.py") and not covers("src", "")


def test_entries_overlap_is_symmetric_and_exact_for_paths_and_directories():
    overlap = jira_mirror.entries_overlap
    for a, b, expected in (("src/a.py", "src/a.py", True), ("src", "src/a.py", True),
                           ("src/x", "src/y", False), ("src", "srcfoo", False),
                           ("tests/**/*.py", "tests/a/test_x.py", True),
                           ("tests/*.py", "tests/sub/test_x.py", False),
                           ("src/billing", "src/auth/**", False),
                           ("src", "src/auth/**", True)):
        assert overlap(a, b) is expected, (a, b)
        assert overlap(b, a) is expected, (b, a)


def test_two_globs_are_treated_conservatively():
    overlap = jira_mirror.entries_overlap
    assert overlap("src/**/*.py", "src/auth/*.py")
    assert not overlap("src/auth/*.py", "src/billing/*.py")
    assert overlap("**/*.py", "src/auth/*.py"), "a prefix-less glob may match anything"


def test_unsafe_path_reasons():
    reason = jira_mirror.unsafe_path_reason
    assert reason("src/a.py") is None and reason("src/") is None and reason("a/**") is None
    assert "absolute" in reason("/etc/passwd") and "absolute" in reason("~/x")
    assert "'..'" in reason("a/../../b")
    assert reason("  ") == "is empty"


def test_issue_keys_are_normalised():
    norm = jira_mirror.normalize_issue_key
    assert norm("ag-14") == "AG-14" and norm(" AG-14 ") == "AG-14"
    assert norm("https://x.atlassian.net/browse/ag-14?focusedId=1") == "AG-14"
    assert norm("10234") == "10234" and jira_mirror.is_numeric_issue_id("10234")
    assert jira_mirror.issue_project("ag-14") == "AG"
    assert jira_mirror.issue_project("10234") is None


# --- the Jira config is found from a linked worktree (review cycle 2, R4) ----

def _git(cwd, *args):
    import subprocess
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, capture_output=True)


def _hooks_copy_in(root):
    """A checkout that carries its own copy of the hooks, like a real worktree does."""
    import shutil
    target = root / ".claude" / "hooks"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(os.path.join(REPO, ".claude", "hooks"), target)
    return target


def _load_config_from(hooks_dir, env_extra=None):
    """Run load_config() using the hooks copy in `hooks_dir`, as a hook there would."""
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "JIRA_CONFIG_PATH"}
    env.update(env_extra or {})
    code = ("import sys, json; sys.path.insert(0, r'%s'); import jira_mirror; "
            "print(json.dumps(jira_mirror.load_config()))" % hooks_dir)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, check=True).stdout
    return json.loads(out)


def test_a_worktree_finds_the_main_checkouts_gitignored_jira_config(tmp_path):
    """The config is gitignored, so a linked worktree has none of its own. Without
    the fallback every Jira hook run from a worktree reads an empty config and
    fails open with "no projectKey configured" -- the whole gate, silently off."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _hooks_copy_in(main)
    (main / ".gitignore").write_text(".claude/jira-config.json\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")
    (main / ".claude" / "jira-config.json").write_text(json.dumps(
        {"projectKey": "AG", "gatedStatuses": ["In Review", "Done"]}))

    worktree = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(worktree), "-b", "issue-1")
    assert not (worktree / ".claude" / "jira-config.json").exists(), "gitignored: not checked out"

    cfg = _load_config_from(worktree / ".claude" / "hooks")
    assert cfg.get("projectKey") == "AG"
    assert cfg.get("gatedStatuses") == ["In Review", "Done"]


def test_a_worktrees_own_config_wins_when_it_has_one(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _hooks_copy_in(main)
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")
    (main / ".claude" / "jira-config.json").write_text(json.dumps({"projectKey": "MAIN"}))
    worktree = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(worktree), "-b", "issue-2")
    (worktree / ".claude" / "jira-config.json").write_text(json.dumps({"projectKey": "WT"}))
    assert _load_config_from(worktree / ".claude" / "hooks")["projectKey"] == "WT"


def test_an_explicit_config_path_is_never_replaced_by_the_fallback(tmp_path):
    """JIRA_CONFIG_PATH pointing at a missing file means "no config", for tests and
    for installs that keep it elsewhere -- not "go and find the main checkout's"."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _hooks_copy_in(main)
    (main / ".claude" / "jira-config.json").write_text(json.dumps({"projectKey": "MAIN"}))
    cfg = _load_config_from(main / ".claude" / "hooks",
                            {"JIRA_CONFIG_PATH": str(tmp_path / "absent.json")})
    assert cfg == {}


# --- R2: `Files:` matching on a case-insensitive disk (AG-18) ----------------

def test_path_matching_folds_case_only_where_the_disk_does(monkeypatch):
    import jira_mirror as jm
    monkeypatch.setattr(jm, "_fold_case", lambda: True)
    assert jm.path_declared_by("src/login.py", "SRC/Login.py")
    assert jm.path_declared_by("SRC/", "src/a/b.py")
    assert jm.path_declared_by("tests/**/*.py", "Tests/Unit/test_a.PY")
    assert not jm.path_declared_by("src/login.py", "src/login2.py")
    monkeypatch.setattr(jm, "_fold_case", lambda: False)
    assert jm.path_declared_by("src/login.py", "src/login.py")
    assert not jm.path_declared_by("src/login.py", "SRC/Login.py")
    assert not jm.path_declared_by("SRC/", "src/a/b.py")
