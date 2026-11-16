"""
The offline "team": scripted stand-ins for each agent role.

Each function gets the conversation so far and returns either:
  - a string: the agent's final answer, or
  - {"tool_calls": [(tool_name, arguments), ...]}: tools it wants to use.

They make three mistakes ON PURPOSE the first time, so the demo shows the
checks catching problems and the agents fixing them after feedback:
  1. the planner uses a role that doesn't exist   -> caught by check_json
  2. the coder's slugify() cuts words in half     -> caught by the real pytest run
  3. the writer's note is far too long            -> caught by check_length
"""

import json
import re


# ------------------------------------------------------------ helpers

def first_prompt(messages):
    return messages[0]["content"]


def got_feedback(messages):
    """True if a check failed earlier and we were asked to fix our work."""
    return any(m["role"] == "user" and isinstance(m["content"], str) and "Some checks failed" in m["content"]
               for m in messages)


def latest_tool_results(messages):
    """The tool results we just received (empty if the last message wasn't tool results)."""
    last = messages[-1]
    if last["role"] != "user" or isinstance(last["content"], str):
        return []
    return [json.loads(block["content"]) for block in last["content"]]


def goal_of(messages):
    match = re.search(r"Goal: (.+)", first_prompt(messages))
    return match.group(1).strip() if match else first_prompt(messages)


def is_slug_goal(messages):
    return "slug" in first_prompt(messages).lower()


def wants_sources(messages):
    """Small scripted demo router; real models classify the goal in their plan."""
    goal = goal_of(messages).lower()
    if re.search(r"\b(?:supplied|provided|attached)\s+(?:documents?|files?|sources?)\b|"
                 r"\b(?:knowledge base|knowledge library|style guide|our documents)\b|\[doc:", goal):
        return True
    # Asking someone for sources is a writing topic, not a request to cite this email.
    if re.search(r"\b(?:email|message|letter)\b.*\b(?:asking|requesting|request)\b.*"
                 r"\b(?:sources?|references?|citations?)\b", goal):
        return False
    goal = re.sub(r"\b(?:without|no)\s+(?:sources?|citations?|references?)\b", "", goal)
    goal = re.sub(r"\b(?:do not|don't)\s+(?:include|add|use|provide)\s+"
                  r"(?:sources?|citations?|references?)\b", "", goal)
    return bool(re.search(r"\b(?:with|include|including|provide|add|using)\s+"
                          r"(?:(?:reliable|credible|supporting)\s+)?"
                          r"(?:sources?|citations?|references?|evidence)\b|"
                          r"\b(?:cite|citing|sourced)\b|\bevidence[- ]based\b", goal))


# ------------------------------------------------------------ planner

def planner(messages):
    if not is_slug_goal(messages) and not wants_sources(messages):
        return json.dumps({"intent": "general_writing", "tasks": [
            {"id": "write", "role": "writer", "title": "Write the requested text",
             "instruction": goal_of(messages), "depends_on": [], "uses_sources": False,
             "checks": ["length", "judge"], "max_words": 200},
        ]}, indent=2)

    if not latest_tool_results(messages) and not got_feedback(messages):
        return {"tool_calls": [("knowledge__list_documents", {})]}  # look at what sources exist first

    if is_slug_goal(messages):
        intent = "other"
        tasks = [
            {"id": "research", "role": "researcher", "title": "Find the slug rules",
             "instruction": "Find the house rules for URL slugs and the Python testing standards, with citations.",
             "depends_on": [], "uses_sources": True, "checks": ["citations", "judge"]},
            {"id": "impl", "role": "coder", "title": "Write slugify() with tests",
             "instruction": "Write slugify(text, max_length=60) in solution.py following every slug rule, with pytest tests.",
             "depends_on": ["research"], "checks": ["safety", "tests", "judge"]},
            {"id": "docs", "role": "writer", "title": "Write the usage note",
             "instruction": "Write a short usage note for slugify() for the team wiki, citing the style guide.",
             "depends_on": ["research", "impl"], "uses_sources": True,
             "checks": ["citations", "length", "judge"], "max_words": 180},
        ]
        if not got_feedback(messages):
            tasks[2]["role"] = "tester"  # MISTAKE 1: there is no "tester" role
    else:
        intent = "sourced_writing"
        tasks = [
            {"id": "research", "role": "researcher", "title": "Research the topic",
             "instruction": f"Search the knowledge base for: {goal_of(messages)}. Summarise with citations.",
             "depends_on": [], "uses_sources": True, "checks": ["citations", "judge"]},
            {"id": "brief", "role": "writer", "title": "Write the brief",
             "instruction": f"Write a short brief answering: {goal_of(messages)}",
             "depends_on": ["research"], "uses_sources": True,
             "checks": ["citations", "length", "judge"], "max_words": 250},
        ]
    return json.dumps({"intent": intent, "tasks": tasks}, indent=2)


# ------------------------------------------------------------ researcher

def researcher(messages):
    results = latest_tool_results(messages)
    if not results:
        query = "slug rules hyphen lowercase testing standards" if is_slug_goal(messages) else goal_of(messages)
        return {"tool_calls": [("knowledge__search", {"query": query})]}

    if "results" in results[0]:  # we got search results: open the top two documents
        top = results[0]["results"][:2]
        if not top:
            return "I couldn't find any relevant documents in the knowledge base."
        return {"tool_calls": [("knowledge__get_document", {"doc_id": hit["id"]}) for hit in top]}

    # we got the documents: write findings with a citation on every line
    lines = ["## Findings", ""]
    for doc in results:
        rules = [line[2:] for line in doc["text"].splitlines() if line.startswith("- ")]
        lines.append(f"**{doc['title']}** [doc:{doc['id']}]")
        if rules:
            lines += [f"- {rule} [doc:{doc['id']}]" for rule in rules[:6]]
        else:
            first_paragraph = doc["text"].split("\n\n")[1] if "\n\n" in doc["text"] else doc["text"]
            lines.append(f"{first_paragraph.strip()} [doc:{doc['id']}]")
        lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------ coder

SOLUTION = '''import re
import unicodedata


def slugify(text, max_length=60):
    """Turn a title into a URL slug, following our style guide."""
    # turn accented letters into plain ones (é -> e) and drop anything else that isn't ASCII
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    # anything that isn't a letter or digit becomes a single hyphen
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(slug) > max_length:
{CUT}
    return slug or "untitled"
'''

BUGGY_CUT = '''        slug = slug[:max_length]'''

FIXED_CUT = '''        # cut at the last hyphen that fits, so no word gets chopped in half
        cut_here = slug[:max_length + 1].rfind("-")
        slug = slug[:cut_here] if cut_here > 0 else slug[:max_length]'''

TESTS = '''from solution import slugify


def test_simple_title():
    assert slugify("Hello, World!") == "hello-world"


def test_accents():
    assert slugify("Crème Brûlée Recipe") == "creme-brulee-recipe"


def test_extra_hyphens_and_spaces():
    assert slugify("--Already--Slugged--") == "already-slugged"
    assert slugify("a  _ b") == "a-b"


def test_non_ascii_is_dropped():
    assert slugify("日本 guide") == "guide"


def test_empty_becomes_untitled():
    assert slugify("!!!") == "untitled"


def test_long_titles_are_cut_between_words():
    title = "the quick brown fox jumps over the lazy dog and keeps running far away"
    slug = slugify(title)
    assert len(slug) <= 60
    assert slug.split("-")[-1] in title.split()   # last word is a whole word
'''


def coder(messages):
    if got_feedback(messages):
        code = SOLUTION.replace("{CUT}", FIXED_CUT)
        note = "Fixed: long slugs are now cut at the last hyphen, so words are never split."
    else:
        code = SOLUTION.replace("{CUT}", BUGGY_CUT)  # MISTAKE 2: cuts in the middle of a word
        note = "Here is slugify() with tests."
    return f"{note}\n\n```python file=solution.py\n{code}```\n\n```python file=test_solution.py\n{TESTS}```\n"


# ------------------------------------------------------------ writer

SHORT_NOTE = """# slugify(): usage note

`slugify(text, max_length=60)` turns a title into a URL slug that follows our style guide [doc:style-guide].

```python
slugify("Crème Brûlée Recipe")   # "creme-brulee-recipe"
slugify("!!!")                   # "untitled"
```

Slugs are lowercase ASCII, accents are converted, and runs of punctuation become one hyphen [doc:style-guide].
Long slugs are cut at a word boundary, and an empty result becomes "untitled" [doc:style-guide].
The function comes with pytest tests for every rule, as our standards require [doc:python-standards].
"""

EXTRA_RAMBLING = """
Some background on why slugs matter: they show up in every shared link, in analytics, in search results
and in support tickets, so having the same rules everywhere avoids broken links and duplicate pages.
Different teams used to write their own helpers with slightly different rules, which caused redirects
to pile up and made migrations painful, so putting it all in one tested function solves that [doc:style-guide].
"""


def writer(messages):
    prompt = first_prompt(messages)
    if "INVENTORY:" in prompt:
        return audit_report(prompt, fixed=got_feedback(messages))

    if "Writing intent: general_writing" in prompt:
        if "renewable energy" in goal_of(messages).lower():
            return (
                "Renewable energy comes from sources that are naturally replenished, such as sunlight, "
                "wind and flowing water. These sources can generate electricity while reducing reliance "
                "on fossil fuels and the emissions associated with burning them. Solar and wind output "
                "varies with the weather, so storage, flexible electricity use and reliable grids help "
                "integrate them into everyday energy supplies."
            )
        return (
            f"This is a scripted offline writing example for the request: {goal_of(messages)}. "
            "The offline team demonstrates drafting, length checks and review. Select a live model "
            "provider to generate text tailored to other topics, audiences and formats."
        )

    # first, read the earlier tasks' outputs from the blackboard (that's how agents share work)
    keys = re.findall(r"blackboard key: ([^\s)]+)", prompt)
    if keys and not latest_tool_results(messages) and not got_feedback(messages):
        return {"tool_calls": [("blackboard__get_artifact", {"key": key}) for key in keys]}

    if not is_slug_goal(messages):
        cites = " ".join(sorted(set(re.findall(r"\[doc:[\w.-]+\]", prompt))))
        bullets = [line for line in prompt.splitlines() if line.startswith("- ")][:8]
        return "# Brief\n\nHere is what our documents say about this " + cites + "\n\n" + "\n".join(bullets)

    if got_feedback(messages):
        return SHORT_NOTE
    return SHORT_NOTE + EXTRA_RAMBLING * 3  # MISTAKE 3: way over the word limit


def audit_report(prompt, fixed):
    """Used by the knowledge-audit routine."""
    inventory = json.loads(prompt.split("INVENTORY:", 1)[1])
    lines = ["# Knowledge base audit", "", f"Checked {len(inventory['documents'])} documents."]
    for doc in inventory["documents"]:
        status = "short, could use more detail" if doc["words"] < 80 else "looks fine"
        cite = f" [doc:{doc['id']}]" if fixed else ""  # first try forgets the citations
        lines.append(f"- {doc['title']} ({doc['words']} words): {status}{cite}")
    return "\n".join(lines)


# ------------------------------------------------------------ reviewer and combiner

def reviewer(messages):
    work = first_prompt(messages).split("WORK TO REVIEW:", 1)[-1]
    problems = []
    if not work.strip():
        problems.append("the work is empty")
    if "TODO" in work:
        problems.append("there are unfinished TODOs")
    return json.dumps({"passed": not problems, "score": 0.9 if not problems else 0.3, "problems": problems})


def combiner(messages):
    outputs = first_prompt(messages).split("FINISHED TASKS:", 1)[1].strip()
    if "Writing intent: general_writing" in first_prompt(messages):
        # Remove the orchestrator's task heading, keeping the checked writing's own format.
        return re.sub(r"^## [^\n]+\n", "", outputs, count=1).strip()
    cites = " ".join(sorted(set(re.findall(r"\[doc:[\w.-]+\]", outputs))))
    return f"# Final result\n\n**Goal:** {goal_of(messages)}\n\nEvery part below passed its checks {cites}.\n\n{outputs}\n"
