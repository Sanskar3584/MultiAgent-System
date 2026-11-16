# How to explain agentmesh

A guide for walking someone through the project: what to say, what to show, and what they'll probably ask.

---

## 1. The 30-second version

> "This project takes a goal and chooses the work needed to finish it. A simple paragraph needs a writer; a coding task can need research, code and a usage note. The agents share a task board and tools. Their output is checked, and failed checks are sent back as feedback so they can try again. There is also a dashboard to watch progress and a scheduler for repeated jobs."

An **agent** here is a role prompt, a model call and a set of allowed tools. It is not a separately trained model. A **tool** is a Python function exposed through MCP. A **check** decides whether an output can be accepted.

---

## 2. The three ideas the project is built on

**1. MCP servers for the tools.** MCP is a standard way for AI models to use tools. Each server is a separate program that offers tools (like `run_tests` or `search`). Because it's a standard, the same servers also work with Claude Desktop or Claude Code, not just my agents.

**2. A blackboard instead of agents chatting.** Agents never message each other. They post tasks, claim tasks, and save results on a shared blackboard. That keeps things simple: there's one place to look to see what's going on, and adding a new agent doesn't mean wiring it to every other agent.

**3. The verification loop.** LLMs often say they're done when they aren't. So every output goes through checks:

- cheap automatic ones first (run the tests, check citations, count words)
- then an AI reviewer that didn't write the work

If a check fails, the problems are sent back as feedback in the same conversation and the agent tries again (up to 3 times). We stop at the first failing check, so we don't pay for an AI review of code whose tests don't even pass.

---

## 3. What happens in one run (follow the demo)

Goal: *"Build a slugify() utility that follows our URL style guide, with tests and a usage note."*

This walkthrough uses `AGENTMESH_PROVIDER=offline`, so the mistakes and fixes are scripted. Groq and Claude use the same workflow, but their plans and results vary.

1. **Planning** (`core/orchestrator.py` → `run_goal`). The planner looks at which documents exist and writes a plan as JSON.
   *Its first plan uses a role called "tester" that doesn't exist. The `plan_format` check catches it, the planner gets the error and fixes the plan.*
2. **Posting.** The three tasks (research → code → docs) go onto the blackboard with their dependencies.
3. **Research.** The researcher searches the documents, opens the two best matches and writes findings with a `[doc:id]` citation on every line. The citation check makes sure every cited id is a real document.
4. **Code.** The coder writes `slugify()` and tests. The validator server actually runs pytest on them.
   *The first version cuts long slugs in the middle of a word. A test fails, the coder gets the exact failing test, fixes the cut, and the tests pass.*
5. **Docs.** The writer reads the research and code **from the blackboard** (you can see the `get_artifact` tool calls), then writes a usage note.
   *The first draft is 274 words with a 180-word limit. The length check catches it, and the writer sends a shorter version.*
6. **Combine.** The combiner joins everything into one answer, which gets one last check.

Three mistakes, three different checks, all fixed automatically.

---

## 4. Tour of the code (in the order I'd show it)

| File | What to say about it |
|---|---|
| `main.py` | Start here. `main()` reads the command, selects a model and opens the tool connections. Both the command line and the website eventually call `run_goal()`. |
| `core/orchestrator.py` | `run_goal()` reads as four steps: plan, post, work, combine. `run_task()` handles one task. `task_prompt()` gives it the right context. `combine_results()` prepares the final answer. `make_waves()` groups tasks that can run together. |
| `core/checks.py` | `run_check()` runs one check; `verify_loop()` retries failed work; `make_feedback()` turns failures into instructions. Source requirements are passed to the author and reviewer. |
| `core/agent.py` | `run_agent()` asks the model, runs any requested tools, and asks again with those results. `ask_model()` shows progress and limits each request to 90 seconds. |
| `core/roles.py` | Each role's instructions and its allowed tools. The planner also chooses whether writing needs sources. The reviewer can read but not write. |
| `core/llm.py` + `core/fake_team.py` | `GroqLLM`, `ClaudeLLM` and `FakeLLM` share an `ask()` method. Provider-specific replies become the same dictionary. The fake team supplies scripted replies for offline demos and tests. |
| `servers/blackboard_server.py` | Shared state in one dictionary. `@server.tool()` turns a normal function into an MCP tool. There's a lock because tool calls can run in parallel threads; that's what stops two agents claiming the same task. `is_ready()` is where dependencies are enforced. |
| `servers/validator_server.py` | Every check returns `{"passed", "problems"}`. `run_tests` writes the files to a temp folder and runs pytest with a timeout; before that, `check_code_safety` walks the code's syntax tree (`ast`) and refuses dangerous imports like `subprocess`. |
| `servers/knowledge_server.py` | Simple keyword search: more matching words means a higher score, and rare words count more. |
| `core/tool_hub.py` | Starts each server as its own process and talks MCP to it over stdin/stdout. Tool names get the server's name in front (`validator__run_tests`) so they can't clash. |
| `core/routines.py` | Loads YAML jobs, fills in `{{steps.x.output}}` templates, retries failed steps, and the `Scheduler` checks every 20 seconds what's due. It won't start a routine that's still running from last time. |
| `web/api.py` + `web/dashboard.html` | FastAPI endpoints; the dashboard polls `/api/runs/<id>/events` every 0.7s and redraws. |

---

## 5. Explain one simple request first

Use: **"Write a paragraph about renewable energy."**

1. The website sends the goal to `POST /api/runs`. The API starts `run_goal()` in the background so the page can keep updating.
2. The planner returns JSON with intent `general_writing`. `normalize_plan()` makes this one writer task with `length` and `judge` checks.
3. `run_task()` claims the task on the blackboard. `work_with_checks()` connects the agent to the retry loop.
4. The writer returns a paragraph. The validator counts its words. A fresh reviewer checks the quality and requested format. Neither requires citations for this request.
5. If a check fails, its problems go back to the writer in the same conversation. If it passes, the text is saved as an artifact: a named result on the blackboard.
6. `combine_results()` prepares the final answer and checks it again. For this request, the final answer stays a paragraph.

A task is just a dictionary. This example shows its main fields:

```python
task = {
    "id": "write",
    "role": "writer",
    "title": "Write the response",
    "instruction": "Write a paragraph about renewable energy.",
    "depends_on": [],
    "checks": ["length", "judge"],
    "max_words": 200,
}
```

`depends_on` is empty because the writer can start immediately. In the slugify demo, the coder waits for research, so that field contains `"research"`. `make_waves()` puts those tasks in the right order. An artifact key such as `run-123abc/write` identifies both the run and the task.

A failed check is also a dictionary:

```python
{"check": "length", "passed": False, "problems": ["too long: 250 words, maximum is 200"]}
```

The same format is used for word counts, citations, tests and reviews. That is why one retry loop can handle all of them. Citations are added when the user asks for sources or the work depends on documents, such as the URL style guide.

## 6. Python features worth being able to explain

| Code you will see | What it means here |
|---|---|
| `async def` and `await` | Model calls and tools take time. While one call waits, Python can work on other pending tasks. This does not make every line run in parallel. |
| `asyncio.gather(*jobs)` | Start all tasks in one wave and wait for every task in that wave before continuing. |
| `async with ToolHub()` | Open the MCP connections and close them when the block finishes, including when it raises an error. |
| `@server.tool()` | Register the function below it so an MCP client can call it. |
| `@app.get(...)` / `@app.post(...)` | Tell FastAPI which function handles a particular URL and HTTP method. |
| `produce(feedback)` inside `work_with_checks()` | Keep a task's conversation between attempts. `nonlocal conversation` lets this inner function update the conversation belonging to the outer function. |
| A `set` of running jobs | Keep track of tasks that have started and prevent scheduled jobs from overlapping. |
| `try` / `except` / `finally` | Handle failures and perform cleanup, such as removing a routine from the running set. |

Start by tracing the general-writing example. Then explain the slugify demo, which adds dependencies and generated-code testing. Finally show one YAML routine, which reuses the same tools and checks on a schedule.

## 7. Demo script (about 3 minutes)

Set `AGENTMESH_PROVIDER=offline` first (in PowerShell: `$env:AGENTMESH_PROVIDER = "offline"`). This keeps the demo deterministic even if an API key is present. See the README for Groq setup; use `AGENTMESH_PROVIDER=groq` to run with your Groq key.

1. `python main.py demo`: point at the three FAIL lines and the PASS lines right after them.
2. `python main.py serve`, open the dashboard, click **Use the slugify example**, then **Start run** (run with `DEMO_DELAY=0.4` so it's watchable).
   - Point at the red stamps and the quoted problem on each card; the next attempt row is all green.
   - Point at the activity feed: the writer calling `blackboard__get_artifact` is agents sharing work through MCP.
3. Bottom tabs: **Routines** → click **Run now** on `knowledge-audit`, then open **Channels → reports** to see what it posted.
4. `pytest`: tests include provider-adapter checks and ones that start the real servers and run everything end to end with offline agents.

---

## 8. Questions you'll probably get

**Why MCP and not just Python functions?**
The tools become reusable: any MCP client (Claude Desktop, Claude Code, other agent frameworks) can use the same servers. Each server is also its own process, so a crash or a slow test run doesn't take down the agents.

**Why a blackboard instead of agents messaging each other?**
With N agents talking directly you get N×N connections and conversations that are hard to follow. With a blackboard every agent only knows one thing, the board, and the board is a complete record of what happened.

**How do you stop an agent looping forever?**
There are at most 8 tool rounds per agent, then one final request without tools. The agent stops early if it repeats the same tool call 3 times. Verification allows at most 3 attempts and stops early if the output does not change. Each model request also has a 90-second timeout, including provider retries.

**Isn't an AI reviewing an AI pointless?**
That's why it's not the only check. The deterministic checks (real tests, citation lookup, word count) come first and can't be talked around. The reviewer is a separate agent with a fresh conversation, so it only sees the task and the result, not the author's reasoning. Using a different model for the reviewer would make it more independent still.

**Running code an AI wrote sounds dangerous.**
It is, so there are layers: the syntax-tree check blocks dangerous imports and calls before anything runs, tests run in a temporary folder with a timeout and without our environment variables. I'd be honest that this is fine for a demo but not a real sandbox; in production I'd run the validator in a locked-down container with no network.

**What happens if a task fails all 3 tries?**
It's marked failed, dependent tasks are skipped, and independent tasks can still finish. The run is `partial` if some tasks finished, or `failed` if none did. A message goes to the `escalations` channel; the `failed-task-digest` routine can collect those failures into a report.

**How do you test something that uses an LLM?**
With the fake team. It returns the same answers every time, including the planted mistakes, so the tests can check that each mistake is caught and fixed. The end-to-end tests start the real MCP servers as separate processes. Provider-adapter tests use mocked API replies to check tool calls and conversation conversion without consuming API quota.

**Which model provider does it use?**
`AGENTMESH_PROVIDER` chooses `auto`, `groq`, `anthropic`, or `offline`. In automatic mode a `GROQ_API_KEY` selects Groq first, an `ANTHROPIC_API_KEY` selects Claude next, and no keys selects the offline team. Groq defaults to `openai/gpt-oss-120b`; `AGENTMESH_MODEL` overrides the selected provider's model. Groq Free plan quotas are controlled by the account, and Agentmesh does not enable billing or verify whether a key belongs to a free account.

**What would you improve next?**

- Compare Groq and Claude on how often each check catches something.
- A different model for the reviewer than for the authors.
- Store the blackboard in a database instead of memory/JSON so several machines could share it.
- A proper sandbox (container) for running generated code.

**How did you build it?**
Answer this honestly in your own words. If you used an AI assistant, say how you used it and what you decided, checked or changed yourself, for example running the tests, reading through each file, or asking for the simplified version. Interviewers mostly want to see that you understand the design and the trade-offs, which this guide is meant to help with.
