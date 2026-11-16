"""Writing uses evidence only when the requested work depends on it."""

import json

import pytest

from core import events
from core.llm import FakeLLM, make_reply
from core.orchestrator import normalize_plan, run_goal
from core.tool_hub import ToolHub
from servers import validator_server as validator


def task(task_id, role="writer", **options):
    return {"id": task_id, "role": role, "title": task_id,
            "instruction": "Complete this task.", **options}


@pytest.fixture(autouse=True)
def isolated_events(monkeypatch):
    monkeypatch.setattr(events, "events", [])
    monkeypatch.setattr(events, "listeners", [])


def test_general_writing_removes_unnecessary_research_and_source_requirements():
    goal = "Write a paragraph about renewable energy."
    plan = {"intent": "general_writing", "tasks": [
        task("research", "researcher"),
        task("draft", depends_on=["research"], checks=["citations", "judge"],
             instruction="Find academic sources and write a research report.", max_words=90),
    ]}

    tasks = normalize_plan(plan, goal)

    assert len(tasks) == 1
    writer = tasks[0]
    assert writer["role"] == "writer"
    assert writer["instruction"] == goal
    assert writer["depends_on"] == []
    assert set(writer["checks"]) == {"length", "judge"}
    assert writer["uses_sources"] is False
    assert writer["min_words"] == 1 and writer["max_words"] == 90


def test_general_writing_preserves_the_requested_word_count_bounds():
    tasks = normalize_plan({"intent": "general_writing", "tasks": [
        task("draft", min_words=8, max_words=12),
    ]}, "Write a greeting in eight to twelve words.")

    assert tasks[0]["min_words"] == 8
    assert tasks[0]["max_words"] == 12


def test_source_dependencies_propagate_through_code_without_citing_code():
    # Deliberately not in dependency order: the prose still inherits research.
    plan = {"intent": "other", "tasks": [
        task("docs", depends_on=["impl"], checks=["length", "judge"]),
        task("impl", "coder", depends_on=["research"]),
        task("research", "researcher"),
        task("message"),
    ]}

    tasks = {t["id"]: t for t in normalize_plan(plan, "Build a utility and describe it.")}

    assert "citations" in tasks["research"]["checks"]
    assert "citations" in tasks["docs"]["checks"]
    assert tasks["docs"]["uses_sources"] is True
    assert tasks["impl"]["uses_sources"] is True
    assert set(tasks["impl"]["checks"]) == {"safety", "tests", "judge"}
    assert set(tasks["message"]["checks"]) == {"length", "judge"}
    assert tasks["message"]["uses_sources"] is False


@pytest.mark.parametrize("intent,options", [
    ("sourced_writing", {}),
    ("other", {"uses_sources": True}),
    ("other", {"checks": ["citations", "length", "judge"]}),
])
def test_requested_or_document_based_writing_retains_citations(intent, options):
    tasks = normalize_plan({"intent": intent, "tasks": [task("draft", **options)]},
                           "Summarize the supplied style guide with sources.")

    assert tasks[0]["uses_sources"] is True
    assert "citations" in tasks[0]["checks"]


class ValidationHub:
    """In-memory task storage with the production JSON, length and citation checks."""

    def __init__(self):
        self.calls = []
        self.tasks = {}
        self.artifacts = {}

    def tool_list_for_claude(self, allowed):
        return []

    async def call(self, name, arguments=None):
        args = arguments or {}
        self.calls.append((name, args))
        if name == "knowledge__list_documents":
            return {"documents": [{"id": "style-guide", "title": "Style guide", "words": 100}]}
        if name.startswith("validator__"):
            return getattr(validator, name.split("__", 1)[1])(**args)
        if name == "blackboard__add_task":
            self.tasks[args["task_id"]] = {**args, "status": "open"}
            return self.tasks[args["task_id"]]
        if name == "blackboard__claim_task":
            self.tasks[args["task_id"]].update(status="claimed", owner=args["agent"])
            return {"claimed": True, "task": self.tasks[args["task_id"]]}
        if name == "blackboard__finish_task":
            self.tasks[args["task_id"]]["status"] = "done" if args["success"] else "failed"
            return self.tasks[args["task_id"]]
        if name == "blackboard__save_artifact":
            self.artifacts[args["key"]] = args["content"]
            return args
        if name == "blackboard__post_message":
            return args
        raise AssertionError(f"Unexpected tool: {name}")


class WritingLLM:
    """Supply controlled work; the real orchestration and checks decide acceptance."""

    def __init__(self, intent, output, **task_options):
        self.plan = {"intent": intent, "tasks": [task("draft", **task_options)]}
        self.output = output
        self.prompts = []

    async def ask(self, name, system_prompt, messages, tools):
        prompt = messages[0]["content"]
        self.prompts.append((name, prompt))
        if name == "planner":
            return make_reply(json.dumps(self.plan))
        if name.startswith("reviewer:"):
            passed = "TODO" not in self.output
            return make_reply(json.dumps({"passed": passed, "score": 0.9 if passed else 0.2,
                                          "problems": [] if passed else ["The requested writing is unfinished."]}))
        return make_reply(self.output)


async def test_short_general_writing_passes_without_sources_and_reviewer_knows_the_policy():
    hub = ValidationHub()
    llm = WritingLLM("general_writing", "Thank you!", max_words=3)

    report = await run_goal("Write a thank-you message of at most three words.", llm, hub,
                            run_id="short-writing")

    assert report["status"] == "succeeded"
    assert report["final_output"] == "Thank you!"
    assert not any(name == "validator__check_citations" for name, _ in hub.calls)
    writer_prompts = [prompt for name, prompt in llm.prompts if name.startswith("writer:")]
    assert writer_prompts and all("[doc:style-guide]" not in prompt for prompt in writer_prompts)
    review_prompts = [prompt.lower() for name, prompt in llm.prompts if name.startswith("reviewer:")]
    assert len(review_prompts) == 2  # writer plus final answer
    assert all("citation policy: optional." in prompt for prompt in review_prompts)


async def test_explicitly_requested_sources_cannot_pass_without_citations():
    hub = ValidationHub()
    llm = WritingLLM("sourced_writing", "Use lowercase letters in readable URL slugs.")

    report = await run_goal("Summarize the style guide and cite your sources.", llm, hub,
                            run_id="missing-sources", max_tries=1)

    assert report["status"] == "failed"
    failed = next(iter(report["tasks"].values()))
    assert "no citations" in failed["error"]
    assert failed["attempts"][0]["results"][0]["check"] == "citations"
    assert not any(name.startswith("reviewer:") for name, _ in llm.prompts)


async def test_source_requirement_reaches_both_writer_and_final_reviewers():
    hub = ValidationHub()
    output = ("The style guide requires readable lowercase URL slugs, with words separated by "
              "hyphens and punctuation removed to keep addresses consistent [doc:style-guide].")
    llm = WritingLLM("sourced_writing", output)

    report = await run_goal("Summarize the supplied style guide with citations.", llm, hub,
                            run_id="sourced-review")

    assert report["status"] == "succeeded"
    review_prompts = [prompt.lower() for name, prompt in llm.prompts if name.startswith("reviewer:")]
    assert len(review_prompts) == 2
    assert all("citation policy: required." in prompt for prompt in review_prompts)
    assert sum(name == "validator__check_citations" for name, _ in hub.calls) == 2


@pytest.mark.parametrize("output,max_words,failed_check", [
    ("This answer has too many words.", 3, "length"),
    ("TODO: finish the requested paragraph.", 100, "judge"),
])
async def test_general_writing_still_rejects_length_and_quality_failures(output, max_words, failed_check):
    hub = ValidationHub()
    llm = WritingLLM("general_writing", output, max_words=max_words)

    report = await run_goal("Write a short paragraph.", llm, hub, run_id="rejected-writing", max_tries=1)

    assert report["status"] == "failed"
    failed = next(iter(report["tasks"].values()))
    result = failed["attempts"][0]["results"][-1]
    assert result["check"] == failed_check and result["passed"] is False
    assert not any(name == "validator__check_citations" for name, _ in hub.calls)


async def test_offline_general_paragraph_uses_only_a_writer_and_no_document_research():
    async with ToolHub() as hub:
        report = await run_goal("Write a paragraph about renewable energy.", FakeLLM(), hub,
                                run_id="general-paragraph")

    assert report["status"] == "succeeded"
    assert len(report["plan"]) == 1 and report["plan"][0]["role"] == "writer"
    assert set(report["plan"][0]["checks"]) == {"length", "judge"}
    assert "renewable" in report["final_output"].lower()
    assert "[doc:" not in report["final_output"]
    assert "\n\n" not in report["final_output"].strip()
    run_events = events.events_for("general-paragraph")
    assert not any(event["kind"] == "check" and event["check"] == "citations" for event in run_events)
    assert not any(event["kind"] == "tool_call" and event["tool"] in
                   {"knowledge__search", "knowledge__get_document"} for event in run_events)


async def test_offline_document_summary_still_researches_and_checks_its_sources():
    async with ToolHub() as hub:
        report = await run_goal("Summarize the URL style guide with sources.", FakeLLM(), hub,
                                run_id="document-summary")

    assert report["status"] == "succeeded"
    assert "[doc:style-guide]" in report["final_output"]
    run_events = events.events_for("document-summary")
    citation_checks = [event for event in run_events if event["kind"] == "check"
                       and event["check"] == "citations"]
    assert citation_checks and all(event["passed"] for event in citation_checks)
    assert any(event["kind"] == "tool_call" and event["tool"] == "knowledge__get_document"
               for event in run_events)
