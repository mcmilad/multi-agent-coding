"""CI must run the secret scan, the linter and the test suite on every push and PR."""
import os

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO, ".github", "workflows", "ci.yml")


def _workflow():
    return yaml.safe_load(open(WORKFLOW, encoding="utf-8"))


def _triggers(wf):
    # PyYAML reads the bare key `on` as boolean True.
    return wf.get("on", wf.get(True))


def _run_steps(wf):
    return [s.get("run", "") for job in wf["jobs"].values() for s in job["steps"]]


def test_runs_on_push_and_pull_request():
    triggers = _triggers(_workflow())
    assert "push" in triggers and "pull_request" in triggers


def test_runs_secret_scan_lint_and_tests():
    runs = "\n".join(_run_steps(_workflow()))
    assert "tests/test_no_tracked_credentials.py" in runs, "secret scan step missing"
    assert "ruff check" in runs, "lint step missing"
    assert any(r.strip() == "python -m pytest -n auto" for r in _run_steps(_workflow())), (
        "full test suite step missing, or not run in parallel")


def test_the_parallel_runner_is_a_pinned_dev_dependency():
    import os
    pins = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "requirements-dev.txt")).read().split()
    assert "pytest-xdist==3.6.1" in pins and any(p.startswith("execnet==") for p in pins)


def test_installs_only_the_pinned_dev_dependencies():
    assert any("-r requirements-dev.txt" in r for r in _run_steps(_workflow()))


def test_token_permissions_are_read_only():
    assert _workflow().get("permissions") == {"contents": "read"}


def test_every_action_is_version_pinned():
    wf = _workflow()
    uses = [s["uses"] for job in wf["jobs"].values() for s in job["steps"] if "uses" in s]
    assert uses
    unpinned = [u for u in uses if "@" not in u or u.endswith(("@main", "@master", "@latest"))]
    assert not unpinned, "actions without a version pin: {}".format(unpinned)


def test_checkout_does_not_persist_credentials():
    wf = _workflow()
    checkouts = [s for job in wf["jobs"].values() for s in job["steps"]
                 if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkouts and all(
        s.get("with", {}).get("persist-credentials") is False for s in checkouts)


def test_every_job_has_a_timeout():
    assert all("timeout-minutes" in job for job in _workflow()["jobs"].values())


def test_pre_commit_runs_the_same_secret_scan():
    cfg = yaml.safe_load(open(os.path.join(REPO, ".pre-commit-config.yaml"), encoding="utf-8"))
    entries = [h["entry"] for r in cfg["repos"] for h in r["hooks"]]
    assert any("tests/test_no_tracked_credentials.py" in e for e in entries)
