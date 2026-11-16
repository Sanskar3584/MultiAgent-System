"""
The orchestrator: takes a goal and gets the team to finish it.

    1. PLAN     the planner splits the goal into tasks (the plan itself is checked too)
    2. POST     the tasks go onto the blackboard with their dependencies
    3. WORK     tasks run in "waves": everything whose dependencies are done runs
                at the same time; each task goes through the verification loop
    4. COMBINE  the combiner joins the finished outputs into one final answer,
                which is checked one last time

If a task fails all its tries, the tasks that depend on it are skipped, the
others still finish, and the run is marked "partial" instead of crashing.
"""

import asyncio
import time
import uuid

from core.agent import run_agent
from core.checks import citation_policy, first_json_object, verify_loop
from core.events import log

# Which checks each kind of task gets if the planner doesn't say.
DEFAULT_CHECKS = {
    "researcher": ["citations", "judge"],
    "coder": ["safety", "tests", "judge"],
    "writer": ["length", "judge"],
}


async def work_with_checks(role, name, prompt, checks, task, llm, hub, run_id, known_ids, max_tries=3):
    """Run an agent inside the verification loop. When a check fails, the
    feedback is added to the SAME conversation, so the agent remembers what it
    did and only has to fix the problems."""
    conversation = []
    task = {**task, "requires_citations": "citations" in checks}
    if role in ("writer", "researcher", "combiner"):
        prompt = f"{citation_policy(task)}\n\n{prompt}"

    async def produce(feedback):
        nonlocal conversation
        message = prompt if feedback is None else feedback
        answer, conversation = await run_agent(role, name, message, llm, hub, run_id, history=conversation)
        return answer

    return await verify_loop(name, produce, checks, task, hub, llm, run_id, known_ids, max_tries)


def make_waves(tasks):
    """Group tasks so each group only depends on earlier groups.
    e.g. research -> impl -> docs gives [[research], [impl], [docs]]."""
    waves = []
    placed = set()
    while len(placed) < len(tasks):
        wave = []
        for task in tasks:
            if task["id"] in placed:
                continue
            dependencies_ready = all(dep in placed for dep in task.get("depends_on", []))
            if dependencies_ready:
                wave.append(task)
        waves.append(wave)
        placed.update(task["id"] for task in wave)
    return waves


def normalize_plan(plan, goal):
    """Apply the planner's semantic intent consistently across the workflow.

    Merely having documents in the library does not make writing source-based.
    Source use follows actual planned dependencies instead of topic keywords.
    """
    if plan.get("intent") == "general_writing":
        proposed = {}
        for task in plan["tasks"]:
            if task["role"] == "writer":
                proposed = task
                break
        return [{
            "id": proposed.get("id", "write"), "role": "writer", "title": "Write the response",
            "instruction": goal, "depends_on": [], "checks": ["length", "judge"],
            "min_words": proposed.get("min_words", 1), "max_words": proposed.get("max_words", 400),
            "uses_sources": False, "requires_citations": False,
        }]

    tasks = []
    for proposed in plan["tasks"]:
        task = proposed.copy()
        task["depends_on"] = list(proposed.get("depends_on", []))
        task["checks"] = list(proposed.get("checks") or DEFAULT_CHECKS[task["role"]])
        tasks.append(task)

    by_id = {task["id"]: task for task in tasks}
    for wave in make_waves(tasks):
        for task in wave:
            # A usage note can depend on research indirectly, through a coding task.
            depends_on_sources = any(by_id[dep]["uses_sources"] for dep in task["depends_on"])
            sources_requested = plan.get("intent") == "sourced_writing" and task["role"] == "writer"
            task["uses_sources"] = bool(
                task.get("uses_sources")
                or task["role"] == "researcher"
                or "citations" in task["checks"]
                or sources_requested
                or depends_on_sources
            )
            if task["uses_sources"] and task["role"] in ("researcher", "writer"):
                if "citations" not in task["checks"]:
                    task["checks"].insert(0, "citations")
            task["requires_citations"] = "citations" in task["checks"]
    return tasks


def task_prompt(task, goal, intent, results, run_id, known_ids):
    """Give an agent its instructions and the earlier results it needs."""
    prompt = f"Goal: {goal}\nWriting intent: {intent}\n\nYour task: {task['title']}\n{task['instruction']}\n"
    if intent == "general_writing":
        prompt += "Use general knowledge. Answer the goal directly in its requested format; no document lookup is needed.\n"
    if task.get("max_words"):
        prompt += f"Word limit: {task['max_words']}\n"

    for dependency in task["depends_on"]:
        previous_output = results[dependency]["output"][:6000]
        prompt += f"\nOutput of task '{dependency}' (blackboard key: {run_id}/{dependency}):\n{previous_output}\n"

    if task["requires_citations"]:
        document_ids = ", ".join(known_ids) or "(none)"
        prompt += (
            "\nAvailable document IDs (cite only relevant documents you have read): "
            f"{document_ids}. Knowledge tools search local documents, not the web."
        )
    prompt += f"\nYour work will be checked by: {', '.join(task['checks'])}"
    return prompt


def failure_reason(attempts):
    """Show the problems from the last attempt, if any checks were run."""
    problems = []
    if attempts:
        for result in attempts[-1]["results"]:
            if not result["passed"]:
                problems.extend(result.get("problems", []))
    return "; ".join(problems) or "the task did not pass its checks"


async def run_task(task, goal, intent, results, llm, hub, run_id, known_ids, max_tries):
    """Complete one task and record its result. Earlier waves are already done."""
    task_id = task["id"]
    board_key = f"{run_id}/{task_id}"
    name = f"{task['role']}:{task_id}"
    failed_dependencies = []
    for dependency in task["depends_on"]:
        if results[dependency]["status"] != "done":
            failed_dependencies.append(dependency)

    if failed_dependencies:
        reason = f"skipped because {failed_dependencies} failed"
        log(run_id, "task_status", "orchestrator", task=task_id, status="skipped", reason=reason)
        results[task_id] = {
            "title": task["title"], "role": task["role"], "status": "skipped",
            "output": reason, "error": reason, "attempts": [],
        }
        return

    await hub.call("blackboard__claim_task", {"agent": name, "task_id": board_key})
    log(run_id, "task_status", name, task=task_id, status="running")
    prompt = task_prompt(task, goal, intent, results, run_id, known_ids)

    reason = None
    try:
        outcome = await work_with_checks(
            task["role"], name, prompt, task["checks"], task,
            llm, hub, run_id, known_ids, max_tries,
        )
    except Exception as error:
        # A broken API request fails this task; independent tasks can still finish.
        reason = str(error).strip() or type(error).__name__
        outcome = {"passed": False, "output": f"Task could not complete: {reason}", "attempts": []}

    status = "done" if outcome["passed"] else "failed"
    if status == "failed" and not reason:
        reason = failure_reason(outcome["attempts"])

    await hub.call("blackboard__save_artifact", {
        "key": board_key, "content": outcome["output"], "author": name,
    })
    await hub.call("blackboard__finish_task", {
        "task_id": board_key, "agent": name,
        "result": outcome["output"][:2000], "success": outcome["passed"],
    })
    if not outcome["passed"]:
        await hub.call("blackboard__post_message", {
            "channel": "escalations", "sender": name,
            "text": f"Task {board_key} failed: {reason}. A human should look at it.",
        })

    log(run_id, "task_status", name, task=task_id, status=status,
        attempts=len(outcome["attempts"]), reason=reason)
    results[task_id] = {
        "title": task["title"], "role": task["role"], "status": status,
        "output": outcome["output"], "attempts": summarize(outcome["attempts"]),
    }
    if reason:
        results[task_id]["error"] = reason


async def combine_results(goal, intent, tasks, results, llm, hub, run_id, known_ids, max_tries):
    """Combine only work that passed, then check and save the final answer."""
    finished = {}
    unfinished = []
    for task_id, result in results.items():
        if result["status"] == "done":
            finished[task_id] = result
        else:
            unfinished.append(task_id)
    if not finished:
        return "", False

    sections = []
    for result in finished.values():
        sections.append(f"## {result['title']} ({result['role']})\n{result['output']}")
    combined = "\n\n".join(sections)
    prompt = f"Goal: {goal}\nWriting intent: {intent}\n"
    if intent == "general_writing":
        prompt += "Return the checked writing in the user's requested format, without a summary or task headings.\n"
    prompt += f"\nFINISHED TASKS:\n{combined}"
    if unfinished:
        prompt += f"\n\nThese tasks did not finish, list them as open items: {unfinished}"

    checks = ["judge"]
    for task in tasks:
        if task["id"] in finished and task["requires_citations"]:
            checks.insert(0, "citations")
            break
    final_task = {
        "id": "final", "title": "Combine the results",
        "instruction": f"Combine the finished work for: {goal}",
    }
    if intent == "general_writing":
        # The final formatting must still respect the writer's word limits.
        checks = ["length", "judge"]
        final_task.update(
            instruction=goal, min_words=tasks[0]["min_words"], max_words=tasks[0]["max_words"],
        )

    final = await work_with_checks(
        "combiner", "combiner", prompt, checks, final_task,
        llm, hub, run_id, known_ids, max_tries,
    )
    await hub.call("blackboard__save_artifact", {
        "key": f"{run_id}/final", "content": final["output"], "author": "combiner",
    })
    log(run_id, "final", "combiner", output=final["output"], passed=final["passed"])
    return final["output"], final["passed"]


async def run_goal(goal, llm, hub, run_id=None, max_tries=3):
    """Main workflow: plan, post tasks, run each wave, and combine the results."""
    run_id = run_id or "run-" + uuid.uuid4().hex[:6]
    started = time.time()
    log(run_id, "run_status", "orchestrator", status="planning", goal=goal)
    documents = await hub.call("knowledge__list_documents")
    known_ids = [document["id"] for document in documents.get("documents", [])]

    # 1. Make a plan and check it before starting any work.
    plan_task = {"id": "plan", "title": "Make a plan", "instruction": goal}
    planning = await work_with_checks(
        "planner", "planner", f"Goal: {goal}", ["plan_format", "plan_order"],
        plan_task, llm, hub, run_id, known_ids, max_tries,
    )
    if not planning["passed"]:
        log(run_id, "run_status", "orchestrator", status="failed", reason="could not make a valid plan")
        return {
            "run_id": run_id, "goal": goal, "status": "failed", "plan": None, "tasks": {},
            "final_output": "", "final_passed": False, "seconds": round(time.time() - started, 2),
        }

    plan = first_json_object(planning["output"])
    intent = plan.get("intent", "other")
    tasks = normalize_plan(plan, goal)
    visible_plan = []
    for task in tasks:
        visible_plan.append({
            "id": task["id"], "role": task["role"], "title": task["title"],
            "depends_on": task["depends_on"], "checks": task["checks"],
        })
    log(run_id, "run_status", "orchestrator", status="working", plan=visible_plan)

    # 2. Post dependencies before the tasks that use them.
    waves = make_waves(tasks)
    for wave in waves:
        for task in wave:
            await hub.call("blackboard__add_task", {
                "task_id": f"{run_id}/{task['id']}", "title": task["title"],
                "description": task["instruction"], "role": task["role"],
                "depends_on": [f"{run_id}/{dep}" for dep in task["depends_on"]],
            })

    # 3. Tasks within a wave can run together. The next wave waits for all of them.
    results = {}
    for wave in waves:
        jobs = []
        for task in wave:
            jobs.append(run_task(task, goal, intent, results, llm, hub, run_id, known_ids, max_tries))
        await asyncio.gather(*jobs)

    # 4. Check the final answer and report whether all or only some work passed.
    final_output, final_passed = await combine_results(
        goal, intent, tasks, results, llm, hub, run_id, known_ids, max_tries,
    )
    completed_count = sum(result["status"] == "done" for result in results.values())
    if completed_count == len(results) and final_passed:
        status = "succeeded"
    elif completed_count > 0:
        status = "partial"
    else:
        status = "failed"
    log(run_id, "run_status", "orchestrator", status=status)
    return {
        "run_id": run_id, "goal": goal, "status": status, "plan": tasks, "tasks": results,
        "final_output": final_output, "final_passed": final_passed,
        "seconds": round(time.time() - started, 2),
    }


def summarize(attempts):
    """Keep what each attempt's checks said, without repeating the whole output."""
    return [
        {"attempt": attempt["attempt"], "passed": attempt["passed"], "results": attempt["results"]}
        for attempt in attempts
    ]
