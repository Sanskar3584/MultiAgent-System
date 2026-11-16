"""
Checks and the verification loop.

The rule of this project: no agent's work is accepted just because the agent
says it's done. Every output goes through checks, and if a check fails the
agent gets the exact problems back and has to try again.

    attempt 1 -> run checks -> failed? -> send the problems back as feedback
    attempt 2 -> run checks -> passed  -> accept the output

Checks run cheapest first (running tests, counting words), and the expensive
one, an AI reviewer, runs last. We stop at the first failure, so we never pay
for a review of code whose tests don't even pass.
"""

import json
import re

from core.agent import run_agent
from core.events import log
from core.roles import PLAN_SCHEMA

FEEDBACK_START = "Some checks failed"

# The order checks run in: cheap and automatic first, the AI reviewer last.
CHECK_ORDER = ["plan_format", "plan_order", "safety", "tests", "citations", "length", "judge"]


def citation_policy(task):
    """Keep authors and reviewers on the same source requirement."""
    default_policy = "citations" in task.get("checks", [])
    requires_citations = task.get("requires_citations", default_policy)
    if requires_citations:
        return (
            "Citation policy: required. Ground document claims in relevant sources you have read "
            "and cite their actual document IDs in the required format. Do not invent sources or use unrelated documents "
            "to satisfy this check. If the needed evidence is unavailable, state that limitation."
        )
    return (
        "Citation policy: optional. General knowledge and creative writing are allowed without "
        "citations. Do not require sources or external research just because the text contains "
        "factual statements. Do not invent document citations. Check relevance, accuracy, clarity "
        "and the user's requested format."
    )


def get_code_files(text):
    """Pull the ```python file=NAME``` code blocks out of an agent's answer."""
    blocks = re.findall(r"```python file=(\S+)\n(.*?)```", text, re.DOTALL)
    return dict(blocks)


def first_json_object(text):
    """Find the first {...} in some text and parse it (models sometimes add words around their JSON)."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    return json.loads(match.group(0))


def plan_order_problems(plan):
    """Every dependency must exist, and tasks must not wait on each other in a circle."""
    ids = [task["id"] for task in plan["tasks"]]
    problems = []
    for task in plan["tasks"]:
        for dependency in task.get("depends_on", []):
            if dependency not in ids:
                problems.append(f"task {task['id']} depends on unknown task {dependency}")
    if problems:
        return problems
    # keep removing tasks whose dependencies are all removed; anything left over is a circle
    waiting = {task["id"]: set(task.get("depends_on", [])) for task in plan["tasks"]}
    while True:
        ready = [task_id for task_id, dependencies in waiting.items() if not dependencies]
        if not ready:
            break
        for task_id in ready:
            del waiting[task_id]
        for dependencies in waiting.values():
            dependencies.difference_update(ready)
    if waiting:
        return [f"tasks {sorted(waiting)} depend on each other in a circle"]
    return []


async def run_check(name, output, task, hub, llm, run_id, known_ids):
    """Run one check on an output. Returns {"check", "passed", "problems"}."""
    if name == "plan_format":
        result = await hub.call("validator__check_json", {"text": output, "schema": PLAN_SCHEMA})
    elif name == "plan_order":
        result = {"problems": plan_order_problems(first_json_object(output))}
    elif name == "tests":
        files = get_code_files(output)
        if "test_solution.py" not in files:
            result = {"problems": ["no test file: reply with ```python file=solution.py``` and ```python file=test_solution.py``` blocks"]}
        else:
            result = await hub.call("validator__run_tests", {"files": files})
    elif name == "safety":
        code = get_code_files(output).get("solution.py")
        if code:
            result = await hub.call("validator__check_code_safety", {"code": code})
        else:
            result = {"problems": ["no solution.py block found"]}
    elif name == "citations":
        result = await hub.call("validator__check_citations", {"text": output, "known_ids": known_ids})
    elif name == "length":
        result = await hub.call("validator__check_length", {
            "text": output,
            "min_words": task.get("min_words", 20),
            "max_words": task.get("max_words", 400),
        })
    elif name == "judge":
        result = await ask_reviewer(output, task, hub, llm, run_id)
    else:
        result = {"problems": [f"unknown check {name}"]}

    if "error" in result:  # the tool itself broke
        result = {"problems": [f"check could not run: {result['error']}"]}
    problems = result.get("problems", [])
    passed = result.get("passed", not problems) and not problems
    return {"check": name, "passed": passed, "problems": problems}


async def ask_reviewer(output, task, hub, llm, run_id):
    """Ask an independent reviewer agent to grade the work. It only sees the task and the result,
    not how the author got there, so it isn't swayed by the author's reasoning."""
    prompt = (f"TASK:\n{task['title']}\n{task.get('instruction', '')}\n\n"
              f"{citation_policy(task)}\n\nWORK TO REVIEW:\n{output}")
    answer, _ = await run_agent("reviewer", f"reviewer:{task['id']}", prompt, llm, hub, run_id)
    try:
        verdict = first_json_object(answer)
        passed = bool(verdict["passed"])
        if passed:
            passed = float(verdict.get("score", 0)) >= 0.7
        problems = verdict.get("problems", []) or []
        if not passed and not problems:
            problems = ["reviewer gave a low score"]
        return {"passed": passed, "problems": problems}
    except (TypeError, KeyError, ValueError):
        return {"passed": False, "problems": ["the reviewer's answer wasn't valid JSON"]}


async def verify_loop(name, produce, checks, task, hub, llm, run_id, known_ids, max_tries=3):
    """Keep asking produce() for work until it passes every check, or we run out of tries.

    produce(feedback) is an async function that returns the agent's output.
    feedback is None the first time, then a list of what went wrong.
    """
    checks = sorted(checks, key=CHECK_ORDER.index)
    # Explicit checks are authoritative, including those configured by routines.
    task = {**task, "requires_citations": "citations" in checks}
    attempts = []
    feedback = None

    for attempt in range(1, max_tries + 1):
        output = await produce(feedback)

        if attempts and output.strip() == attempts[-1]["output"].strip():
            log(run_id, "no_progress", name, attempt=attempt)
            break  # the agent sent the same thing again; more tries won't help

        results = []
        for check in checks:
            result = await run_check(check, output, task, hub, llm, run_id, known_ids)
            results.append(result)
            log(run_id, "check", name, attempt=attempt, check=check, passed=result["passed"], problems=result["problems"][:5])
            if not result["passed"]:
                break  # stop at the first failure

        passed = all(r["passed"] for r in results) and len(results) == len(checks)
        attempts.append({"attempt": attempt, "output": output, "results": results, "passed": passed})
        if passed:
            return {"passed": True, "output": output, "attempts": attempts}

        feedback = make_feedback(results, attempt, max_tries)

    # Never passed: keep the attempt that got furthest through the checks, and say it's unverified.
    best = max(attempts, key=passed_check_count)
    return {"passed": False, "output": best["output"], "attempts": attempts}


def passed_check_count(attempt):
    """A failed run keeps the version that passed the most checks."""
    return sum(result["passed"] for result in attempt["results"])


def make_feedback(results, attempt, max_tries):
    lines = [f"{FEEDBACK_START} (attempt {attempt} of {max_tries}). Fix every problem and send the complete new version:"]
    for result in results:
        if not result["passed"]:
            for problem in result["problems"] or ["failed"]:
                lines.append(f"- [{result['check']}] {problem}")
    return "\n".join(lines)
