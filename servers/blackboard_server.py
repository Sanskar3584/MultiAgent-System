"""
Blackboard MCP server: the shared workspace for the agents.

The agents never talk to each other directly. Instead they all read and
write this "blackboard":
  - tasks:     work items, with dependencies on other tasks
  - messages:  notes posted to named channels (e.g. "reports", "escalations")
  - artifacts: saved outputs (each task's result is stored here)

Run it on its own with:  python servers/blackboard_server.py
"""

import json
import os
import threading
import time

from mcp.server.fastmcp import FastMCP

server = FastMCP("blackboard", log_level="WARNING")

# All the shared state lives in this one dictionary.
board = {"tasks": {}, "messages": [], "artifacts": {}}

# MCP can run tool calls in parallel threads, so every change goes through
# this lock. That way two agents can't claim the same task at the same time.
lock = threading.Lock()

# Optional: save the board to a JSON file so it survives restarts.
SAVE_FILE = os.environ.get("BLACKBOARD_FILE")


def save():
    if SAVE_FILE:
        with open(SAVE_FILE, "w", encoding="utf-8") as saved_board:
            json.dump(board, saved_board, indent=2)


def load():
    global board
    if SAVE_FILE and os.path.exists(SAVE_FILE):
        with open(SAVE_FILE, encoding="utf-8") as saved_board:
            board = json.load(saved_board)


# ---------------------------------------------------------------- tasks

@server.tool()
def add_task(task_id: str, title: str, description: str = "", role: str = "", depends_on: list[str] | None = None) -> dict:
    """Add a task. It can only be claimed after all the tasks in depends_on are done."""
    depends_on = depends_on or []  # (a [] default would be shared between calls)
    with lock:
        if task_id in board["tasks"]:
            return {"error": f"task {task_id} already exists"}
        for dependency in depends_on:
            if dependency not in board["tasks"]:
                return {"error": f"unknown dependency {dependency}"}
        board["tasks"][task_id] = {
            "id": task_id, "title": title, "description": description, "role": role,
            "depends_on": depends_on, "status": "open", "owner": None, "result": None,
            "created": time.time(),
        }
        save()
        return board["tasks"][task_id]


def is_ready(task):
    """A task is ready when it's still open and everything it depends on is done."""
    if task["status"] != "open":
        return False
    for dependency in task["depends_on"]:
        if board["tasks"][dependency]["status"] != "done":
            return False
    return True


@server.tool()
def claim_task(agent: str, task_id: str = "", role: str = "") -> dict:
    """Claim a ready task (a specific one, or the oldest one for your role)."""
    with lock:
        if task_id:
            candidates = [board["tasks"].get(task_id)]
        else:
            candidates = sorted(board["tasks"].values(), key=lambda task: task["created"])
        for task in candidates:
            if task is None:
                continue
            if role and task["role"] != role:
                continue
            if is_ready(task):
                task["status"] = "claimed"
                task["owner"] = agent
                save()
                return {"claimed": True, "task": task}
        return {"claimed": False, "task": None}


@server.tool()
def finish_task(task_id: str, agent: str, result: str, success: bool = True) -> dict:
    """Mark your claimed task as done (or failed) and store a short result."""
    with lock:
        task = board["tasks"].get(task_id)
        if task is None:
            return {"error": f"unknown task {task_id}"}
        if task["owner"] != agent:
            return {"error": f"task {task_id} belongs to {task['owner']}, not {agent}"}
        task["status"] = "done" if success else "failed"
        task["result"] = result
        save()
        return task


@server.tool()
def list_tasks(status: str = "") -> dict:
    """List all tasks, or only the ones with a given status (open/claimed/done/failed)."""
    with lock:
        tasks = []
        for task in board["tasks"].values():
            if not status or task["status"] == status:
                tasks.append(task)
        return {"tasks": tasks}


# ------------------------------------------------------------- messages

@server.tool()
def post_message(channel: str, sender: str, text: str) -> dict:
    """Post a message to a channel."""
    with lock:
        message = {"id": len(board["messages"]) + 1, "channel": channel, "sender": sender,
                   "text": text, "time": time.time()}
        board["messages"].append(message)
        save()
        return message


@server.tool()
def read_messages(channel: str) -> dict:
    """Read all messages on a channel."""
    with lock:
        messages = [message for message in board["messages"] if message["channel"] == channel]
        return {"messages": messages}


# ------------------------------------------------------------ artifacts

@server.tool()
def save_artifact(key: str, content: str, author: str) -> dict:
    """Save an output under a key. Task outputs are saved under their task id."""
    with lock:
        previous = board["artifacts"].get(key)
        version = previous["version"] + 1 if previous else 1
        board["artifacts"][key] = {"key": key, "content": content, "author": author, "version": version}
        save()
        return board["artifacts"][key]


@server.tool()
def get_artifact(key: str) -> dict:
    """Read a saved output, e.g. the result of a task another agent did."""
    with lock:
        if key not in board["artifacts"]:
            return {"error": f"no artifact called {key}"}
        return board["artifacts"][key]


@server.tool()
def list_artifacts() -> dict:
    """List the keys of all saved artifacts."""
    with lock:
        return {"keys": sorted(board["artifacts"])}


if __name__ == "__main__":
    load()
    server.run()  # talks MCP over stdin/stdout
