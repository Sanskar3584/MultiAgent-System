# agentmesh

A small team of AI agents that work together on a goal, and **check each other's work** before anything is accepted.

You give it a goal like *"Build a slugify() function with tests and a usage note"*. A planner splits it into tasks, a researcher, coder and writer do them, and every piece of work has to pass automatic checks and an independent AI reviewer. If a check fails, the agent gets told exactly what's wrong and tries again.

The agents share their tools through **MCP (Model Context Protocol)** servers built for this project: a shared task board, a checker that runs tests, and a document library.

![Dashboard](docs/dashboard.png)

## Running it

```bash
pip install -r requirements.txt

python main.py demo          # run the example goal and watch it in the terminal
python main.py serve         # dashboard at http://127.0.0.1:8000
python main.py routines      # list the scheduled jobs
python main.py routine knowledge-audit
pytest                       # run the tests with offline agents
```

By default, `GROQ_API_KEY` selects Groq, then `ANTHROPIC_API_KEY` selects Claude. With neither key, it uses an **offline demo team** (scripted agents in `core/fake_team.py`) that makes three mistakes on purpose so you can see the checks catch them. Set `DEMO_DELAY=0.4` to slow the offline dashboard down enough to watch.

### Using your Groq key on Windows

Run these commands in PowerShell from the project folder. The key prompt hides your input and keeps the key out of command history.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

$env:AGENTMESH_PROVIDER = "groq"
$env:GROQ_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'Groq API key' -AsSecureString)).Password

.\.venv\Scripts\python.exe main.py demo
.\.venv\Scripts\python.exe main.py serve
```

The dashboard is available at `http://127.0.0.1:8000`. These environment settings apply to this PowerShell session and its child processes. The Groq default model is `openai/gpt-oss-120b`; set `AGENTMESH_MODEL` to use another supported model.

`AGENTMESH_PROVIDER` accepts `auto` (the default), `groq`, `anthropic`, or `offline`. To reproduce the scripted demo even when keys are set, use `$env:AGENTMESH_PROVIDER = "offline"` before running it. Switch back to `groq` for real model responses.

Groq's [Free plan has usage limits](https://console.groq.com/docs/rate-limits); a full agent run makes several requests and may hit those limits. Keep your Groq account on the [Free plan](https://console.groq.com/docs/billing-faqs) for free API usage. Agentmesh does not change or verify your account's billing tier.

## How it works

General writing requests, such as **"Write a paragraph about renewable energy"**, use one writer
with word-length checks and an independent quality review. They do not require citations or a
research task. The final answer preserves the requested format, including a single paragraph.

Requests such as **"Summarize our style guide"** or **"Write a brief with sources"** still require
citations. Research uses the local `knowledge/` library; there is no web search. Required evidence
must come from relevant documents, and missing sources should be reported instead of replaced
with unrelated references. The offline team includes a scripted renewable-energy paragraph;
use a live provider for writing tailored to other topics.

The diagram below shows the multi-task workflow used by the slugify demo:

```
 goal
  │
  ▼
 planner ──► plan checked (valid JSON? dependencies make sense?)
  │
  ▼
 tasks posted to the blackboard ─────────────────────────────┐
  │                                                          │
  ▼   wave 1: research    wave 2: code      wave 3: docs     │  agents read each
 each task:  agent works ─► checks ─► fail? feedback ─► retry│  other's results
             (max 3 tries)          pass? save result ───────┘  from the blackboard
  │
  ▼
 combiner ──► final checks ──► final answer
```

| Folder | What's in it |
|---|---|
| `servers/` | The 3 MCP servers: `blackboard_server.py` (shared tasks, messages, results), `validator_server.py` (runs tests, checks code safety, citations, length, JSON), `knowledge_server.py` (document search) |
| `core/` | `tool_hub.py` connects to the servers · `agent.py` the agent loop · `roles.py` what each agent does · `checks.py` the verification loop · `orchestrator.py` runs a whole goal · `routines.py` scheduled jobs · `llm.py` Groq, Claude or the fake team · `events.py` the event log |
| `web/` | `api.py` the web server · `dashboard.html` the live dashboard |
| `routines/` | Scheduled jobs written in YAML |
| `knowledge/` | The documents the researcher can search |
| `tests/` | Tests for the servers, the logic, and the whole thing end to end |

**Explaining the project:** start with `main.py`, then `run_goal()` in `core/orchestrator.py`,
then `verify_loop()` in `core/checks.py`. [EXPLAIN.md](EXPLAIN.md) walks through a simple
writing request, the task dictionaries, the Python concepts used here, and a short demo.
