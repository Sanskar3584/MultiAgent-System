"""Tests for the pure-logic helpers: no servers or agents needed."""

from datetime import datetime, timezone

import pytest

from core.checks import get_code_files, make_feedback, plan_order_problems
from core.orchestrator import make_waves
from core.routines import check_routine, fill, next_run_time


def test_plan_with_a_circle_is_rejected():
    plan = {"tasks": [{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}]}
    assert "circle" in plan_order_problems(plan)[0]


def test_plan_with_unknown_dependency_is_rejected():
    plan = {"tasks": [{"id": "a", "depends_on": ["zzz"]}]}
    assert "unknown task zzz" in plan_order_problems(plan)[0]


def test_waves_put_tasks_after_their_dependencies():
    tasks = [{"id": "docs", "depends_on": ["impl", "research"]}, {"id": "impl", "depends_on": ["research"]},
             {"id": "research", "depends_on": []}, {"id": "other", "depends_on": []}]
    waves = [[t["id"] for t in wave] for wave in make_waves(tasks)]
    assert waves == [["research", "other"], ["impl"], ["docs"]]


def test_code_blocks_are_extracted():
    text = "Here:\n```python file=solution.py\nx = 1\n```\n```python file=test_solution.py\ndef test_x(): pass\n```"
    assert get_code_files(text) == {"solution.py": "x = 1\n", "test_solution.py": "def test_x(): pass\n"}


def test_feedback_lists_every_problem():
    results = [{"check": "tests", "passed": False, "problems": ["test_a failed", "test_b failed"]}]
    feedback = make_feedback(results, 1, 3)
    assert "[tests] test_a failed" in feedback and "[tests] test_b failed" in feedback


def test_templates():
    context = {"inputs": {"channel": "reports"}, "steps": {"a": {"output": "hello", "data": {"n": 2}}}}
    assert fill("send {{steps.a.output}} to {{inputs.channel}}", context) == "send hello to reports"
    assert fill({"x": ["{{steps.a.data}}"]}, context) == {"x": ['{"n": 2}']}


def test_routine_cannot_use_a_later_step():
    bad = {"name": "bad", "steps": [{"id": "a", "tool": "x", "args": {"t": "{{steps.b.output}}"}}, {"id": "b", "tool": "x"}]}
    with pytest.raises(ValueError):
        check_routine(bad)


def test_next_run_time_daily_on_weekdays():
    schedule = {"daily_at": "08:41", "days": ["mon", "tue", "wed", "thu", "fri"], "timezone": "Asia/Kolkata"}
    friday_evening = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)      # Fri 17:30 in India
    next_time = next_run_time(schedule, friday_evening)
    assert next_time.strftime("%a %H:%M") == "Mon 08:41"                     # skips the weekend


def test_next_run_time_every_few_minutes():
    now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    assert next_run_time({"every_minutes": 15}, now) == datetime(2026, 11, 16, 12, 15, tzinfo=timezone.utc)
