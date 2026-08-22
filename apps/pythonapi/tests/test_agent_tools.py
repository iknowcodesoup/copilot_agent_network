"""Tool-calling tests for the AG-UI chat agent.

Two things are under test and they fail differently. VoiceToolRegistry has to
turn every outcome into a string the model can read, including its own
failures. The agent loop has to emit the AG-UI tool events in the order the
CopilotKit client's state machine expects, and it has to know which side runs
which tool.
"""

import json

import pytest

from pythonapi.config import settings
from pythonapi.core import chat_agent
from pythonapi.core.voice_agent_tools import (
    TOOL_GET_RUN,
    TOOL_LIST_RUNS,
    TOOL_SEARCH_VIDEOS,
    TOOL_START_RUN,
    VoiceToolRegistry,
)
from pythonapi.core.voice_factory_gateway import VoiceFactoryError
from pythonapi.dependencies import get_voice_tool_registry
from pythonapi.main import app
from pythonapi.models.voice import VideoResult
from pythonapi.repositories.voice_runs import InMemoryVoiceRunRepository

FRONTEND_TOOL_NAME = "confirm_action"


# --------------------------------------------------------------------------
# Stand-ins for the model gateway. No test here reaches LiteLLM.
# --------------------------------------------------------------------------


class _FakeFunction:
    def __init__(self, name: str | None, arguments: str | None) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCallDelta:
    def __init__(
        self,
        index: int,
        tool_call_id: str | None = None,
        name: str | None = None,
        arguments: str | None = None,
    ) -> None:
        self.index = index
        self.id = tool_call_id
        self.function = _FakeFunction(name, arguments)


class _FakeDelta:
    def __init__(self, content=None, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeChunk:
    def __init__(self, content=None, tool_calls=None) -> None:
        self.choices = [
            type("Choice", (), {"delta": _FakeDelta(content, tool_calls)})()
        ]


class _FakeCompletions:
    """Replays one prepared response per create() call, in order."""

    def __init__(self, responses: list[list[_FakeChunk]], requests: list[dict]) -> None:
        self._responses = list(responses)
        self._requests = requests

    async def create(self, **kwargs):
        self._requests.append(kwargs)
        chunks = self._responses.pop(0) if self._responses else []

        async def stream():
            for chunk in chunks:
                yield chunk

        return stream()


class _FakeOpenAI:
    def __init__(self, responses: list[list[_FakeChunk]], requests: list[dict]) -> None:
        self.chat = type(
            "Chat", (), {"completions": _FakeCompletions(responses, requests)}
        )()

    async def close(self) -> None:
        return None


def _tool_call_response(name: str, arguments: str, tool_call_id: str = "call-1"):
    """One streamed step that asks for a tool, split across deltas like a
    real gateway sends it: the name once, then the arguments in pieces."""
    midpoint = len(arguments) // 2
    return [
        _FakeChunk(
            tool_calls=[_FakeToolCallDelta(0, tool_call_id, name, arguments[:midpoint])]
        ),
        _FakeChunk(tool_calls=[_FakeToolCallDelta(0, arguments=arguments[midpoint:])]),
    ]


def _text_response(text: str):
    return [_FakeChunk(content=text)]


def _run_input(content: str = "How is the Picard run doing?", tools=None) -> dict:
    return {
        "threadId": "thread-1",
        "runId": "run-1",
        "state": {},
        "messages": [{"id": "m1", "role": "user", "content": content}],
        "tools": tools or [],
        "context": [],
        "forwardedProps": {},
    }


def _parse_sse(body: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class _StubGateway:
    """Only the gateway methods the tools under test reach."""

    def __init__(self) -> None:
        self.resolved_urls: list[str] = []
        self.search_error: Exception | None = None

    async def search_videos(self, query: str, limit: int) -> list[VideoResult]:
        if self.search_error is not None:
            raise self.search_error
        return [
            VideoResult(
                video_id="abc123",
                title=f"{query} result",
                url="https://youtu.be/abc123",
            )
        ][:limit]

    async def resolve_video_id(self, url: str) -> str:
        self.resolved_urls.append(url)
        return "abc123"

    async def list_videos(self) -> list[dict]:
        # the factory owns the title, so listing runs asks it for the names
        return [{"video_id": "abc123", "title": "Picard speaks"}]


@pytest.fixture
def repository():
    return InMemoryVoiceRunRepository()


@pytest.fixture
def gateway():
    return _StubGateway()


@pytest.fixture
def registry(gateway, repository):
    return VoiceToolRegistry(gateway=gateway, repository=repository)


@pytest.fixture
def stub_llm(monkeypatch, registry):
    """Install a scripted model and hand the agent the real tool registry."""
    monkeypatch.setattr(settings, "LLM_API_KEY", "test-key")
    app.dependency_overrides[get_voice_tool_registry] = lambda: registry
    requests: list[dict] = []

    def install(responses: list[list[_FakeChunk]]):
        client = _FakeOpenAI(responses, requests)
        monkeypatch.setattr(chat_agent, "AsyncOpenAI", lambda **_: client)
        return requests

    return install


# --------------------------------------------------------------------------
# VoiceToolRegistry
# --------------------------------------------------------------------------


def test_registry_claims_only_its_own_tools(registry):
    assert registry.handles(TOOL_SEARCH_VIDEOS) is True
    # The browser owns anything the registry does not, so this has to be false
    # rather than an error: it is how the agent decides where a call goes.
    assert registry.handles(FRONTEND_TOOL_NAME) is False


def test_registry_publishes_a_schema_for_every_tool_it_handles(registry):
    for schema in registry.schemas:
        assert registry.handles(schema["function"]["name"])


@pytest.mark.asyncio
async def test_registry_reports_unparsable_arguments(registry):
    result = json.loads(await registry.run(TOOL_SEARCH_VIDEOS, "{not json"))

    # A model that produced bad JSON has to be told, not crashed on: the next
    # step is its chance to fix the call.
    assert result["ok"] is False
    assert "JSON" in result["error"]


@pytest.mark.asyncio
async def test_registry_reports_unknown_arguments(registry):
    result = json.loads(
        await registry.run(TOOL_SEARCH_VIDEOS, json.dumps({"nonsense": 1}))
    )

    assert result["ok"] is False
    assert TOOL_SEARCH_VIDEOS in result["error"]


@pytest.mark.asyncio
async def test_registry_reports_a_factory_failure_as_a_result(registry, gateway):
    gateway.search_error = VoiceFactoryError("connection refused")

    result = json.loads(
        await registry.run(TOOL_SEARCH_VIDEOS, json.dumps({"query": "picard"}))
    )

    # Raising here would end the whole run. As a result, the model can say
    # what went wrong.
    assert result["ok"] is False
    assert "connection refused" in result["error"]


@pytest.mark.asyncio
async def test_registry_clamps_an_oversized_search_limit(registry):
    result = json.loads(
        await registry.run(
            TOOL_SEARCH_VIDEOS, json.dumps({"query": "picard", "limit": 5000})
        )
    )

    assert result["ok"] is True


@pytest.mark.asyncio
async def test_start_run_creates_a_run_in_the_downloading_phase(
    registry, repository, gateway
):
    result = json.loads(
        await registry.run(
            TOOL_START_RUN,
            json.dumps(
                {
                    "primary_character": "picard",
                    "source_url": "https://youtu.be/abc123",
                }
            ),
        )
    )

    assert result["ok"] is True
    assert result["phase"] == "downloading"
    assert gateway.resolved_urls == ["https://youtu.be/abc123"]

    stored = await repository.get_run(result["run_id"])
    assert stored is not None
    assert stored.primary_character == "picard"
    assert stored.video_id == "abc123"


@pytest.mark.asyncio
async def test_get_run_reports_a_missing_run(registry):
    result = json.loads(
        await registry.run(TOOL_GET_RUN, json.dumps({"run_id": "gone"}))
    )

    assert result["ok"] is False


@pytest.mark.asyncio
async def test_list_runs_returns_summaries_not_whole_runs(registry, repository):
    await registry.run(
        TOOL_START_RUN,
        json.dumps(
            {"primary_character": "picard", "source_url": "https://youtu.be/abc123"}
        ),
    )

    result = json.loads(await registry.run(TOOL_LIST_RUNS, "{}"))

    assert result["ok"] is True
    assert len(result["runs"]) == 1
    # Deliberately narrow: a full VoiceRun each would fill the context with
    # fields nobody asked for. get_voice_run has the rest.
    assert set(result["runs"][0]) == {
        "id",
        "primary_character",
        "phase",
        "video_title",
        "error",
    }
    # the title is resolved from the factory at read time, because no
    # voice_runs column holds one
    assert result["runs"][0]["video_title"] == "Picard speaks"


# --------------------------------------------------------------------------
# The agent loop
# --------------------------------------------------------------------------


def test_agent_runs_a_backend_tool_and_answers_from_its_result(client, stub_llm):
    requests = stub_llm(
        [
            _tool_call_response(TOOL_SEARCH_VIDEOS, json.dumps({"query": "picard"})),
            _text_response("I found one video."),
        ]
    )

    response = client.post("/api/agent", json=_run_input())
    events = _parse_sse(response.text)

    assert [event["type"] for event in events] == [
        "RUN_STARTED",
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "TOOL_CALL_RESULT",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]

    # The args deltas have to rejoin into exactly what the model sent, because
    # the client renders the call from them.
    arguments = "".join(
        event["delta"] for event in events if event["type"] == "TOOL_CALL_ARGS"
    )
    assert json.loads(arguments) == {"query": "picard"}

    # The second model call must see the assistant's tool call and its result,
    # or the model has no idea the tool ever ran.
    second_call_messages = requests[1]["messages"]
    assert second_call_messages[-2]["tool_calls"][0]["function"]["name"] == (
        TOOL_SEARCH_VIDEOS
    )
    assert second_call_messages[-1]["role"] == "tool"
    assert json.loads(second_call_messages[-1]["content"])["ok"] is True


def test_agent_offers_its_tools_to_the_model(client, stub_llm):
    requests = stub_llm([_text_response("Hello")])

    client.post("/api/agent", json=_run_input())

    offered = {tool["function"]["name"] for tool in requests[0]["tools"]}
    assert TOOL_SEARCH_VIDEOS in offered
    assert TOOL_START_RUN in offered


def test_agent_hands_a_frontend_tool_back_to_the_browser(client, stub_llm):
    requests = stub_llm(
        [_tool_call_response(FRONTEND_TOOL_NAME, json.dumps({"run_id": "r1"}))]
    )

    response = client.post(
        "/api/agent",
        json=_run_input(
            tools=[
                {
                    "name": FRONTEND_TOOL_NAME,
                    "description": "Ask the operator to confirm.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]
        ),
    )
    events = [event["type"] for event in _parse_sse(response.text)]

    # The run ends at TOOL_CALL_END. Only the browser can answer, and AG-UI
    # carries no state between runs, so the result arrives on the next POST.
    assert events == [
        "RUN_STARTED",
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "RUN_FINISHED",
    ]
    assert "TOOL_CALL_RESULT" not in events
    # One model call only: nothing here can advance the run.
    assert len(requests) == 1

    offered = {tool["function"]["name"] for tool in requests[0]["tools"]}
    assert FRONTEND_TOOL_NAME in offered
    assert TOOL_SEARCH_VIDEOS in offered


def test_agent_ignores_a_frontend_tool_that_shadows_a_backend_name(client, stub_llm):
    requests = stub_llm([_text_response("Hello")])

    client.post(
        "/api/agent",
        json=_run_input(
            tools=[
                {
                    "name": TOOL_SEARCH_VIDEOS,
                    "description": "An impostor.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]
        ),
    )

    # One name must mean one thing, or the agent sends the call to the wrong
    # side of the wire.
    names = [tool["function"]["name"] for tool in requests[0]["tools"]]
    assert names.count(TOOL_SEARCH_VIDEOS) == 1


def test_agent_stops_after_the_tool_step_limit(client, stub_llm, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MAX_TOOL_STEPS", 2)
    call = _tool_call_response(TOOL_LIST_RUNS, "{}")
    requests = stub_llm([call, call])

    response = client.post("/api/agent", json=_run_input())
    events = _parse_sse(response.text)

    assert len(requests) == 2
    assert events[-1]["type"] == "RUN_FINISHED"
    # The transcript has to say why the agent stopped rather than go quiet.
    assert chat_agent.TOOL_STEP_LIMIT_MESSAGE in "".join(
        event.get("delta", "") for event in events
    )


def test_agent_runs_without_tools_when_the_voice_factory_is_absent(
    client, stub_llm, monkeypatch
):
    requests = stub_llm([_text_response("Hello")])
    app.dependency_overrides[get_voice_tool_registry] = lambda: None

    response = client.post("/api/agent", json=_run_input())

    # VOICE_FACTORY_URL is optional, so no registry means a plain chat agent,
    # not a broken one. An empty tools list is omitted: some gateways reject it.
    assert response.status_code == 200
    assert "tools" not in requests[0]
    assert [event["type"] for event in _parse_sse(response.text)] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
