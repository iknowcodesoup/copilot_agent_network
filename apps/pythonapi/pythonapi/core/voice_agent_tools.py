"""Voice-pipeline tools the chat agent can call.

The agent reaches the star-trek-voyicer factory through the same gateway and
repository the /api/voice routes use, so a tool call and a REST call cannot
drift apart. Nothing here writes a run phase. VoiceRunReconciler stays the only
writer, exactly as it is for the routes.

Approving a review and retrying a failed run are deliberately absent. Both are
decisions a person makes, so they belong to a human-in-the-loop card in the
browser rather than to a model acting on its own. AWAITING_REVIEW is the one
transition this service asks a human for, and a tool here would take it away.

Every tool answers with a string, because that is what an AG-UI
ToolCallResultEvent carries and what the model reads back. A failure comes back
as one of those strings rather than as an exception: the model can then tell
the user what went wrong, where a raised error would end the whole run.

Each tool below is a single `@tool`-decorated function: the docstring's
summary and Args section are the only place its name, description, and
per-argument docs are written. `_build_tools()` closes each one over the
gateway and repository it needs, so nothing hand-duplicates a JSON Schema or a
handler dispatch table next to it.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from langchain_core.tools import BaseTool, tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from pythonapi.core.video_titles import resolve_video_titles
from pythonapi.core.voice_factory_gateway import (
    VoiceFactoryError,
    VoiceFactoryGateway,
)
from pythonapi.models.voice import VoiceRun, VoiceRunPhase
from pythonapi.repositories.voice_runs import VoiceRunRepository

logger = logging.getLogger(__name__)

TOOL_SEARCH_VIDEOS = "search_voice_videos"
TOOL_LIST_CHARACTERS = "list_voice_characters"
TOOL_LIST_RUNS = "list_voice_runs"
TOOL_GET_RUN = "get_voice_run"
TOOL_GET_TRAINING_PROGRESS = "get_voice_training_progress"
TOOL_START_RUN = "start_voice_run"

# Search and listing caps. A model that asks for everything would spend the
# context window on rows nobody reads, so the tool clamps rather than trusts.
MAX_SEARCH_RESULTS = 25
MAX_LISTED_RUNS = 25


def _build_tools(
    gateway: VoiceFactoryGateway, repository: VoiceRunRepository
) -> list[BaseTool]:
    """Build the six voice tools, closed over the gateway and repository.

    `parse_docstring=True` reads each tool's description and per-argument
    docs straight off its docstring, and checks every documented argument
    against the real signature, so a renamed parameter fails loudly here
    instead of drifting silently out of sync with what the model is told.
    """

    @tool(TOOL_SEARCH_VIDEOS, parse_docstring=True)
    async def search_voice_videos(query: str, limit: int = 10) -> str:
        """Search YouTube for source videos to train a voice model on.

        Downloads nothing. Use this to find a video id or url before starting a run.

        Args:
            query: What to search for, in plain words.
            limit: How many hits to return, 1 to 25.
        """
        videos = await gateway.search_videos(query, _clamp(limit, MAX_SEARCH_RESULTS))
        return _success({"videos": [video.model_dump() for video in videos]})

    @tool(TOOL_LIST_CHARACTERS, parse_docstring=True)
    async def list_voice_characters() -> str:
        """List the characters the voice factory already holds a dataset for.

        Use this before starting a run, to reuse a name rather than invent a new one.
        """
        return _success({"characters": await gateway.list_characters()})

    @tool(TOOL_LIST_RUNS, parse_docstring=True)
    async def list_voice_runs(limit: int = 10) -> str:
        """List voice runs, newest first, with the phase each has reached.

        Use this to answer 'what is running' or to find a run id.

        Args:
            limit: How many runs to return, 1 to 25.
        """
        runs = await repository.list_runs(limit=_clamp(limit, MAX_LISTED_RUNS))
        # one call names every video in the page, because the factory owns the
        # title and a run row carries only the id
        titles = await resolve_video_titles(gateway, [run.video_id for run in runs])
        # A summary, not the whole run: a full VoiceRun each would fill the
        # context with fields nobody asked about. get_voice_run has the rest.
        return _success(
            {
                "runs": [
                    {
                        "id": run.id,
                        "primary_character": run.primary_character,
                        "phase": run.phase.value,
                        "video_title": titles.get(run.video_id),
                        "error": run.error,
                    }
                    for run in runs
                ]
            }
        )

    @tool(TOOL_GET_RUN, parse_docstring=True)
    async def get_voice_run(run_id: str) -> str:
        """Read one voice run in full.

        Its phase, the video it came from, training progress, and any error.

        Args:
            run_id: The run id.
        """
        run = await repository.get_run(run_id)
        if run is None:
            return _failure(f"There is no run with id {run_id}.")
        return _success(run.model_dump(mode="json"))

    @tool(TOOL_GET_TRAINING_PROGRESS, parse_docstring=True)
    async def get_voice_training_progress(run_id: str) -> str:
        """Read live training progress for a run.

        The current epoch, the current loss, and the checkpoints written so far.

        Args:
            run_id: The run id.
        """
        run = await repository.get_run(run_id)
        if run is None:
            return _failure(f"There is no run with id {run_id}.")
        progress = await gateway.get_training_progress(run.primary_character)
        return _success(progress.model_dump(mode="json"))

    @tool(TOOL_START_RUN, parse_docstring=True)
    async def start_voice_run(
        primary_character: str,
        source_url: str,
        diarize: bool = True,
        num_speakers: int | None = None,
    ) -> str:
        """Start a voice run against one source video.

        This begins a pipeline that can run for days on the GPU host.

        Confirm the video and the character with the user before calling it.

        The run pauses for human review once the clips are ready.

        Args:
            primary_character: The dataset every unmapped speaker's clips land in.
            source_url: The full YouTube url of the source video.
            diarize: Split the audio by speaker. Leave true unless the video has
                one speaker throughout.
            num_speakers: How many speakers to expect, 1 to 20. Omit to let the
                pipeline decide.
        """
        # Resolving the video id is the only thing done inline, for the same
        # reason the route does it: it costs one request, downloads nothing,
        # and every later call needs the id to find the run's directory.
        video_id = await gateway.resolve_video_id(source_url)
        now = datetime.now(UTC)
        run = VoiceRun(
            id=uuid.uuid4().hex,
            primary_character=primary_character,
            source_url=source_url,
            video_id=video_id,
            phase=VoiceRunPhase.DOWNLOADING,
            diarize=diarize,
            num_speakers=num_speakers,
            created_at=now,
            updated_at=now,
        )
        await repository.create_run(run)
        return _success({"run_id": run.id, "phase": run.phase.value})

    return [
        search_voice_videos,
        list_voice_characters,
        list_voice_runs,
        get_voice_run,
        get_voice_training_progress,
        start_voice_run,
    ]


class VoiceToolRegistry:
    """Dispatches one tool call onto the voice factory.

    Builds its tools from the gateway and the repository rather than building
    those itself, because both are built once in main.py's lifespan and shared
    by every request.
    """

    def __init__(
        self,
        gateway: VoiceFactoryGateway,
        repository: VoiceRunRepository,
    ) -> None:
        self._tools: dict[str, BaseTool] = {
            tool_.name: tool_ for tool_ in _build_tools(gateway, repository)
        }
        self._schemas = [
            convert_to_openai_tool(tool_) for tool_ in self._tools.values()
        ]

    @property
    def schemas(self) -> list[dict[str, Any]]:
        """The tool definitions, in the shape the model gateway expects."""
        return self._schemas

    def handles(self, tool_name: str) -> bool:
        """True when this registry runs the tool, false when the browser does."""
        return tool_name in self._tools

    async def run(self, tool_name: str, arguments: str) -> str:
        """Run one tool call and return what the model should read back.

        `arguments` arrives as the raw JSON string the model produced, which is
        why malformed JSON is a normal outcome here rather than a bug.
        """
        tool_ = self._tools.get(tool_name)
        if tool_ is None:
            return _failure(f"There is no tool named {tool_name}.")

        try:
            parsed_arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as error:
            return _failure(f"Those arguments are not valid JSON: {error}")

        if not isinstance(parsed_arguments, dict):
            return _failure("Tool arguments must be a JSON object.")

        try:
            return await tool_.ainvoke(parsed_arguments)
        except ValidationError as error:
            # The model passed an argument the tool does not take. Telling it
            # so lets it correct the call on the next step.
            return _failure(f"Those arguments do not fit {tool_name}: {error}")
        except VoiceFactoryError as error:
            return _failure(f"The voice factory did not answer: {error}")
        except Exception as error:  # noqa: BLE001 - reported to the model, not raised
            logger.exception("Voice tool %s failed", tool_name)
            return _failure(f"{type(error).__name__}: {error}")


def _clamp(value: int, maximum: int) -> int:
    return max(1, min(int(value), maximum))


def _success(payload: dict[str, Any]) -> str:
    return json.dumps({"ok": True, **payload}, default=str)


def _failure(message: str) -> str:
    return json.dumps({"ok": False, "error": message})
