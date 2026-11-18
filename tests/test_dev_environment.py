"""The dev environment is pinned and belongs to this checkout.

The virtualenv once belonged to a sibling clone, so tracebacks named files in
the wrong repository, and `addopts = -q` hid the pass/fail summary whenever the
documented command also passed -q.
"""
import configparser
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    return open(os.path.join(REPO, rel), encoding="utf-8").read()


def test_python_version_is_pinned():
    version = _read(".python-version").strip()
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?", version), version


def test_every_dev_dependency_is_pinned_exactly():
    lines = [l.strip() for l in _read("requirements-dev.txt").splitlines()
             if l.strip() and not l.strip().startswith("#")]
    assert lines, "requirements-dev.txt is empty"
    unpinned = [l for l in lines if not re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][^\s]*", l)]
    assert not unpinned, "not pinned with ==: {}".format(unpinned)


def test_pytest_config_does_not_force_quiet_mode():
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(REPO, "pytest.ini"))
    addopts = cfg.get("pytest", "addopts", fallback="")
    assert "-q" not in addopts.split(), (
        "addopts -q turns a user's own -q into -qq and hides the summary line")


def test_virtualenv_belongs_to_this_checkout():
    cfg_path = os.path.join(REPO, ".venv", "pyvenv.cfg")
    if not os.path.exists(cfg_path):
        pytest.skip("no .venv in this checkout (CI installs into its own environment)")
    command = [l for l in open(cfg_path).read().splitlines() if l.startswith("command")]
    if not command:
        pytest.skip("pyvenv.cfg records no creation command")
    target = command[0].split()[-1]
    assert os.path.realpath(target) == os.path.realpath(os.path.join(REPO, ".venv")), (
        ".venv was created for {} -- recreate it in this checkout".format(target))


def test_isolation_directories_are_gitignored():
    ignored = _read(".gitignore").splitlines()
    for entry in (".venv/", "__pycache__/"):
        assert entry in ignored, "{} missing from .gitignore".format(entry)
