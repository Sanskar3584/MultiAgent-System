"""Tests for the three MCP servers. We call the tool functions directly, like normal Python functions."""

import threading

import pytest

from servers import blackboard_server as bb
from servers import knowledge_server as kb
from servers import validator_server as v


@pytest.fixture(autouse=True)
def empty_board():
    bb.board = {"tasks": {}, "messages": [], "artifacts": {}}


# ---------------- blackboard

def test_task_waits_for_its_dependency():
    bb.add_task("a", "research")
    bb.add_task("b", "build", depends_on=["a"])
    assert bb.claim_task("w1")["task"]["id"] == "a"
    assert bb.claim_task("w2")["claimed"] is False      # b is blocked until a is done
    bb.finish_task("a", "w1", "notes")
    assert bb.claim_task("w2")["task"]["id"] == "b"


def test_only_the_owner_can_finish_a_task():
    bb.add_task("x", "something")
    bb.claim_task("alice")
    assert "error" in bb.finish_task("x", "bob", "not mine")


def test_two_agents_never_get_the_same_task():
    for i in range(20):
        bb.add_task(f"t{i}", f"task {i}")
    claimed = []

    def worker(name):
        while True:
            result = bb.claim_task(name)
            if not result["claimed"]:
                return
            claimed.append(result["task"]["id"])

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(f"t{i}" for i in range(20))  # each task exactly once


def test_unknown_dependency_is_rejected():
    assert "error" in bb.add_task("x", "y", depends_on=["ghost"])


# ---------------- validator

GOOD_CODE = {
    "solution.py": "def add(a, b):\n    return a + b\n",
    "test_solution.py": "from solution import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
}


def test_passing_tests():
    assert v.run_tests(GOOD_CODE)["passed"] is True


def test_failing_tests_say_which_test_failed():
    broken = {**GOOD_CODE, "solution.py": "def add(a, b):\n    return a - b\n"}
    result = v.run_tests(broken)
    assert result["passed"] is False
    assert "test_add" in result["problems"][0]


def test_dangerous_code_is_never_run():
    evil = {**GOOD_CODE, "solution.py": "import subprocess\nsubprocess.run(['rm', '-rf', '/'])\n"}
    result = v.run_tests(evil)
    assert result["passed"] is False and "not allowed" in result["problems"][0]


def test_infinite_loop_times_out():
    result = v.run_tests({"test_loop.py": "def test_loop():\n    while True:\n        pass\n"}, timeout_seconds=2)
    assert result["passed"] is False and "longer than" in result["problems"][0]


def test_check_json():
    schema = {"type": "object", "required": ["a"]}
    assert v.check_json('{"a": 1}', schema)["passed"]
    assert not v.check_json('{"b": 1}', schema)["passed"]
    assert not v.check_json("not json", schema)["passed"]


def test_made_up_citations_are_caught():
    assert v.check_citations("Slugs are lowercase [doc:style-guide].", ["style-guide"])["passed"]
    result = v.check_citations("Slugs are lowercase [doc:fake-doc].", ["style-guide"])
    assert not result["passed"] and "fake-doc" in result["problems"][0]


def test_check_length():
    assert "too long" in v.check_length("word " * 50, max_words=10)["problems"][0]
    assert v.check_length("word " * 30, max_words=100, must_include=["word"])["passed"]


# ---------------- knowledge

def test_search_finds_the_right_document():
    assert kb.search("slug hyphen rules")["results"][0]["id"] == "style-guide"
    assert kb.search("release tuesday freeze")["results"][0]["id"] == "release-process"
