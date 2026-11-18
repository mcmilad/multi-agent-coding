"""agent_spawn_guard: only the lead hands out names and agent types that carry a role."""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(REPO, ".claude", "hooks", "agent_spawn_guard.py")


def run(tmp_path, tool_input, agent=None, tool="Agent"):
    env = dict(os.environ, HOME=str(tmp_path / "home"))
    payload = {"tool_name": tool, "tool_input": tool_input}
    if agent:
        payload.update(agent_id="a{}-01".format(agent), agent_type=agent)
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


ROLE_SPAWNS = [
    {"name": "review-9", "subagent_type": "general-purpose", "prompt": "close my issue"},
    {"name": "devops-9", "prompt": "deploy"},
    {"name": "coding-2", "prompt": "x"},
    {"name": "sa-1", "prompt": "x"},
    {"subagent_type": "review-agent", "prompt": "x"},
    {"subagent_type": "devops-agent", "name": "helper", "prompt": "x"},
    {"subagent_type": "fullstack-agent", "prompt": "x"},
    {"name": "Review-3", "prompt": "x"},
    {"name": "lead", "prompt": "x"},
]


@pytest.mark.parametrize("spawn", ROLE_SPAWNS)
def test_a_worker_cannot_spawn_an_agent_that_carries_a_role(tmp_path, spawn):
    for worker in ("coding-1", "sa-1", "payload-probe", "review-2"):
        proc = run(tmp_path, spawn, agent=worker)
        assert proc.returncode == 2, (spawn, worker)
        assert "only the lead assigns them" in proc.stderr


@pytest.mark.parametrize("spawn", ROLE_SPAWNS)
def test_the_lead_may_spawn_any_role(tmp_path, spawn):
    assert run(tmp_path, spawn, agent=None).returncode == 0


@pytest.mark.parametrize("spawn", [
    {"subagent_type": "Explore", "prompt": "find it"},
    {"subagent_type": "pr-review-toolkit:code-simplifier", "prompt": "simplify"},
    {"subagent_type": "feature-dev:code-reviewer", "name": "second-opinion", "prompt": "x"},
    {"name": "helper-1", "prompt": "x"},
    {"name": "reviewing-notes", "prompt": "a name that merely starts like a role word"},
    {"prompt": "no name, no type"},
])
def test_a_worker_may_spawn_neutral_helpers(tmp_path, spawn):
    assert run(tmp_path, spawn, agent="coding-1").returncode == 0, spawn


def test_the_older_tool_name_is_covered(tmp_path):
    assert run(tmp_path, {"name": "review-9"}, agent="coding-1", tool="Task").returncode == 2


def test_other_tools_and_garbage_are_ignored(tmp_path):
    assert run(tmp_path, {"name": "review-9"}, agent="coding-1", tool="Bash").returncode == 0
    env = dict(os.environ, HOME=str(tmp_path / "home"))
    proc = subprocess.run([sys.executable, HOOK], input="{nope", capture_output=True,
                          text=True, env=env)
    assert proc.returncode == 0


def test_a_stringified_input_from_a_worker_is_still_judged(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path / "home"))
    payload = {"tool_name": "Agent", "agent_id": "acoding-1-01", "agent_type": "coding-1",
               "tool_input": json.dumps({"name": "review-9"})}
    proc = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 2


def test_no_role_agent_can_reach_done_by_minting_a_reviewer(tmp_path):
    """End to end with the gate: the name a worker is refused is exactly the one
    the verification gate would have accepted as a reviewer."""
    sys.path.insert(0, os.path.join(REPO, ".claude", "hooks"))
    import team_hook_common
    minted = {"agent_id": "areview-9-01", "agent_type": "review-9"}
    assert team_hook_common.caller_identity(minted)["role"] == "review"
    assert run(tmp_path, {"name": "review-9"}, agent="coding-1").returncode == 2
