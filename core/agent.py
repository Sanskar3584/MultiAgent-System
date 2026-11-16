"""
The agent loop.

An agent is just: a role prompt + an LLM + some allowed tools.
We send the conversation to the LLM. If it asks to use tools, we run them
through the ToolHub, add the results to the conversation, and ask again.
When it answers without asking for tools, that answer is the result.

Two safety limits stop an agent from running forever:
  - max_steps: at most this many rounds of tool use
  - repeat check: asking for the exact same tool call 3 times in a row means it's stuck
"""

import asyncio
import json
import time

from core.events import log
from core.llm import model_error_message
from core.roles import ROLES
from core.tool_hub import is_allowed

MODEL_TIMEOUT_SECONDS = 90


async def ask_model(role_prompt, name, messages, tools, llm, run_id):
    """Show request progress and stop waiting after 90 seconds, including retries."""
    started = time.monotonic()
    log(run_id, "model_status", name, status="waiting", detail="Waiting for model response")
    try:
        reply = await asyncio.wait_for(
            llm.ask(name, role_prompt, messages, tools), timeout=MODEL_TIMEOUT_SECONDS
        )
    except TimeoutError as error:
        detail = f"Model response timed out after {MODEL_TIMEOUT_SECONDS:g} seconds. Try again."
        log(run_id, "model_status", name, status="failed", detail=detail,
            seconds=round(time.monotonic() - started, 2))
        raise TimeoutError(detail) from error
    except Exception as error:
        detail = model_error_message(error)
        log(run_id, "model_status", name, status="failed", detail=detail,
            seconds=round(time.monotonic() - started, 2))
        raise RuntimeError(detail) from error
    log(run_id, "model_status", name, status="done", seconds=round(time.monotonic() - started, 2))
    return reply


async def run_agent(role, name, prompt, llm, hub, run_id, history=None, max_steps=8):
    """Run one agent until it gives a final answer.

    Returns (answer, history). Pass history back in to continue the same
    conversation, which is how an agent gets feedback and fixes its work.
    """
    role_info = ROLES[role]
    allowed_tools = role_info["tools"]
    role_prompt = role_info["prompt"]
    tools = hub.tool_list_for_claude(allowed_tools)
    # Copy the list so adding messages does not change the caller's history list.
    messages = list(history or [])
    messages.append({"role": "user", "content": prompt})
    recent_calls = []

    for _ in range(max_steps):
        reply = await ask_model(role_prompt, name, messages, tools, llm, run_id)
        messages.append({"role": "assistant", "content": reply["content"]})

        if not reply["tool_calls"]:
            return reply["text"], messages  # no tools requested: this is the final answer

        # Ignore call IDs: repeating the same tool and arguments means no progress.
        for call in reply["tool_calls"]:
            call_details = json.dumps([call["name"], call["input"]], sort_keys=True)
            recent_calls.append(call_details)
        if len(recent_calls) >= 3 and len(set(recent_calls[-3:])) == 1:
            log(run_id, "agent_stuck", name, call=recent_calls[-1])
            return reply["text"] or "(the agent got stuck repeating itself)", messages

        # gather runs these calls together and returns their results in the same order.
        pending_calls = []
        for call in reply["tool_calls"]:
            pending_calls.append(use_tool(call, allowed_tools, name, hub, run_id))
        results = await asyncio.gather(*pending_calls)
        messages.append({"role": "user", "content": results})

    # Out of steps: ask once more for a final answer, this time without tools.
    messages.append({"role": "user", "content": "You are out of tool steps. Give your best final answer now."})
    reply = await ask_model(role_prompt, name, messages, [], llm, run_id)
    messages.append({"role": "assistant", "content": reply["content"]})
    return reply["text"], messages


async def use_tool(call, allowed, agent_name, hub, run_id):
    """Run one tool call and package the result the way the LLM expects."""
    log(run_id, "tool_call", agent_name, tool=call["name"], input=call["input"])
    if not is_allowed(call["name"], allowed):
        result = {"error": f"{agent_name} is not allowed to use {call['name']}"}
    else:
        result = await hub.call(call["name"], call["input"])
    return {
        "type": "tool_result",
        "tool_use_id": call["id"],
        "content": json.dumps(result)[:10000],  # keep huge results from flooding the conversation
        "is_error": "error" in result,
    }
