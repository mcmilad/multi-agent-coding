"""Suite-wide test isolation.

Coordination state (mirror, claim locks, sentinels, run locks) lives in the
repository's git directory by default. Tests must never write there: every test
that runs a hook gives it a throwaway HOME, so pointing the state root at
`~/.claude/logs` (expanded with that HOME by the hook itself) keeps each test's
state inside its own tmp_path. Tests that exercise the default resolution unset
this variable explicitly.
"""
import os

os.environ["CLAUDE_HARNESS_STATE_DIR"] = "~/.claude/logs"

# Hooks resolve the project (tracked gate configuration, the working tree whose
# files evidence is bound to) from CLAUDE_PROJECT_DIR. Pin it to this checkout so
# the suite behaves the same from any working directory, and point the gates file
# at nothing: a gate that runs this suite from inside this suite would recurse.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CLAUDE_PROJECT_DIR"] = _REPO
os.environ["CLAUDE_HARNESS_GATES_PATH"] = os.path.join(_REPO, ".no-gates-in-tests.json")
os.environ.pop("CLAUDE_HARNESS_IN_GATE", None)
