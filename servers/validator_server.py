"""
Validator MCP server: the automatic checks.

Every tool here returns the same simple shape:
    {"passed": True/False, "problems": ["...", "..."]}

The problems are written so they can be sent straight back to an agent as
feedback ("test X failed", "too long: 282 words, max 180"), which is what
makes the retry loop work.

Run it on its own with:  python servers/validator_server.py
"""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import jsonschema
from mcp.server.fastmcp import FastMCP

server = FastMCP("validator", log_level="WARNING")

# Code that agents write must not do anything dangerous before we run it.
BANNED_IMPORTS = {"subprocess", "socket", "shutil", "ctypes", "requests", "urllib", "http", "multiprocessing"}
BANNED_CALLS = {"os.system", "os.popen", "os.remove", "eval", "exec", "__import__"}


def call_name(node):
    """Turn a call like os.system(...) into the string 'os.system'."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return call_name(node.value) + "." + node.attr
    return ""


@server.tool()
def check_code_safety(code: str) -> dict:
    """Make sure the code parses and doesn't import or call anything dangerous."""
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        return {"passed": False, "problems": [f"syntax error on line {error.lineno}: {error.msg}"]}

    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BANNED_IMPORTS:
                    problems.append(f"line {node.lineno}: importing {alias.name} is not allowed")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in BANNED_IMPORTS:
                problems.append(f"line {node.lineno}: importing {node.module} is not allowed")
        elif isinstance(node, ast.Call) and call_name(node.func) in BANNED_CALLS:
            problems.append(f"line {node.lineno}: calling {call_name(node.func)}() is not allowed")
    return {"passed": not problems, "problems": problems}


def build_test_environment(folder):
    """Give generated tests the OS settings they need, without copying API keys."""
    environment = {}
    for name in ("PATH", "SystemRoot", "WINDIR"):
        if name in os.environ:
            environment[name] = os.environ[name]
    for name in ("HOME", "USERPROFILE", "TEMP", "TMP"):
        environment[name] = folder
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return environment


@server.tool()
def run_tests(files: dict[str, str], timeout_seconds: int = 30) -> dict:
    """Write the files to a temporary folder and run pytest on them.

    files looks like {"solution.py": "...code...", "test_solution.py": "...tests..."}
    """
    if not any(name.startswith("test_") for name in files):
        return {"passed": False, "problems": ["no test file found (it should be called test_something.py)"]}

    # Safety first: only simple file names, and no dangerous code.
    for name, code in files.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.py", name):
            return {"passed": False, "problems": [f"bad file name {name!r}"]}
        safety = check_code_safety(code)
        if not safety["passed"]:
            problems = [f"{name}: {problem}" for problem in safety["problems"]]
            return {"passed": False, "problems": problems}

    with tempfile.TemporaryDirectory() as folder:
        for name, code in files.items():
            Path(folder, name).write_text(code, encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rf", folder],
                cwd=folder, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout_seconds,
                env=build_test_environment(folder),
            )
        except subprocess.TimeoutExpired:
            return {"passed": False, "problems": [f"tests took longer than {timeout_seconds}s (infinite loop?)"]}

    output = result.stdout + result.stderr
    if result.returncode == 0:
        return {"passed": True, "problems": [], "output": output[-1500:]}

    # Pull out the useful lines: which tests failed first, then the assertion messages.
    lines = output.splitlines()
    failed = [line.strip() for line in lines if line.startswith("FAILED")]
    errors = [line.strip() for line in lines if line.startswith("E ") and "+  where" not in line]
    useful = failed + errors
    return {"passed": False, "problems": useful[:8] or [output[-800:]], "output": output[-1500:]}


@server.tool()
def check_json(text: str, schema: dict) -> dict:
    """Check that text is valid JSON and matches the given JSON schema."""
    text = text.strip()
    if text.startswith("```"):  # allow ```json ... ``` fences
        text = text.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        return {"passed": False, "problems": [f"not valid JSON: {error.msg}"]}

    validator = jsonschema.Draft202012Validator(schema)
    problems = []
    for error in validator.iter_errors(data):
        location = "/".join(str(part) for part in error.path) or "top level"
        problems.append(f"{location}: {error.message}")
    return {"passed": not problems, "problems": problems}


@server.tool()
def check_citations(text: str, known_ids: list[str]) -> dict:
    """Check the [doc:id] citations: there must be some, and every id must be a real document."""
    cited = re.findall(r"\[doc:([\w.-]+)\]", text)
    problems = []
    if not cited:
        problems.append("no citations found; cite sources like [doc:style-guide]")
    made_up = sorted(set(cited) - set(known_ids))
    if made_up:
        problems.append(f"these sources don't exist (made up?): {made_up}")
    return {"passed": not problems, "problems": problems}


@server.tool()
def check_length(text: str, max_words: int = 400, min_words: int = 20, must_include: list[str] | None = None) -> dict:
    """Check the word count, and that some required words or phrases appear."""
    words = len(text.split())
    problems = []
    if words > max_words:
        problems.append(f"too long: {words} words, maximum is {max_words}")
    if words < min_words:
        problems.append(f"too short: {words} words, minimum is {min_words}")
    for phrase in must_include or []:
        if phrase.lower() not in text.lower():
            problems.append(f"missing required text: {phrase!r}")
    return {"passed": not problems, "problems": problems, "words": words}


if __name__ == "__main__":
    server.run()
