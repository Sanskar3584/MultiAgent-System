"""Provider selection and Groq tool conversations, without external API calls."""

import asyncio
import copy
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core import llm


@pytest.fixture(autouse=True)
def isolated_provider_environment(monkeypatch):
    for name in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "AGENTMESH_PROVIDER", "AGENTMESH_MODEL", "DEMO_DELAY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def groq_client(monkeypatch):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())))
    monkeypatch.setitem(sys.modules, "groq", SimpleNamespace(AsyncGroq=Mock(return_value=client)))
    monkeypatch.setenv("GROQ_API_KEY", "test-key-only")
    return client


def response(text=None, tool_calls=None):
    message = SimpleNamespace(content=text, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if tool_calls else "stop")])


def tool_call(call_id, name, arguments):
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments=arguments))


@pytest.mark.parametrize("provider,groq_key,claude_key,expected", [
    (None, False, False, "offline"),
    (None, True, False, "groq"),
    (None, False, True, "anthropic"),
    (None, True, True, "groq"),
    ("groq", True, True, "groq"),
    ("anthropic", True, True, "anthropic"),
    ("offline", True, True, "offline"),
])
def test_provider_selection(monkeypatch, provider, groq_key, claude_key, expected):
    groq = SimpleNamespace(model="groq-test-model")
    claude = SimpleNamespace(model="claude-test-model")
    monkeypatch.setattr(llm, "GroqLLM", Mock(return_value=groq))
    monkeypatch.setattr(llm, "ClaudeLLM", Mock(return_value=claude))
    if provider is not None:
        monkeypatch.setenv("AGENTMESH_PROVIDER", provider)
    if groq_key:
        monkeypatch.setenv("GROQ_API_KEY", "test-key-only")
    if claude_key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-only")

    selected, label = llm.pick_llm()

    if expected == "offline":
        assert isinstance(selected, llm.FakeLLM)
        assert label == "offline demo team"
    else:
        assert selected is {"groq": groq, "anthropic": claude}[expected]
        assert selected.model in label


@pytest.mark.parametrize("provider,key_name", [
    ("groq", "GROQ_API_KEY"),
    ("anthropic", "ANTHROPIC_API_KEY"),
])
def test_explicit_provider_requires_its_key(monkeypatch, provider, key_name):
    monkeypatch.setenv("AGENTMESH_PROVIDER", provider)
    with pytest.raises(ValueError, match=key_name):
        llm.pick_llm()


def test_unknown_provider_is_not_silently_replaced_with_demo(monkeypatch):
    monkeypatch.setenv("AGENTMESH_PROVIDER", "typo")
    with pytest.raises(ValueError, match="(?i)provider"):
        llm.pick_llm()


def test_offline_argument_overrides_live_configuration(monkeypatch):
    monkeypatch.setenv("AGENTMESH_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key-only")
    selected, _ = llm.pick_llm(offline=True)
    assert isinstance(selected, llm.FakeLLM)


def test_live_request_without_keys_does_not_fall_back_to_demo():
    with pytest.raises(ValueError, match="GROQ_API_KEY|ANTHROPIC_API_KEY"):
        llm.pick_llm(offline=False)


def test_groq_model_can_be_overridden(monkeypatch, groq_client):
    assert llm.GroqLLM().model == "openai/gpt-oss-120b"
    monkeypatch.setenv("AGENTMESH_MODEL", "configured-model")
    assert llm.GroqLLM().model == "configured-model"
    assert llm.GroqLLM(model="explicit-model").model == "explicit-model"


@pytest.mark.parametrize("tool_text", [None, "I will read both documents."])
def test_groq_round_trips_parallel_tool_calls_and_error_results(groq_client, tool_text):
    tools = [{
        "name": "knowledge__get_document",
        "description": "Read a document.",
        "input_schema": {"type": "object", "properties": {"doc_id": {"type": "string"}}, "required": ["doc_id"]},
    }]
    messages = [{"role": "user", "content": "Read the style guide and the missing document."}]
    original = copy.deepcopy(messages)
    groq_client.chat.completions.create.side_effect = [
        response(tool_text, [
            tool_call("read-1", "knowledge__get_document", '{"doc_id":"style-guide"}'),
            tool_call("read-2", "knowledge__get_document", '{"doc_id":"missing"}'),
        ]),
        response("The style guide is available; the second document was not found."),
    ]
    adapter = llm.GroqLLM()

    async def conversation():
        first = await adapter.ask("researcher:research", "Use sources.", messages, tools)
        assert first["tool_calls"] == [
            {"id": "read-1", "name": "knowledge__get_document", "input": {"doc_id": "style-guide"}},
            {"id": "read-2", "name": "knowledge__get_document", "input": {"doc_id": "missing"}},
        ]
        assert first["text"] == (tool_text or "")
        if tool_text:
            assert first["content"][0] == {"type": "text", "text": tool_text}
        else:
            assert [block["type"] for block in first["content"]] == ["tool_use", "tool_use"]
        history = messages + [
            {"role": "assistant", "content": first["content"]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "read-1", "content": '{"text":"Use lowercase."}', "is_error": False},
                {"type": "tool_result", "tool_use_id": "read-2", "content": '{"error":"not found"}', "is_error": True},
            ]},
        ]
        return await adapter.ask("researcher:research", "Use sources.", history, tools)

    final = asyncio.run(conversation())

    assert messages == original  # conversion must not rewrite the stored conversation
    sent = groq_client.chat.completions.create.call_args.kwargs
    assert sent["model"] == adapter.model
    assert sent["tools"] == [{"type": "function", "function": {
        "name": tools[0]["name"], "description": tools[0]["description"], "parameters": tools[0]["input_schema"],
    }}]
    outgoing = sent["messages"]
    assert outgoing[0] == {"role": "system", "content": "Use sources."}
    assert outgoing[1] == messages[0]
    assert outgoing[2]["role"] == "assistant"
    assert outgoing[2]["content"] == tool_text
    calls = outgoing[2]["tool_calls"]
    assert [call["id"] for call in calls] == ["read-1", "read-2"]
    assert all(call["type"] == "function" for call in calls)
    assert [json.loads(call["function"]["arguments"]) for call in calls] == [
        {"doc_id": "style-guide"}, {"doc_id": "missing"},
    ]
    assert outgoing[3] == {"role": "tool", "tool_call_id": "read-1", "content": '{"text":"Use lowercase."}'}
    assert outgoing[4] == {"role": "tool", "tool_call_id": "read-2", "content": '{"error":"not found"}'}
    assert final == {
        "text": "The style guide is available; the second document was not found.",
        "tool_calls": [],
        "content": [{"type": "text", "text": "The style guide is available; the second document was not found."}],
    }


def test_groq_final_answer_request_has_no_tools(groq_client):
    groq_client.chat.completions.create.return_value = response("Done.")
    answer = asyncio.run(llm.GroqLLM().ask("combiner", "Combine the work.", [{"role": "user", "content": "Finish."}], []))
    assert answer["text"] == "Done."
    assert answer["tool_calls"] == []
    assert not groq_client.chat.completions.create.call_args.kwargs.get("tools")


@pytest.mark.parametrize("include_tool,include_text", [(False, True), (True, True), (False, False)])
def test_claude_keeps_the_shared_reply_format(monkeypatch, include_tool, include_text):
    blocks = []
    tools = []
    expected_calls = []
    expected_content = []
    if include_text:
        blocks.append(SimpleNamespace(type="text", text="Read "))
    if include_tool:
        blocks.append(SimpleNamespace(type="tool_use", id="read-1", name="knowledge__search", input={"query": "slugs"}))
        tools.append({"name": "knowledge__search", "input_schema": {"type": "object"}})
        expected_calls.append({"id": "read-1", "name": "knowledge__search", "input": {"query": "slugs"}})
    if include_text:
        blocks.append(SimpleNamespace(type="text", text="the rules."))
        expected_content.append({"type": "text", "text": "Read the rules."})
    if include_tool:
        expected_content.append({"type": "tool_use", **expected_calls[0]})

    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(content=blocks))))
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(AsyncAnthropic=Mock(return_value=client)))
    messages = [{"role": "user", "content": "Find the rules."}]
    original = copy.deepcopy(messages)

    reply = asyncio.run(llm.ClaudeLLM(model="test-model").ask("researcher", "Use documents.", messages, tools))

    assert reply == {"text": "Read the rules." if include_text else "",
                     "tool_calls": expected_calls, "content": expected_content}
    assert messages == original
    request = client.messages.create.call_args.kwargs
    assert request["model"] == "test-model" and request["max_tokens"] == 4096
    assert request.get("tools", []) == tools
    if not include_tool:
        assert "tools" not in request


@pytest.mark.parametrize("arguments", ["not json", "[]"])
def test_groq_rejects_invalid_tool_arguments(groq_client, arguments):
    groq_client.chat.completions.create.return_value = response(None, [tool_call("call-1", "knowledge__search", arguments)])
    with pytest.raises(ValueError, match="(?i)(argument|json|object)"):
        asyncio.run(llm.GroqLLM().ask("researcher", "Research.", [{"role": "user", "content": "Find rules."}], []))


class ProviderError(Exception):
    def __init__(self, status=400, code="tool_use_failed"):
        super().__init__("rejected reply")
        self.status_code = status
        self.body = {"error": {"code": code, "message": "Attempted to call unavailable tool json",
                               "failed_generation": "untrusted generated payload"}}


@pytest.mark.parametrize("role", ["planner", "reviewer:research"])
def test_groq_recovers_invalid_structured_reply_once(groq_client, role):
    verdict = '{"passed":false,"score":0.1,"problems":["No relevant sources"]}'
    groq_client.chat.completions.create.side_effect = [ProviderError(), response(verdict)]
    tools = [{"name": "knowledge__search", "description": "Search", "input_schema": {"type": "object"}}]
    history = [{"role": "user", "content": "Return JSON."}]
    reply = asyncio.run(llm.GroqLLM().ask(role, "Return JSON.", history, tools))
    assert reply["text"] == verdict  # a negative review is preserved, never promoted to a pass
    requests = groq_client.chat.completions.create.call_args_list
    assert len(requests) == 2
    assert requests[0].kwargs["tools"]
    retry = requests[1].kwargs
    assert retry["tool_choice"] == "none"
    assert retry["response_format"] == {"type": "json_object"}
    assert "tools" not in retry
    assert "untrusted generated payload" not in str(retry)
    assert history == [{"role": "user", "content": "Return JSON."}]


@pytest.mark.parametrize("role,status,code", [
    ("reviewer:research", 401, "invalid_api_key"),
    ("reviewer:research", 429, "rate_limit_exceeded"),
    ("reviewer:research", 400, "invalid_request"),
    ("researcher:research", 400, "tool_use_failed"),
])
def test_groq_does_not_retry_unrelated_errors(groq_client, role, status, code):
    error = ProviderError(status, code)
    groq_client.chat.completions.create.side_effect = error
    with pytest.raises(ProviderError):
        asyncio.run(llm.GroqLLM().ask(role, "Work.", [{"role": "user", "content": "Work."}], []))
    assert groq_client.chat.completions.create.call_count == 1


def test_groq_recovery_cannot_loop(groq_client):
    groq_client.chat.completions.create.side_effect = [ProviderError(), ProviderError()]
    with pytest.raises(ProviderError):
        asyncio.run(llm.GroqLLM().ask("reviewer:research", "Return JSON.", [], []))
    assert groq_client.chat.completions.create.call_count == 2


def test_model_error_keeps_provider_reason_without_generated_payload():
    message = llm.model_error_message(ProviderError())
    assert "HTTP 400" in message and "unavailable tool json" in message
    assert "untrusted generated payload" not in message


def test_groq_sdk_recovers_json_tool_error_with_local_transport():
    import httpx
    from groq import AsyncGroq

    async def scenario():
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(400, json={"error": {
                    "message": "attempted to call tool 'json' which was not in request.tools",
                    "type": "invalid_request_error", "code": "tool_use_failed",
                    "failed_generation": '{"name":"json","arguments":{"passed":true}}',
                }})
            return httpx.Response(200, json={
                "id": "local-recovery", "object": "chat.completion", "created": 0,
                "model": "openai/gpt-oss-120b", "choices": [{
                    "index": 0, "finish_reason": "stop", "message": {
                        "role": "assistant", "content": '{"passed":false,"score":0.1,"problems":["Missing sources"]}'
                    }
                }],
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            async with AsyncGroq(api_key="local-test-only", http_client=http, max_retries=0) as client:
                adapter = llm.GroqLLM.__new__(llm.GroqLLM)
                adapter.client, adapter.model = client, "openai/gpt-oss-120b"
                reply = await adapter.ask("reviewer:research", "Review the work and return JSON.",
                                          [{"role": "user", "content": "Review this paragraph."}], [])
        assert len(requests) == 2
        assert requests[1]["tool_choice"] == "none"
        assert json.loads(reply["text"])["passed"] is False

    asyncio.run(scenario())
