"""scripts/lessons.py: agents record, share and curate lessons."""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "lessons.py")


def load():
    spec = importlib.util.spec_from_file_location("lessons_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lessons = load()


@pytest.fixture
def store(tmp_path, monkeypatch):
    d = tmp_path / "lessons"
    monkeypatch.setenv(lessons.DIR_ENV, str(d))
    return d


def add(roles="backend", tags="testing", source="AG-1 review",
        when="a test ran in a simpler setting than production",
        do="run new path-guard tests inside a real git checkout"):
    return lessons.main(["add", "--roles", roles, "--tags", tags, "--source", source,
                         "--when", when, "--do", do])


def files(store):
    return sorted(p for p in os.listdir(store) if p.endswith(".md")) if store.exists() else []


def test_add_writes_one_well_formed_lesson(store, capsys):
    assert add() == 0
    (name,) = files(store)
    assert capsys.readouterr().out.strip() == name[:-3]
    lesson = lessons.parse(str(store / name))
    assert lesson["roles"] == ["backend"] and lesson["status"] == "active"
    assert lesson["seen"][0].startswith("AG-1 review (")


def test_a_repeat_strengthens_the_existing_lesson(store, capsys):
    add()
    assert add(roles="coding", source="AG-2 review",
               when="A test ran in a simpler setting than in production.") == 0
    (name,) = files(store)
    lesson = lessons.parse(str(store / name))
    assert len(lesson["seen"]) == 2 and lesson["roles"] == ["backend", "coding"]
    assert "strengthened" in capsys.readouterr().out


def test_a_different_lesson_with_the_same_tag_is_its_own_file(store):
    add()
    add(when="mocked DynamoDB hid a missing index", do="assert the GSI exists in the stack")
    assert len(files(store)) == 2


def test_a_similar_lesson_under_other_tags_is_not_merged(store):
    add()
    add(tags="frontend")
    assert len(files(store)) == 2


def test_show_filters_by_role_and_includes_all(store, capsys):
    add(roles="backend", when="backend thing happened", do="do backend fix")
    add(roles="all", tags="process", when="everyone forgets to verify", do="verify after the last edit")
    add(roles="frontend", tags="ui", when="layout broke on mobile", do="check at 375px")
    capsys.readouterr()
    lessons.main(["show", "--role", "backend"])
    out = capsys.readouterr().out
    assert out.startswith(lessons.HEADER)
    assert "backend thing" in out and "everyone forgets" in out and "mobile" not in out


def test_show_ranks_by_repeats_and_honours_the_limit(store, capsys):
    add(when="rare mistake one", do="a")
    add(tags="x", when="common mistake two", do="b")
    add(tags="x", source="AG-9", when="common mistake two!", do="b")
    capsys.readouterr()
    lessons.main(["show", "--role", "backend", "--limit", "1"])
    out = capsys.readouterr().out
    assert "common mistake two" in out and "rare mistake" not in out


def test_show_with_nothing_recorded(store, capsys):
    assert lessons.main(["show", "--role", "sa"]) == 0
    assert "no lessons for sa yet" in capsys.readouterr().out


def test_promoted_and_retired_lessons_leave_show_but_stay_in_report(store, capsys):
    add()
    (name,) = files(store)
    assert lessons.main(["set-status", name[:-3], "promoted"]) == 0
    capsys.readouterr()
    lessons.main(["show", "--role", "backend"])
    assert "no lessons" in capsys.readouterr().out
    lessons.main(["report"])
    assert "promoted" in capsys.readouterr().out


def test_report_lists_lessons_seen_three_times_for_promotion(store, capsys):
    for n in range(3):
        add(source="AG-{}".format(n))
    capsys.readouterr()
    lessons.main(["report"])
    out = capsys.readouterr().out
    assert "consider promoting" in out and "real git checkout" in out


@pytest.mark.parametrize("override, why", [
    ({"when": "x" * 301}, "over 300"),
    ({"do": "  "}, "empty"),
    ({"when": "line one\nline two"}, "one line"),
    ({"roles": "designer"}, "--roles"),
    ({"do": "set " + "AKIA" + "Q" * 16}, "credential"),
    ({"do": "token=" + "z" * 24}, "credential"),
])
def test_add_refuses_what_should_not_be_shared(store, capsys, override, why):
    assert add(**override) == 2
    assert why in capsys.readouterr().err and files(store) == []


def test_same_day_same_slug_gets_a_new_name(store):
    add()
    add(tags="other")
    a, b = files(store)
    assert a != b and a[:-10] == b[:-10]       # same date and slug, own random suffix


def test_two_lessons_in_one_file_are_refused_not_mixed(store, capsys):
    add()
    add(tags="ui", when="layout broke on mobile", do="check at 375px")
    first, second = (store / n for n in files(store))
    first.write_text(first.read_text() + second.read_text())   # what a name clash merges to
    second.unlink()
    assert lessons.load_all() == [] and "malformed" in capsys.readouterr().err


@pytest.mark.parametrize("a, b, want", [("active", "retired", "retired"),
                                        ("retired", "active", "retired"),
                                        ("promoted", "active", "promoted"),
                                        ("retired", "promoted", "retired")])
def test_a_merged_pair_of_status_lines_keeps_the_final_one(store, a, b, want):
    add()
    (name,) = files(store)
    path = store / name
    path.write_text(path.read_text().replace("status: active", "status: {}\nstatus: {}".format(a, b)))
    assert lessons.parse(str(path))["status"] == want


def test_a_hash_in_a_field_reads_back_exactly(store):
    add(source="PR #12 review", tags="c#")
    (lesson,) = lessons.load_all()
    assert lesson["source"] == "PR #12 review" and lesson["tags"] == ["c#"]
    add(source="AG-7", tags="c#")               # strengthen: rewrites the file
    (lesson,) = lessons.load_all()
    assert lesson["source"] == "PR #12 review" and len(lesson["seen"]) == 2


@pytest.mark.parametrize("given", ["backend", "backend-1", "Backend-Agent"])
def test_show_accepts_a_role_or_an_instance_name(store, capsys, given):
    add()
    capsys.readouterr()
    assert lessons.main(["show", "--role", given]) == 0
    assert "real git checkout" in capsys.readouterr().out


@pytest.mark.parametrize("given", ["designer", "sample-1", ""])
def test_show_refuses_an_unknown_role(store, capsys, given):
    assert lessons.main(["show", "--role", given]) == 2
    assert "--role must be" in capsys.readouterr().err


def test_a_malformed_file_is_skipped_not_fatal(store, capsys):
    add()
    (store / "20200101-broken.md").write_text("no header here\n")
    capsys.readouterr()
    assert lessons.main(["show", "--role", "backend"]) == 0
    captured = capsys.readouterr()
    assert "real git checkout" in captured.out and "malformed" in captured.err


def test_the_readme_in_the_store_is_not_a_lesson(store):
    store.mkdir()
    (store / "README.md").write_text("# Lessons\n")
    assert lessons.load_all() == []


def test_set_status_on_an_unknown_lesson(store):
    assert lessons.main(["set-status", "nope", "retired"]) == 2


def test_digest_counts_recent_blocks_by_reason(tmp_path, capsys):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    old = now - datetime.timedelta(days=30)
    log = tmp_path / "audit.jsonl"
    rows = [{"captured_at": now.isoformat(), "decision": "block", "reason": "no evidence for AG-12"},
            {"captured_at": now.isoformat(), "decision": "block", "reason": "no evidence for AG-40"},
            {"captured_at": old.isoformat(), "decision": "block", "reason": "ancient"},
            {"captured_at": now.isoformat(), "decision": "allow",
             "reason": "hook error (fail-open): boom"},
            {"captured_at": now.isoformat(), "decision": "allow",
             "reason": "no projectKey configured -- fail-open"}]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n{corrupt\n")
    assert lessons.main(["digest", "--log", str(log)]) == 0
    out = capsys.readouterr().out
    assert "2  no evidence for <KEY>" in out and "ancient" not in out
    assert "failed open: 1" in out


def test_digest_without_a_log(tmp_path, capsys):
    assert lessons.main(["digest", "--log", str(tmp_path / "missing")]) == 0
    assert "no audit log" in capsys.readouterr().out


def test_the_script_runs_as_a_command(store):
    env = dict(os.environ, **{lessons.DIR_ENV: str(store)})
    done = subprocess.run([sys.executable, SCRIPT, "add", "--roles", "all", "--tags", "t",
                           "--source", "s", "--when", "w happened", "--do", "d it"],
                          capture_output=True, text=True, env=env)
    assert done.returncode == 0 and files(store)
    shown = subprocess.run([sys.executable, SCRIPT, "show", "--role", "review"],
                           capture_output=True, text=True, env=env)
    assert shown.returncode == 0 and "w happened" in shown.stdout
    bad = subprocess.run([sys.executable, SCRIPT, "nonsense"], capture_output=True, env=env)
    assert bad.returncode == 2


def _git(repo, *args):
    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                          cwd=repo, capture_output=True, text=True)


def test_lessons_from_two_branches_merge_without_conflict(tmp_path):
    """Workers in separate worktrees strengthen the same lesson, or record the same
    new one, on parallel branches. The merge must not conflict (review of AG-25)."""
    import shutil
    repo = tmp_path / "repo"
    store = repo / ".claude" / "lessons"
    store.mkdir(parents=True)
    shutil.copy(os.path.join(REPO, ".gitattributes"), repo / ".gitattributes")
    env = dict(os.environ, **{lessons.DIR_ENV: str(store)})

    def add_on(branch, **kw):
        if _git(repo, "rev-parse", "-q", "--verify", "HEAD").returncode == 0:
            assert _git(repo, "checkout", "-q", branch).returncode == 0
        args = dict(roles="backend", tags="testing", source="AG-1",
                    when="a test ran in a simpler setting than production", do="use a real repo")
        args.update(kw)
        argv = [sys.executable, SCRIPT, "add"] + [x for k, v in args.items()
                                                    for x in ("--" + k, v)]
        assert subprocess.run(argv, env=env, capture_output=True).returncode == 0
        _git(repo, "add", "-A")
        assert _git(repo, "commit", "-q", "-m", branch).returncode == 0

    _git(repo, "init", "-q", "-b", "main")
    add_on("main")
    _git(repo, "branch", "a")
    _git(repo, "branch", "b")
    add_on("a", source="AG-2", roles="coding")                  # strengthen, new role
    add_on("a", tags="ui", when="layout broke on mobile", do="check 375px")   # new lesson
    add_on("b", source="AG-3", roles="frontend")                # strengthen the same one
    add_on("b", tags="ui", when="layout broke on mobile", do="check 375px", source="AG-9")
    _git(repo, "checkout", "-q", "a")
    merged = _git(repo, "merge", "-q", "--no-edit", "b")
    assert merged.returncode == 0, merged.stdout + merged.stderr
    os.environ[lessons.DIR_ENV] = str(store)
    try:
            merged_lessons = lessons.load_all()
    finally:
        del os.environ[lessons.DIR_ENV]
    found = {}
    for lesson in merged_lessons:
        found.setdefault(lesson["when"], []).append(lesson)
    (strong,) = found["a test ran in a simpler setting than production"]
    assert len(strong["seen"]) == 3
    assert set(strong["roles"]) == {"backend", "coding", "frontend"}
    # The same new lesson on two branches is two files (own names), both intact;
    # the lead folds such duplicates at Epic close. Nothing is lost or mixed.
    mobile = found["layout broke on mobile"]
    assert len(mobile) == 2 and sorted(s for l in mobile for s in l["seen"])[0].startswith("AG-1")


def test_a_triple_dash_inside_a_field_does_not_break_the_lesson(store, capsys):
    assert add(source="AG-1 --- review", do="split on --- lines, not substrings") == 0
    (lesson,) = lessons.load_all()
    assert lesson["source"] == "AG-1 --- review" and "--- lines" in lesson["do"]


def test_digest_groups_blocks_by_cause_not_by_shared_prefix(tmp_path, capsys):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    gate = "Transition of {} to 'In Review' blocked by the verification gate: {}\n\nProduce evidence"
    reasons = [gate.format("AG-1", "no evidence record at /repo/.git/claude-harness/verified/AG/AG-1.verified"),
               gate.format("AG-2", "no evidence record at /other/.git/claude-harness/verified/AG/AG-2.verified"),
               gate.format("AG-3", "the evidence is stale: AG-3's files changed after it was recorded")]
    log = tmp_path / "audit.jsonl"
    log.write_text("".join(json.dumps({"captured_at": now, "decision": "block", "reason": r}) + "\n"
                           for r in reasons))
    lessons.main(["digest", "--log", str(log)])
    out = capsys.readouterr().out
    assert "   2  Transition of <KEY>" in out and "no evidence record at <path>" in out
    assert "   1  " in out and "stale" in out
