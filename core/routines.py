"""
Routines: jobs that run on a schedule (or when you click "Run now").

A routine is a YAML file in the routines/ folder with a list of steps.
Each step does ONE of these:
    tool:  call an MCP tool               e.g. knowledge__list_documents
    agent: ask an agent (with checks)     e.g. a writer with citations + length checks
    goal:  run the whole team on a goal   (the full orchestrator)

Steps can use earlier results with {{steps.<id>.output}} and inputs with
{{inputs.<name>}}. Other options: only_if, retries, must_pass, keep_going.

The scheduler wakes up every 20 seconds and starts any routine that's due.
It never starts a routine that is still running from last time.
"""

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from core.agent import run_agent
from core.events import log
from core.orchestrator import run_goal, work_with_checks

ROUTINES_FOLDER = Path(__file__).parent.parent / "routines"
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

history = []  # results of past routine runs, newest last


def load_routines(folder=ROUTINES_FOLDER):
    routines = {}
    for path in sorted(Path(folder).glob("*.yaml")):
        routine = yaml.safe_load(path.read_text())
        check_routine(routine)
        routines[routine["name"]] = routine
    return routines


def check_routine(routine):
    """Catch mistakes in a routine file when it's loaded, not halfway through a run."""
    earlier_steps = []
    for step in routine["steps"]:
        kinds = [kind for kind in ("tool", "agent", "goal") if kind in step]
        if len(kinds) != 1:
            raise ValueError(f"step {step['id']} must have exactly one of tool / agent / goal")
        references = re.findall(r"steps\.(\w+)", json.dumps(step))
        for reference in references:
            if reference not in earlier_steps:
                raise ValueError(f"step {step['id']} uses steps.{reference}, which doesn't come before it")
        earlier_steps.append(step["id"])


# ------------------------------------------------------------ templates

def fill(value, context):
    """Replace {{a.b.c}} with values from context, inside strings, lists and dicts."""
    if isinstance(value, dict):
        return {key: fill(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [fill(item, context) for item in value]
    if not isinstance(value, str):
        return value

    def lookup(match):
        current = context
        for part in match.group(1).split("."):
            current = current[part]  # a KeyError here means the template has a typo
        if isinstance(current, str):
            return current
        return json.dumps(current)

    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", lookup, value)


# ------------------------------------------------------------ running

def step_is_enabled(step, context):
    """An only_if template can skip a step without failing the routine."""
    if "only_if" not in step:
        return True
    condition = fill(step["only_if"], context).strip().lower()
    return condition not in ("", "false", "null", "none", "[]", "0")


async def run_routine(routine, llm, hub, inputs=None, run_id=None, trigger="manual"):
    run_id = run_id or f"{routine['name']}-{uuid.uuid4().hex[:6]}"
    record = {
        "run_id": run_id,
        "routine": routine["name"],
        "trigger": trigger,
        "status": "running",
        "started": time.time(),
        "steps": [],
    }
    history.append(record)
    routine_inputs = dict(routine.get("inputs", {}))
    routine_inputs.update(inputs or {})
    context = {"inputs": routine_inputs, "steps": {}}
    log(run_id, "routine_started", routine["name"], trigger=trigger)

    stopped = False
    for step in routine["steps"]:
        result = {
            "id": step["id"], "status": "skipped", "output": "",
            "passed": None, "error": None, "tries": 0,
        }
        if stopped:
            result["error"] = "an earlier step failed"
        elif not step_is_enabled(step, context):
            result["error"] = f"only_if was false: {step['only_if']}"
        else:
            await run_step_with_retries(step, context, result, llm, hub, run_id)
            if result["status"] == "failed" and not step.get("keep_going", False):
                stopped = True
        record["steps"].append(result)
        # Later templates can read the result, even when this step was skipped.
        context["steps"][step["id"]] = {
            "output": result["output"], "passed": result["passed"], "data": result.get("data"),
        }
        log(run_id, "routine_step", routine["name"], step=step["id"], status=result["status"], error=result["error"])

    record["status"] = "failed" if stopped else "succeeded"
    log(run_id, "routine_finished", routine["name"], status=record["status"])
    return record


async def run_step_with_retries(step, context, result, llm, hub, run_id):
    max_attempts = 1 + step.get("retries", 0)
    for attempt in range(1, max_attempts + 1):
        result["tries"] = attempt
        try:
            await run_step(step, context, result, llm, hub, run_id)
            result["status"] = "ok"
            result["error"] = None
            return
        except Exception as error:   # any failure: record it and maybe try again
            result["status"] = "failed"
            result["error"] = str(error)
            if attempt < max_attempts:
                await asyncio.sleep(attempt)  # wait 1s, then 2s, ... between tries


async def run_step(step, context, result, llm, hub, run_id):
    """Choose the one action described by this YAML step."""
    if "tool" in step:
        await run_tool_step(step, context, result, hub)
    elif "goal" in step:
        await run_goal_step(step, context, result, llm, hub, run_id)
    else:
        await run_agent_step(step, context, result, llm, hub, run_id)


async def run_tool_step(step, context, result, hub):
    arguments = fill(step.get("args", {}), context)
    data = await hub.call(step["tool"], arguments)
    if "error" in data:
        raise RuntimeError(data["error"])

    result["data"] = data
    result["output"] = json.dumps(data)
    if "passed" in data:  # A validator tool can act as a quality gate.
        result["passed"] = data["passed"]
        if step.get("must_pass") and not data["passed"]:
            raise RuntimeError(f"gate failed: {data['problems']}")


async def run_goal_step(step, context, result, llm, hub, run_id):
    goal = fill(step["goal"], context)
    goal_run_id = f"{run_id}-{step['id']}"
    report = await run_goal(goal, llm, hub, run_id=goal_run_id)
    result["output"] = report["final_output"]
    result["passed"] = report["status"] == "succeeded"
    result["data"] = {"status": report["status"]}
    if report["status"] == "failed":
        raise RuntimeError("the team could not finish the goal")


async def run_agent_step(step, context, result, llm, hub, run_id):
    prompt = fill(step["prompt"], context)
    role = step["agent"]
    name = f"{role}:{step['id']}"
    checks = step.get("checks", [])
    if not checks:
        result["output"], _ = await run_agent(role, name, prompt, llm, hub, run_id)
        return

    docs = await hub.call("knowledge__list_documents")
    known_ids = [document["id"] for document in docs.get("documents", [])]
    task = {
        "id": step["id"], "title": step["id"], "instruction": prompt,
        "max_words": step.get("max_words", 400),
    }
    outcome = await work_with_checks(role, name, prompt, checks, task, llm, hub, run_id, known_ids)
    result["output"] = outcome["output"]
    result["passed"] = outcome["passed"]
    if not outcome["passed"]:
        raise RuntimeError(f"checks still failing after {len(outcome['attempts'])} tries")


# ------------------------------------------------------------ scheduling

def next_run_time(schedule, after):
    """When should this schedule run next, after the given (timezone-aware) time?"""
    if "every_minutes" in schedule:
        return after + timedelta(minutes=schedule["every_minutes"])

    zone = ZoneInfo(schedule.get("timezone", "UTC"))
    local_now = after.astimezone(zone)
    hour, minute = map(int, schedule["daily_at"].split(":"))
    days = schedule.get("days", DAY_NAMES)

    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    while DAY_NAMES[candidate.weekday()] not in days:
        candidate += timedelta(days=1)
    return candidate


class Scheduler:
    def __init__(self, routines, llm, hub):
        self.routines = routines
        self.llm = llm
        self.hub = hub
        self.next_runs = {}   # routine name -> when it should run next
        self.running = set()  # routines that haven't finished yet
        self.jobs = set()     # keep a reference to running jobs so Python doesn't garbage-collect them
        self.task = None

    async def check_due(self, now=None):
        """Start every routine whose time has come. Returns the names started."""
        now = now or datetime.now(timezone.utc)
        started = []
        for name, routine in self.routines.items():
            if "schedule" not in routine:
                continue
            if name not in self.next_runs:
                self.next_runs[name] = next_run_time(routine["schedule"], now)
            if now < self.next_runs[name]:
                continue
            self.next_runs[name] = next_run_time(routine["schedule"], now)
            if name in self.running:
                log("-", "routine_skipped", name, reason="the last run is still going")
                continue
            self.running.add(name)
            job = asyncio.create_task(self._run(routine))
            self.jobs.add(job)
            job.add_done_callback(self.jobs.discard)
            started.append(name)
        return started

    async def _run(self, routine):
        try:
            await run_routine(routine, self.llm, self.hub, trigger="schedule")
        finally:
            self.running.discard(routine["name"])

    async def loop(self):
        while True:
            await self.check_due()
            await asyncio.sleep(20)

    def start(self):
        self.task = asyncio.create_task(self.loop())

    def stop(self):
        if self.task:
            self.task.cancel()
