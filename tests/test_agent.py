"""Visible progress and cancellation of model requests, with no external calls."""

import asyncio

import pytest

from core import agent, events
from core.llm import make_reply


class EmptyHub:
    def tool_list_for_claude(self, allowed):
        return []


def test_pending_model_is_visible_and_then_completes():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class WaitingLLM:
            async def ask(self, *args):
                entered.set()
                await release.wait()
                return make_reply("Finished.")

        pending = asyncio.create_task(agent.run_agent(
            "writer", "writer:paragraph", "Write.", WaitingLLM(), EmptyHub(), "model-progress-test"
        ))
        await entered.wait()
        assert events.events_for("model-progress-test")[-1]["status"] == "waiting"
        release.set()
        output, _ = await pending
        assert output == "Finished."
        assert [e["status"] for e in events.events_for("model-progress-test")] == ["waiting", "done"]

    asyncio.run(scenario())


def test_model_deadline_cancels_the_request_and_reports_failure(monkeypatch):
    monkeypatch.setattr(agent, "MODEL_TIMEOUT_SECONDS", 0.02)

    async def scenario():
        cancelled = asyncio.Event()

        class NeverResponds:
            async def ask(self, *args):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        with pytest.raises(TimeoutError, match="timed out"):
            await agent.run_agent("reviewer", "reviewer:research", "Review.", NeverResponds(),
                                  EmptyHub(), "model-timeout-test")
        assert cancelled.is_set()
        records = events.events_for("model-timeout-test")
        assert [e["status"] for e in records] == ["waiting", "failed"]
        assert "timed out" in records[-1]["detail"]

    asyncio.run(scenario())
