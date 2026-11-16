"""
Talking to the language model.

GroqLLM    - Groq-hosted models (needs GROQ_API_KEY).
ClaudeLLM  - Claude (needs ANTHROPIC_API_KEY).
FakeLLM    - a scripted stand-in so the whole project runs offline, for free,
             with the same results every time (see core/fake_team.py).

All have the same ask() method, so the rest of the code doesn't care which
one it's using. ask() returns:
    {"text": "...", "tool_calls": [{"id", "name", "input"}, ...], "content": [...]}
where "content" uses the text/tool_use blocks stored in conversation history.
"""

import asyncio
import json
import os
import uuid


class ClaudeLLM:
    def __init__(self, model=None):
        from anthropic import AsyncAnthropic  # only needed when we actually use Claude

        self.client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY from the environment
        self.model = model or os.environ.get("AGENTMESH_MODEL", "claude-sonnet-5-5")

    async def ask(self, agent_name, system_prompt, messages, tools):
        request_options = {}
        if tools:
            request_options["tools"] = tools
        response = await self.client.messages.create(
            model=self.model, max_tokens=4096, system=system_prompt,
            messages=messages, **request_options
        )
        text_parts = []
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "input": block.input})
        return build_reply("".join(text_parts), tool_calls)


class GroqLLM:
    """Translate the shared agent conversation into Groq's chat/tool format."""

    def __init__(self, model=None):
        from groq import AsyncGroq

        self.client = AsyncGroq(timeout=60.0, max_retries=2)  # reads GROQ_API_KEY
        self.model = model or os.environ.get("AGENTMESH_MODEL", "openai/gpt-oss-120b")

    async def ask(self, agent_name, system_prompt, messages, tools):
        request_options = {}
        if tools:
            groq_tools = []
            for tool in tools:
                function = {
                    "name": tool["name"], "description": tool.get("description", ""),
                    "parameters": tool["input_schema"],
                }
                groq_tools.append({"type": "function", "function": function})
            request_options["tools"] = groq_tools

        role = agent_name.split(":", 1)[0]
        if role in ("planner", "reviewer"):
            system_prompt += (
                " Return your final JSON object as assistant text. JSON is an output format, "
                "not a tool; never call a tool named json. Only call the supplied tools."
            )
        request_messages = groq_messages(system_prompt, messages)
        try:
            response = await self.client.chat.completions.create(
                model=self.model, max_completion_tokens=4096,
                messages=request_messages, **request_options
            )
        except Exception as error:
            # Some models try to emit a JSON verdict as a nonexistent function.
            # Only this specific failure gets one retry with tool calls disabled.
            if not is_json_tool_error(role, error):
                raise
            retry_message = {
                "role": "user",
                "content": (
                    "Your previous reply was rejected as an invalid tool call. "
                    "Return only the final JSON object as text, using the information already available. "
                    "Do not call any tools or invent missing evidence."
                ),
            }
            response = await self.client.chat.completions.create(
                model=self.model, max_completion_tokens=4096,
                messages=request_messages + [retry_message],
                tool_choice="none", response_format={"type": "json_object"},
            )
        return read_groq_reply(response)


def is_json_tool_error(role, error):
    """Check whether a planner/reviewer tool-call failure can be retried as JSON."""
    if role not in ("planner", "reviewer"):
        return False
    if getattr(error, "status_code", None) != 400:
        return False
    details = getattr(error, "body", None)
    if not isinstance(details, dict):
        return False
    details = details.get("error", details)
    return isinstance(details, dict) and details.get("code") == "tool_use_failed"


def read_groq_reply(response):
    """Convert Groq's response to the same format used by the other models."""
    if not response.choices:
        raise ValueError("Groq returned no response choices")

    answer = response.choices[0].message
    tool_calls = []
    for call in answer.tool_calls or []:
        tool_name = call.function.name
        try:
            arguments = json.loads(call.function.arguments)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"Groq returned invalid JSON arguments for tool {tool_name}") from error
        if not isinstance(arguments, dict):
            raise ValueError(f"Groq tool arguments for {tool_name} must be a JSON object")
        tool_calls.append({"id": call.id, "name": tool_name, "input": arguments})
    return build_reply(answer.content or "", tool_calls)


def build_reply(text, tool_calls):
    """Keep the answer, tool requests and saved conversation in one shared shape."""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for call in tool_calls:
        content.append({
            "type": "tool_use", "id": call["id"],
            "name": call["name"], "input": call["input"],
        })
    return {"text": text, "tool_calls": tool_calls, "content": content}


def model_error_message(error):
    """Keep the provider's useful error message without its generated payload."""
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        detail = body.get("error", body)
        if isinstance(detail, dict) and isinstance(detail.get("message"), str):
            status = getattr(error, "status_code", None)
            prefix = f"Model request failed (HTTP {status})" if status else "Model request failed"
            return f"{prefix}: {detail['message']}"
    return str(error) or type(error).__name__


def groq_messages(system_prompt, messages):
    """Convert stored text/tool blocks without changing the agent's history."""
    converted = [{"role": "system", "content": system_prompt}]
    for message in messages:
        role = message["role"]
        content = message["content"]
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue

        text_parts = []
        for block in content:
            if block["type"] == "text":
                text_parts.append(block["text"])
        text = "".join(text_parts)

        if role == "assistant":
            tool_calls = []
            for block in content:
                if block["type"] == "tool_use":
                    function = {"name": block["name"], "arguments": json.dumps(block["input"])}
                    tool_calls.append({"id": block["id"], "type": "function", "function": function})
            reply = {"role": "assistant", "content": text or None}
            if tool_calls:
                reply["tool_calls"] = tool_calls
            converted.append(reply)
        elif role == "user":
            # Groq expects one tool message per result, linked by the original ID.
            for block in content:
                if block["type"] == "tool_result":
                    converted.append({"role": "tool", "tool_call_id": block["tool_use_id"],
                                      "content": block["content"]})
            if text:
                converted.append({"role": "user", "content": text})
        else:
            raise ValueError(f"Unsupported conversation role: {role}")
    return converted


class FakeLLM:
    """Pretends to be the model. Each role has a function in fake_team.py that
    looks at the conversation and decides what to say or which tool to call."""

    def __init__(self, delay=0.0):
        from core import fake_team

        self.team = fake_team
        self.delay = delay  # optional pause so you can watch the dashboard update

    async def ask(self, agent_name, system_prompt, messages, tools):
        if self.delay:
            await asyncio.sleep(self.delay)
        role = agent_name.split(":")[0]          # "coder:impl" -> "coder"
        answer = getattr(self.team, role)(messages)
        return make_reply(answer)


def make_reply(answer):
    """fake_team functions return either a string (final answer)
    or {"tool_calls": [(name, input), ...]}. Turn that into an ask() reply."""
    if isinstance(answer, str):
        return {"text": answer, "tool_calls": [], "content": [{"type": "text", "text": answer}]}
    tool_calls = []
    for name, arguments in answer["tool_calls"]:
        call_id = "call_" + uuid.uuid4().hex[:8]
        tool_calls.append({"id": call_id, "name": name, "input": arguments})
    return build_reply("", tool_calls)


def pick_llm(offline=None):
    """Select an explicit provider, or prefer Groq, then Claude, then offline."""
    provider = os.environ.get("AGENTMESH_PROVIDER", "auto").strip().lower()
    if offline is True:
        provider = "offline"
    if provider not in ("auto", "groq", "anthropic", "offline"):
        raise ValueError("AGENTMESH_PROVIDER must be auto, groq, anthropic, or offline")
    if provider == "auto":
        if os.environ.get("GROQ_API_KEY"):
            provider = "groq"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif offline is False:
            raise ValueError("Set GROQ_API_KEY or ANTHROPIC_API_KEY to use a live model")
        else:
            provider = "offline"
    if provider == "offline":
        delay = float(os.environ.get("DEMO_DELAY", "0"))
        return FakeLLM(delay=delay), "offline demo team"

    if provider == "groq":
        key_name = "GROQ_API_KEY"
    else:
        key_name = "ANTHROPIC_API_KEY"
    if not os.environ.get(key_name):
        raise ValueError(f"Set {key_name} to use AGENTMESH_PROVIDER={provider}")

    if provider == "groq":
        llm = GroqLLM()
    else:
        llm = ClaudeLLM()
    return llm, llm.model
