"""Operational model failures must finish tasks instead of leaving them running."""

import asyncio
import json

import pytest

from core import events
from core.llm import make_reply
from core.orchestrator import run_goal


class MemoryHub:
    """Small in-memory tool stand-in; no server, network, or generated code runs."""

    def __init__(self):
        self.tasks = {}
        self.artifacts = {}
        self.messages = []

    def tool_list_for_claude(self, allowed):
        return []

    async def call(self, name, arguments=None):
        args = arguments or {}
        if name == "knowledge__list_documents":
            return {"documents": [{"id": "style-guide"}]}
        if name == "validator__check_json":
            json.loads(args["text"])
            return {"passed": True, "problems": []}
        if name == "validator__check_citations":
            passed = "[doc:style-guide]" in args["text"]
            return {"passed": passed, "problems": [] if passed else ["no citations"]}
        if name == "blackboard__add_task":
            task = {**args, "status": "open", "owner": None}
            self.tasks[args["task_id"]] = task
            return task
        if name == "blackboard__claim_task":
            task = self.tasks[args["task_id"]]
            assert all(self.tasks[dep]["status"] == "done" for dep in task["depends_on"])
            task.update(status="claimed", owner=args["agent"])
            return {"claimed": True, "task": task}
        if name == "blackboard__finish_task":
            task = self.tasks[args["task_id"]]
            assert task["owner"] == args["agent"]
            task.update(status="done" if args["success"] else "failed", result=args["result"])
            return task
        if name == "blackboard__save_artifact":
            self.artifacts[args["key"]] = args["content"]
            return args
        if name == "blackboard__post_message":
            self.messages.append(args)
            return args
        raise AssertionError(f"Unexpected tool: {name}")


class ReviewerFailureLLM:
    def __init__(self, failure, with_independent=True):
        self.failure = failure
        self.called_names = []
        self.tasks = [
            {"id": "research", "role": "researcher", "title": "Research",
             "instruction": "Find the facts.", "depends_on": [], "checks": ["citations", "judge"]},
            {"id": "dependent", "role": "writer", "title": "Dependent note",
             "instruction": "Use the research.", "depends_on": ["research"], "checks": ["judge"]},
        ]
        if with_independent:
            self.tasks.append({"id": "independent", "role": "writer", "title": "Independent note",
                               "instruction": "Write a separate note.", "depends_on": [], "checks": ["judge"]})

    async def ask(self, name, system_prompt, messages, tools):
        self.called_names.append(name)
        if name == "planner":
            return make_reply(json.dumps({"tasks": self.tasks}))
        if name == "reviewer:research":
            raise self.failure
        if name.startswith("reviewer:"):
            return make_reply('{"passed": true, "score": 0.9, "problems": []}')
        return make_reply("These are the completed findings from our document library [doc:style-guide].")


@pytest.fixture(autouse=True)
def isolated_events(monkeypatch):
    monkeypatch.setattr(events, "events", [])
    monkeypatch.setattr(events, "listeners", [])


@pytest.mark.parametrize("failure,expected_problem", [
    (RuntimeError("Groq rejected the reviewer request: unsupported tool json"), "unsupported tool json"),
    (TimeoutError("Provider request stalled"), "timed out"),
])
def test_reviewer_error_fails_claimed_task_but_independent_work_finishes(failure, expected_problem):
    hub = MemoryHub()
    llm = ReviewerFailureLLM(failure)

    report = asyncio.run(run_goal("Write two notes", llm, hub, run_id="review-error"))

    assert report["status"] == "partial"
    assert {key: task["status"] for key, task in report["tasks"].items()} == {
        "research": "failed", "independent": "done", "dependent": "skipped",
    }
    reason = report["tasks"]["research"]["error"]
    assert expected_problem in reason
    assert hub.tasks["review-error/research"]["status"] == "failed"
    assert reason in hub.tasks["review-error/research"]["result"]
    assert hub.tasks["review-error/independent"]["status"] == "done"
    assert "writer:dependent" not in llm.called_names
    assert any(reason in message["text"] for message in hub.messages)

    run_events = events.events_for("review-error")
    assert any(event["kind"] == "check" and event["check"] == "citations" and event["passed"]
               for event in run_events)
    terminal = [event for event in run_events
                if event["kind"] == "task_status" and event["task"] == "research"][-1]
    assert terminal["status"] == "failed"
    assert terminal["reason"] == reason
    skipped = [event for event in run_events
               if event["kind"] == "task_status" and event["task"] == "dependent"][-1]
    assert skipped["status"] == "skipped" and "research" in skipped["reason"]


def test_run_with_no_completed_tasks_ends_failed_after_reviewer_error():
    hub = MemoryHub()
    llm = ReviewerFailureLLM(RuntimeError("Provider unavailable"), with_independent=False)

    report = asyncio.run(run_goal("Write a note", llm, hub, run_id="all-failed"))

    assert report["status"] == "failed"
    assert report["final_passed"] is False
    assert report["final_output"] == ""
    assert "combiner" not in llm.called_names
    assert events.events_for("all-failed")[-1]["status"] == "failed"


async def test_independent_tasks_overlap_and_dependent_task_waits_for_both():
    """Refactoring the workflow must keep parallel work and dependency ordering."""
    hub = MemoryHub()
    started = {name: asyncio.Event() for name in ("left", "right")}
    tasks = [
        {"id": name, "role": "writer", "title": name, "instruction": "Write a note.",
         "depends_on": [], "checks": ["judge"]}
        for name in started
    ]
    tasks.append({
        "id": "joined", "role": "writer", "title": "Use both notes",
        "instruction": "Combine the two notes.", "depends_on": ["left", "right"],
        "checks": ["judge"],
    })

    class ParallelTeam:
        async def ask(self, name, system_prompt, messages, tools):
            if name == "planner":
                return make_reply(json.dumps({"intent": "other", "tasks": tasks}))
            if name.startswith("reviewer:"):
                return make_reply('{"passed": true, "score": 0.9, "problems": []}')
            if name in ("writer:left", "writer:right"):
                task_id = name.split(":")[1]
                started[task_id].set()
                # Neither task can finish until its partner has also started.
                other_id = "right" if task_id == "left" else "left"
                await started[other_id].wait()
            if name == "writer:joined":
                assert hub.tasks["parallel/left"]["status"] == "done"
                assert hub.tasks["parallel/right"]["status"] == "done"
            return make_reply("Both notes are ready to be used in the final response.")

    report = await asyncio.wait_for(
        run_goal("Write two independent notes, then combine them.", ParallelTeam(), hub,
                 run_id="parallel", max_tries=1),
        timeout=3,
    )
    assert report["status"] == "succeeded"
    assert all(result["status"] == "done" for result in report["tasks"].values())
