"""
The web server: a small REST API plus the dashboard page.

    GET  /                          the dashboard
    POST /api/runs  {"goal": ...}   start the team on a goal (runs in the background)
    GET  /api/runs                  list runs
    GET  /api/runs/{id}             a run's status and final report
    GET  /api/runs/{id}/events      what has happened so far (the dashboard polls this)
    GET  /api/routines              routines and when they'll run next
    POST /api/routines/{name}/run   run a routine right now
    GET  /api/routine-runs/{id}     result of a routine run
    GET  /api/channels/{name}       messages on a blackboard channel
    GET  /api/tools                 every MCP tool the agents can use
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core import events, routines
from core.llm import pick_llm
from core.orchestrator import run_goal
from core.tool_hub import ToolHub

app_state = {}  # Shared services, filled in when the server starts.
runs = {}       # Run ID -> goal, status, and final report.
background = set()  # Keep background tasks alive until they finish.


@asynccontextmanager
async def startup_and_shutdown(app):
    hub = ToolHub()
    await hub.start()
    llm, model_name = pick_llm()
    all_routines = routines.load_routines()
    scheduler = routines.Scheduler(all_routines, llm, hub)
    if app_state.get("scheduler_on", True):
        scheduler.start()
    app_state.update(hub=hub, llm=llm, model=model_name, routines=all_routines, scheduler=scheduler)
    yield
    scheduler.stop()
    await hub.stop()


app = FastAPI(title="agentmesh", lifespan=startup_and_shutdown)


def in_background(coroutine):
    """Start work without making the HTTP request wait for it to finish."""
    job = asyncio.create_task(coroutine)
    background.add(job)
    job.add_done_callback(background.discard)


@app.get("/")
async def dashboard():
    return FileResponse(Path(__file__).parent / "dashboard.html")


@app.get("/api/health")
async def health():
    hub = app_state["hub"]
    return {
        "ok": True,
        "model": app_state["model"],
        "servers": sorted(hub.clients),
        "tools": len(hub.tools),
    }


# ------------------------------------------------------------ runs

class NewRun(BaseModel):
    goal: str


async def finish_run(run_id, goal):
    """Save either the team's report or the error that stopped the run."""
    try:
        report = await run_goal(goal, app_state["llm"], app_state["hub"], run_id)
        runs[run_id].update(status=report["status"], report=report)
    except Exception as error:
        reason = str(error)
        runs[run_id].update(status="error", error=reason)
        events.log(run_id, "run_status", "orchestrator", status="failed", reason=reason)


@app.post("/api/runs", status_code=202)
async def start_run(body: NewRun):
    if len(body.goal.strip()) < 5:
        raise HTTPException(422, "the goal is too short")

    run_id = "run-" + uuid.uuid4().hex[:6]
    runs[run_id] = {
        "run_id": run_id,
        "goal": body.goal,
        "status": "running",
        "report": None,
    }
    in_background(finish_run(run_id, body.goal))
    return {"run_id": run_id}


@app.get("/api/runs")
async def list_runs():
    summaries = []
    for run in reversed(runs.values()):
        summaries.append({
            "run_id": run["run_id"],
            "goal": run["goal"],
            "status": run["status"],
        })
    return summaries


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    if run_id not in runs:
        raise HTTPException(404, "no run with that id")
    return runs[run_id]


@app.get("/api/runs/{run_id}/events")
async def get_events(run_id: str, after: int = 0):
    """Events for one run. Pass after=N to only get the ones you haven't seen yet."""
    run_events = events.events_for(run_id)
    return {"events": run_events[after:], "total": len(run_events)}


# ------------------------------------------------------------ routines

@app.get("/api/routines")
async def list_routines():
    scheduler = app_state["scheduler"]
    summaries = []
    for name, routine in app_state["routines"].items():
        next_run = scheduler.next_runs.get(name)
        summaries.append({
            "name": name,
            "description": routine.get("description", ""),
            "schedule": routine.get("schedule"),
            "next_run": next_run.isoformat() if next_run else None,
            "steps": [step["id"] for step in routine["steps"]],
        })
    return summaries


@app.post("/api/routines/{name}/run", status_code=202)
async def run_routine_now(name: str):
    if name not in app_state["routines"]:
        raise HTTPException(404, "no routine with that name")

    run_id = f"{name}-{uuid.uuid4().hex[:6]}"
    routine = app_state["routines"][name]
    in_background(routines.run_routine(
        routine, app_state["llm"], app_state["hub"], run_id=run_id
    ))
    return {"run_id": run_id}


@app.get("/api/routine-runs/{run_id}")
async def get_routine_run(run_id: str):
    for record in routines.history:
        if record["run_id"] == run_id:
            return record
    return {"run_id": run_id, "status": "running", "steps": []}


# ------------------------------------------------------------ blackboard + tools

@app.get("/api/channels/{channel}")
async def read_channel(channel: str):
    return await app_state["hub"].call("blackboard__read_messages", {"channel": channel})


@app.get("/api/tools")
async def list_tools():
    summaries = []
    for name, tool in sorted(app_state["hub"].tools.items()):
        summaries.append({"name": name, "description": tool.description or ""})
    return summaries
