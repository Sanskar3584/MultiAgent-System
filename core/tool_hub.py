"""
ToolHub: connects to all our MCP servers and lets agents call their tools.

Each server runs as its own process, and we talk to it over stdin/stdout
using the MCP protocol. Tool names get the server name in front
("validator__run_tests"), so two servers could both have a "search" tool
without clashing.
"""

import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).parent.parent

# server name -> the script that runs it
SERVERS = {
    "blackboard": ROOT / "servers" / "blackboard_server.py",
    "validator": ROOT / "servers" / "validator_server.py",
    "knowledge": ROOT / "servers" / "knowledge_server.py",
}


class ToolHub:
    def __init__(self, servers=None):
        self.servers = servers or SERVERS
        self.clients = {}   # server name -> MCP client
        self.tools = {}     # "server__tool" -> tool description from the server
        # The stack closes connections in reverse order, including after a failure.
        self.stack = AsyncExitStack()

    async def start(self):
        """Start every server process and ask each one which tools it has."""
        server_env = {}
        for variable in ("BLACKBOARD_FILE", "KNOWLEDGE_DIR"):
            if variable in os.environ:
                server_env[variable] = os.environ[variable]
        server_env["PYTHONUTF8"] = "1"
        try:
            for server_name, script in self.servers.items():
                parameters = StdioServerParameters(
                    command=sys.executable, args=[str(script)], env=server_env
                )
                read_stream, write_stream = await self.stack.enter_async_context(stdio_client(parameters))
                client = await self.stack.enter_async_context(ClientSession(read_stream, write_stream))
                await client.initialize()
                self.clients[server_name] = client

                available_tools = await client.list_tools()
                for tool in available_tools.tools:
                    full_name = f"{server_name}__{tool.name}"
                    self.tools[full_name] = tool
        except BaseException:
            # Also clean up if startup was cancelled partway through.
            await self.stop()
            raise

    async def stop(self):
        try:
            await self.stack.aclose()
        finally:
            self.clients.clear()
            self.tools.clear()

    # Lets us write:  async with ToolHub() as hub: ...
    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *error):
        await self.stop()

    async def call(self, full_name, arguments=None):
        """Call a tool like "validator__run_tests" and return its result as a dict."""
        if full_name not in self.tools:
            return {"error": f"there is no tool called {full_name}"}
        server_name, tool_name = full_name.split("__", 1)
        result = await self.clients[server_name].call_tool(tool_name, arguments or {})
        text_parts = []
        for block in result.content:
            if block.type == "text":
                text_parts.append(block.text)
        text = "\n".join(text_parts)
        if result.isError:
            return {"error": text}
        if result.structuredContent is not None:
            return result.structuredContent
        try:
            return json.loads(text)   # our servers always return JSON objects
        except json.JSONDecodeError:
            return {"text": text}

    def tool_list_for_claude(self, allowed):
        """Describe the allowed tools in the shared format used by our agents.

        This is Claude's tool format; the Groq adapter converts it when needed.
        allowed is a list like ["knowledge__search", "validator__*"]; a name
        ending in * means "every tool from that server".
        """
        tool_descriptions = []
        for full_name, tool in self.tools.items():
            if is_allowed(full_name, allowed):
                tool_descriptions.append({
                    "name": full_name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema,
                })
        return tool_descriptions


def is_allowed(full_name, allowed):
    """Match an exact tool name or a prefix such as 'knowledge__*'."""
    for pattern in allowed:
        if pattern == full_name:
            return True
        if pattern.endswith("*"):
            prefix = pattern[:-1]
            if full_name.startswith(prefix):
                return True
    return False
