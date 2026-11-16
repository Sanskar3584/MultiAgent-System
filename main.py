"""
agentmesh: a team of AI agents that check each other's work.

    python main.py demo                     run the example goal and print what happens
    python main.py run "your goal here"     run the team on any goal
    python main.py routines                 list the scheduled routines
    python main.py routine knowledge-audit  run one routine now
    python main.py serve                    start the dashboard at http://127.0.0.1:8000

Set GROQ_API_KEY for Groq or ANTHROPIC_API_KEY for Claude (Groq takes precedence).
With neither key it uses the offline demo team (free, same result every time).
AGENTMESH_PROVIDER=groq|anthropic|offline|auto selects the provider explicitly.
AGENTMESH_MODEL overrides the model for the selected provider.
"""

import asyncio
import sys

from core import events
from core.llm import pick_llm
from core.orchestrator import run_goal
from core.routines import load_routines, run_routine
from core.tool_hub import ToolHub

DEMO_GOAL = "Build a slugify() utility that follows our URL style guide, with tests and a usage note."


def print_event(event):
    """Print the interesting events as they happen."""
    who = event["who"].ljust(20)
    kind = event["kind"]
    if kind == "run_status":
        print(f"\n== run {event['status']} ==")
    elif kind == "tool_call":
        print(f"  {who} uses {event['tool']}")
    elif kind == "check":
        verdict = "PASS" if event["passed"] else "FAIL"
        problem = ""
        if event["problems"]:
            problem = f"  <- {event['problems'][0][:90]}"
        print(f"  {who} attempt {event['attempt']}: {event['check']:<11} {verdict}{problem}")
    elif kind == "task_status":
        print(f"  {who} task {event['task']} is {event['status']}")
    elif kind == "routine_step":
        message = f"  {who} step {event['step']}: {event['status']}"
        if event["error"]:
            message += f" ({event['error']})"
        print(message)


async def run_command(goal):
    llm, model = pick_llm()
    print(f"Using: {model}")
    events.listeners.append(print_event)
    async with ToolHub() as hub:
        print(f"Connected to MCP servers: {', '.join(hub.clients)} ({len(hub.tools)} tools)")
        report = await run_goal(goal, llm, hub)
    print(f"\nStatus: {report['status']}   ({report['seconds']}s)")
    print("\n----- final result -----\n")
    print(report["final_output"])


async def routine_command(name):
    routines = load_routines()
    if name not in routines:
        print(f"No routine called {name}. Try: {', '.join(routines)}")
        return
    llm, _ = pick_llm()
    events.listeners.append(print_event)
    async with ToolHub() as hub:
        record = await run_routine(routines[name], llm, hub)
    print(f"\n{name}: {record['status']}")


def main():
    # The first argument picks the command; the remaining ones are its inputs.
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return

    command = args[0]
    if command == "demo":
        asyncio.run(run_command(DEMO_GOAL))
    elif command == "run" and len(args) > 1:
        asyncio.run(run_command(" ".join(args[1:])))
    elif command == "routines":
        for name, routine in load_routines().items():
            print(f"{name:<22} {routine.get('description', '')}")
    elif command == "routine" and len(args) > 1:
        asyncio.run(routine_command(args[1]))
    elif command == "serve":
        import uvicorn
        port = int(args[1]) if len(args) > 1 else 8000
        print(f"Dashboard: http://127.0.0.1:{port}")
        uvicorn.run("web.api:app", host="127.0.0.1", port=port, log_level="warning")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
