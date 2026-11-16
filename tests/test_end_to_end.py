"""Run the real thing: MCP servers as separate processes, the offline team, the whole pipeline."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from core import fake_team
from core.llm import FakeLLM
from core.orchestrator import run_goal
from core.routines import Scheduler, load_routines, run_routine
from core.tool_hub import ToolHub

GOAL = "Build a slugify() utility that follows our URL style guide, with tests and a usage note."


async def test_the_team_catches_and_fixes_every_mistake():
    async with ToolHub() as hub:
        report = await run_goal(GOAL, FakeLLM(), hub, run_id="t1")
        board = await hub.call("blackboard__list_tasks")

    assert report["status"] == "succeeded" and report["final_passed"]
    tasks = report["tasks"]
    # the coder's first try failed the real pytest run, the second passed
    assert [a["passed"] for a in tasks["impl"]["attempts"]] == [False, True]
    assert tasks["impl"]["attempts"][0]["results"][-1]["check"] == "tests"
    # the writer's first try was too long
    assert "too long" in tasks["docs"]["attempts"][0]["results"][-1]["problems"][0]
    # the work really went through the blackboard
    assert {t["id"]: t["status"] for t in board["tasks"]} == {"t1/research": "done", "t1/impl": "done", "t1/docs": "done"}
    assert "def slugify" in report["final_output"]


async def test_a_task_that_keeps_failing_skips_the_tasks_after_it(monkeypatch):
    monkeypatch.setattr(fake_team, "coder", lambda messages: "Sorry, I couldn't write any code this time.")
    async with ToolHub() as hub:
        report = await run_goal(GOAL, FakeLLM(), hub, run_id="t2", max_tries=2)
        alerts = await hub.call("blackboard__read_messages", {"channel": "escalations"})

    statuses = {tid: t["status"] for tid, t in report["tasks"].items()}
    assert statuses == {"research": "done", "impl": "failed", "docs": "skipped"}
    assert report["status"] == "partial"
    assert "t2/impl" in alerts["messages"][0]["text"]  # a human gets told


async def test_routines():
    routines = load_routines()
    async with ToolHub() as hub:
        audit = await run_routine(routines["knowledge-audit"], FakeLLM(), hub, inputs={"channel": "test"})
        posted = await hub.call("blackboard__read_messages", {"channel": "test"})
        digest = await run_routine(routines["failed-task-digest"], FakeLLM(), hub)

    assert audit["status"] == "succeeded" and audit["steps"][1]["passed"]
    assert "[doc:style-guide]" in posted["messages"][0]["text"]
    assert digest["steps"][1]["status"] == "skipped"  # no failed tasks, so nobody is told


async def test_scheduler_runs_due_routines_but_never_twice_at_once():
    routine = {"name": "quick", "schedule": {"every_minutes": 1},
               "steps": [{"id": "look", "tool": "knowledge__list_documents"}]}
    start = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    async with ToolHub() as hub:
        scheduler = Scheduler({"quick": routine}, FakeLLM(), hub)
        assert await scheduler.check_due(start) == []                          # first call only works out the next time
        scheduler.running.add("quick")                                         # pretend the last run is still going
        assert await scheduler.check_due(start + timedelta(minutes=1)) == []   # due, but skipped
        scheduler.running.clear()
        assert await scheduler.check_due(start + timedelta(minutes=2)) == ["quick"]
        await asyncio.sleep(0.5)                                               # let it finish before we shut down


def test_web_api(monkeypatch):
    from web import api

    monkeypatch.setattr(api, "pick_llm", lambda: (FakeLLM(), "offline demo team"))
    monkeypatch.setitem(api.app_state, "scheduler_on", False)
    with TestClient(api.app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/health").json()["tools"] == 17
        run_id = client.post("/api/runs", json={"goal": GOAL}).json()["run_id"]
        for _ in range(100):
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] != "running":
                break
            time.sleep(0.1)
        assert run["status"] == "succeeded"
        kinds = {e["kind"] for e in client.get(f"/api/runs/{run_id}/events").json()["events"]}
        assert {"tool_call", "check", "task_status", "final"} <= kinds
        assert client.post("/api/runs", json={"goal": "x"}).status_code == 422
        assert {r["name"] for r in client.get("/api/routines").json()} == {"knowledge-audit", "failed-task-digest", "weekly-self-test"}
