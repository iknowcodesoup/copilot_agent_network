"""Tests for the voice pipeline: routes, reconciler, and phase transitions.

A FakeVoiceFactoryGateway stands in for the control API, so nothing here needs
the star-trek-voyicer repo, a GPU, or the network. Redis is fakeredis, which
runs real stream semantics in process.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from starlette.requests import Request

from pythonapi.config import settings
from pythonapi.core.video_titles import resolve_video_titles
from pythonapi.core.voice_events import (
    EVENT_RUN_UPDATED,
    STREAM_START,
    VoiceEventStream,
)
from pythonapi.core.voice_factory_gateway import (
    VoiceFactoryError,
    VoiceFactoryTransientError,
)
from pythonapi.core.voice_pipeline_graph import (
    build_voice_pipeline_graph,
    ingest_stages_for,
)
from pythonapi.dependencies import (
    get_required_voice_event_stream,
    get_required_voice_factory_gateway,
    get_required_voice_run_reconciler,
    get_required_voice_run_repository,
)
from pythonapi.infrastructure.redis_client import (
    build_blocking_redis_client,
    build_redis_client,
)
from pythonapi.main import app
from pythonapi.models.orm import VoiceRunRow
from pythonapi.models.voice import (
    ClipSummary,
    TrainingProgress,
    VideoClips,
    VideoResult,
    VoiceRun,
    VoiceRunPhase,
)
from pythonapi.repositories.voice_runs import (
    InMemoryVoiceRunRepository,
    _row_from_run,
    _run_from_row,
)
from pythonapi.routes.voice import get_voice_events
from pythonapi.workers.voice_run_reconciler import VoiceRunReconciler

MAX_ERRORS = 3
WEBHOOK_TOKEN = "test-webhook-token"


class FakeVoiceFactoryGateway:
    """Records what the pipeline asked for and replays canned answers."""

    def __init__(self) -> None:
        self.started_jobs: list[dict] = []
        self.cancelled_jobs: list[str] = []
        self.job_states: dict[str, str] = {}
        self.clips: list[ClipSummary] = []
        self.speaker_maps: list[tuple[str, dict]] = []
        self.clip_updates: list[list[dict]] = []
        # video_id each call actually received, so a regression that
        # reintroduces character-scoping on the real gateway shows up here
        self.get_clips_video_ids: list[str] = []
        self.update_clips_video_ids: list[str] = []
        self.stream_clip_audio_video_ids: list[str] = []
        # dicts, not models: the routes pass the factory's own payload
        # through, so a field the factory adds must survive the trip
        self.videos: list[dict] = []
        self.video_speakers: list[dict] = []
        self.speaker_map: dict[str, str | None] = {}
        self.clip_audio: bytes = b""
        self.next_job_id = 0
        self.fail_with: VoiceFactoryError | None = None
        self.commit_calls: list[dict] = []
        self.committed: dict[str, int] = {}

    def _guard(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    async def resolve_video_id(self, url: str) -> str:
        self._guard()
        return "vid_abc123"

    async def search_videos(self, query: str, limit: int) -> list[VideoResult]:
        self._guard()
        return [
            VideoResult(
                video_id="vid_abc123",
                title="Janeway speaks",
                duration_sec=91.0,
                channel="Delta Quadrant",
                url="https://www.youtube.com/watch?v=vid_abc123",
            )
        ][:limit]

    async def list_characters(self) -> list[str]:
        self._guard()
        return ["doctor", "janeway"]

    async def start_job(self, **fields) -> str:
        self._guard()
        self.next_job_id += 1
        job_id = f"job{self.next_job_id}"
        self.started_jobs.append({"job_id": job_id, **fields})
        self.job_states[job_id] = "running"
        return job_id

    async def get_job_state(self, job_id: str) -> str:
        self._guard()
        return self.job_states[job_id]

    async def get_job_logs(self, job_id: str, offset: int = 0) -> dict:
        self._guard()
        return {"offset": offset + 5, "content": "hello", "state": "running"}

    async def cancel_job(self, job_id: str) -> None:
        self._guard()
        self.cancelled_jobs.append(job_id)

    async def list_videos(self) -> list[dict]:
        self._guard()
        return list(self.videos)

    async def get_video_speakers(self, video_id: str) -> list[dict]:
        self._guard()
        return list(self.video_speakers)

    async def get_clips(self, video_id: str) -> VideoClips:
        self._guard()
        self.get_clips_video_ids.append(video_id)
        return VideoClips(
            video_id=video_id,
            speaker_map=dict(self.speaker_map),
            clips=list(self.clips),
        )

    async def update_clips(self, video_id: str, decisions: list[dict]) -> int:
        self._guard()
        self.update_clips_video_ids.append(video_id)
        self.clip_updates.append(decisions)
        by_clip_id = {clip.clip_id: clip for clip in self.clips}
        for decision in decisions:
            clip = by_clip_id.get(decision["clip_id"])
            if clip is None:
                continue
            if "keep" in decision:
                clip.keep = decision["keep"]
            if "speaker_label" in decision:
                clip.speaker_label = decision["speaker_label"]
        return len(decisions)

    async def set_speaker_map(self, video_id: str, speaker_map: dict) -> None:
        self._guard()
        self.speaker_maps.append((video_id, speaker_map))

    async def commit_clips(self, assignments: dict) -> dict:
        self._guard()
        self.commit_calls.append(assignments)
        return dict(self.committed)

    async def get_training_progress(self, character: str) -> TrainingProgress:
        self._guard()
        return TrainingProgress(
            character=character, preprocessed=True, current_epoch=42
        )

    def stream_clip_audio(self, video_id: str, clip_id: str):
        self._guard()
        self.stream_clip_audio_video_ids.append(video_id)
        return _FakeAudioStream(self.clip_audio)

    def finish_latest_job(self, state: str = "succeeded") -> None:
        self.job_states[self.started_jobs[-1]["job_id"]] = state


class _FakeAudioStream:
    """Stands in for the httpx streaming response get_clip_audio forwards."""

    def __init__(self, content: bytes) -> None:
        self._content = content

    async def __aenter__(self) -> "_FakeAudioStream":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, chunk_size: int):
        yield self._content


def clip(clip_id: str, speaker_label: str | None, keep: bool = True) -> ClipSummary:
    return ClipSummary(
        clip_id=clip_id,
        keep=keep,
        quality_score=22.5,
        speaker_label=speaker_label,
        speaker_coverage=1.0 if speaker_label else 0.4,
        duration_sec=3.0,
        text=f"line for {clip_id}",
    )


def make_run(phase: VoiceRunPhase, **overrides) -> VoiceRun:
    now = datetime.now(UTC)
    fields = {
        "id": "run1",
        "primary_character": "janeway",
        "source_url": "https://www.youtube.com/watch?v=vid_abc123",
        "video_id": "vid_abc123",
        "phase": phase,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(overrides)
    return VoiceRun(**fields)


@pytest.fixture
def gateway() -> FakeVoiceFactoryGateway:
    return FakeVoiceFactoryGateway()


@pytest.fixture
def repository() -> InMemoryVoiceRunRepository:
    return InMemoryVoiceRunRepository()


@pytest.fixture
def event_stream(fake_redis) -> VoiceEventStream:
    return VoiceEventStream(
        redis=fake_redis,
        blocking_redis=fake_redis,
        stream_key="test:voice:events",
        max_length=100,
    )


@pytest.fixture
def reconciler(gateway, repository, event_stream) -> VoiceRunReconciler:
    return VoiceRunReconciler(
        repository=repository,
        graph=build_voice_pipeline_graph(gateway),
        interval_seconds=0.01,
        event_stream=event_stream,
        lease_seconds=30.0,
        max_consecutive_errors=MAX_ERRORS,
        gateway=gateway,
    )


@pytest.fixture
def voice_client(client, gateway, repository, event_stream, reconciler):
    app.dependency_overrides[get_required_voice_factory_gateway] = lambda: gateway
    app.dependency_overrides[get_required_voice_run_repository] = lambda: repository
    app.dependency_overrides[get_required_voice_event_stream] = lambda: event_stream
    app.dependency_overrides[get_required_voice_run_reconciler] = lambda: reconciler
    return client


@pytest.fixture
def webhook_token(monkeypatch):
    """The webhook route reads the token off `settings` at request time."""
    monkeypatch.setattr(settings, "VOICE_WEBHOOK_TOKEN", WEBHOOK_TOKEN)
    return WEBHOOK_TOKEN


async def published_runs(event_stream: VoiceEventStream) -> list[VoiceRun]:
    return [event.data for event in await event_stream.read_after(STREAM_START, 100)]


# --- routes ---------------------------------------------------------------




def test_the_run_keyed_clip_routes_are_gone(voice_client):
    """Clips belong to the video. No caller may outlive its route."""
    assert voice_client.get("/api/voice/runs/run1/speakers").status_code == 404
    audio = voice_client.get("/api/voice/runs/run1/clips/clip_0001/audio")
    assert audio.status_code == 404



def test_start_run_resolves_the_video_and_returns_202(voice_client, repository):
    response = voice_client.post(
        "/api/voice/runs",
        json={
            "primary_character": "janeway",
            "source_url": "https://www.youtube.com/watch?v=vid_abc123",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["phase"] == VoiceRunPhase.DOWNLOADING

    stored = repository._runs[body["id"]]
    assert stored.video_id == "vid_abc123"
    assert stored.primary_character == "janeway"


def test_start_run_reports_502_when_the_factory_is_unreachable(voice_client, gateway):
    gateway.fail_with = VoiceFactoryError("connection refused")

    response = voice_client.post(
        "/api/voice/runs",
        json={"primary_character": "janeway", "source_url": "https://example.com/v"},
    )

    assert response.status_code == 502


def test_speaker_board_groups_clips_and_puts_rejects_last(voice_client, gateway):
    gateway.clips = [
        clip("clip_0001", "SPEAKER_01"),
        clip("clip_0002", "SPEAKER_00"),
        clip("clip_0003", None, keep=False),
        clip("clip_0004", "SPEAKER_00"),
    ]

    response = voice_client.get("/api/voice/videos/vid_abc123/clips")

    assert response.status_code == 200
    speakers = response.json()["speakers"]
    assert [group["speaker_label"] for group in speakers] == [
        "SPEAKER_00",
        "SPEAKER_01",
        None,
    ]
    assert speakers[0]["clip_count"] == 2
    assert speakers[0]["total_duration_sec"] == 6.0
    assert speakers[2]["kept_count"] == 0


def test_speaker_board_reads_a_video_no_run_has_claimed(voice_client, gateway):
    """The board is keyed on the video, so a person can review a video that
    no voice_runs row references at all."""
    gateway.clips = [clip("clip_0001", "SPEAKER_00")]

    response = voice_client.get("/api/voice/videos/vid_nobody_claimed/clips")

    assert response.status_code == 200
    body = response.json()
    assert body["video_id"] == "vid_nobody_claimed"
    assert body["run_id"] is None
    assert body["speakers"][0]["clip_count"] == 1


def test_speaker_board_names_the_character_from_the_factory_map(voice_client, gateway):
    """assigned_character comes from the factory's speaker_map.json, which is
    the one copy. Nothing here keeps a second one."""
    gateway.clips = [clip("clip_0001", "SPEAKER_00"), clip("clip_0002", "SPEAKER_01")]
    gateway.speaker_map = {"SPEAKER_00": "janeway", "SPEAKER_01": None}

    response = voice_client.get("/api/voice/videos/vid_abc123/clips")

    assert response.status_code == 200
    speakers = response.json()["speakers"]
    assert speakers[0]["assigned_character"] == "janeway"
    assert speakers[1]["assigned_character"] is None


@pytest.mark.asyncio
async def test_speaker_board_is_shared_across_characters_for_the_same_video(
    voice_client, gateway, repository
):
    """FR12: claiming an already-ingested video for a second character reads
    the same shared artifacts, so the call carries only the video id, never a
    character and never a run."""
    gateway.clips = [clip("clip_0001", "SPEAKER_00")]
    await repository.create_run(
        make_run(
            VoiceRunPhase.AWAITING_REVIEW, id="run_janeway", primary_character="janeway"
        )
    )
    await repository.create_run(
        make_run(
            VoiceRunPhase.AWAITING_REVIEW,
            id="run_chakotay",
            primary_character="chakotay",
        )
    )

    first = voice_client.get("/api/voice/videos/vid_abc123/clips")
    second = voice_client.get("/api/voice/videos/vid_abc123/clips")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["speakers"] == second.json()["speakers"]
    assert gateway.get_clips_video_ids == ["vid_abc123", "vid_abc123"]


@pytest.mark.asyncio
async def test_approve_writes_the_speaker_map_and_moves_to_committing(
    voice_client, gateway, repository
):
    await repository.create_run(make_run(VoiceRunPhase.AWAITING_REVIEW))

    response = voice_client.post(
        "/api/voice/runs/run1/approve",
        json={"speaker_map": {"SPEAKER_00": "janeway", "SPEAKER_01": None}},
    )

    assert response.status_code == 200
    assert response.json()["phase"] == VoiceRunPhase.COMMITTING
    assert gateway.speaker_maps == [
        ("vid_abc123", {"SPEAKER_00": "janeway", "SPEAKER_01": None})
    ]
    stored = await repository.get_run("run1")
    assert stored.phase is VoiceRunPhase.COMMITTING
    # the map went to the factory and nowhere else: this service keeps no copy
    assert not hasattr(stored, "speaker_map")


@pytest.mark.asyncio
async def test_approve_rejects_a_run_that_is_not_awaiting_review(
    voice_client, repository
):
    await repository.create_run(make_run(VoiceRunPhase.TRAINING))

    response = voice_client.post(
        "/api/voice/runs/run1/approve",
        json={"speaker_map": {"SPEAKER_00": "janeway"}},
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_approve_rejects_a_map_with_no_character(voice_client, repository):
    await repository.create_run(make_run(VoiceRunPhase.AWAITING_REVIEW))

    response = voice_client.post(
        "/api/voice/runs/run1/approve",
        json={"speaker_map": {"SPEAKER_00": None}},
    )

    assert response.status_code == 422


def test_get_run_reports_404_for_an_unknown_id(voice_client):
    assert voice_client.get("/api/voice/runs/nope").status_code == 404


@pytest.mark.asyncio
async def test_get_training_progress_stays_character_scoped(voice_client, repository):
    """get_training_progress has no video_id concept and keeps its own URL
    (FR13 does not apply here -- see the Spec's Boundaries & Constraints)."""
    await repository.create_run(make_run(VoiceRunPhase.TRAINING))

    response = voice_client.get("/api/voice/runs/run1/training")

    assert response.status_code == 200
    assert response.json()["character"] == "janeway"


@pytest.mark.asyncio
async def test_delete_run_cancels_its_job(voice_client, gateway, repository):
    await repository.create_run(make_run(VoiceRunPhase.TRAINING, voyicer_job_id="job7"))

    response = voice_client.delete("/api/voice/runs/run1")

    assert response.status_code == 204
    assert gateway.cancelled_jobs == ["job7"]
    assert await repository.get_run("run1") is None


# --- reconciler and phase transitions -------------------------------------


@pytest.mark.asyncio
async def test_reconciler_leaves_a_run_awaiting_review_alone(
    reconciler, repository, gateway
):
    await repository.create_run(make_run(VoiceRunPhase.AWAITING_REVIEW))

    changed = await reconciler.tick()

    assert changed == 0
    assert gateway.started_jobs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [VoiceRunPhase.READY, VoiceRunPhase.FAILED])
async def test_reconciler_leaves_terminal_runs_alone(
    reconciler, repository, gateway, phase
):
    await repository.create_run(make_run(phase))

    assert await reconciler.tick() == 0
    assert gateway.started_jobs == []


def test_ingest_stages_for_skips_diarize_when_not_requested():
    """Same rule commit_stage_index already applies to its three stages."""
    diarized = make_run(VoiceRunPhase.DOWNLOADING, diarize=True)
    plain = make_run(VoiceRunPhase.DOWNLOADING, diarize=False)

    assert ingest_stages_for(diarized) == (
        "youtube-download",
        "youtube-transcribe",
        "youtube-chunk",
        "youtube-diarize",
        "youtube-review",
    )
    assert ingest_stages_for(plain) == (
        "youtube-download",
        "youtube-transcribe",
        "youtube-chunk",
        "youtube-review",
    )


@pytest.mark.asyncio
async def test_downloading_walks_its_ingest_steps_in_order(
    reconciler, repository, gateway
):
    """One job per step, so a failure only costs the step that failed."""
    await repository.create_run(
        make_run(VoiceRunPhase.DOWNLOADING, diarize=True, num_speakers=3)
    )

    for _ in range(12):
        if gateway.started_jobs:
            gateway.finish_latest_job()
        await reconciler.tick()
        if (await repository.get_run("run1")).phase is not VoiceRunPhase.DOWNLOADING:
            break

    assert [job["stage"] for job in gateway.started_jobs] == [
        "youtube-download",
        "youtube-transcribe",
        "youtube-chunk",
        "youtube-diarize",
        "youtube-review",
    ]
    assert gateway.started_jobs[3]["num_speakers"] == 3

    stored = await repository.get_run("run1")
    assert stored.phase is VoiceRunPhase.DIARIZING
    assert stored.ingest_stage_index == 0

    # a second job for the same step must not start while the first is running
    await repository.create_run(make_run(VoiceRunPhase.DOWNLOADING, id="run2"))
    started_before = len(gateway.started_jobs)
    await reconciler.tick()
    await reconciler.tick()
    assert len(gateway.started_jobs) == started_before + 1


@pytest.mark.asyncio
async def test_a_full_run_reaches_ready(reconciler, repository, gateway):
    """Walk one run from download to an exported model."""
    gateway.clips = [clip("clip_0001", "SPEAKER_00")]
    await repository.create_run(make_run(VoiceRunPhase.DOWNLOADING))

    async def run_until(phase: VoiceRunPhase, max_ticks: int = 12) -> VoiceRun:
        for _ in range(max_ticks):
            current = await repository.get_run("run1")
            if current.phase is phase:
                return current
            if gateway.started_jobs:
                gateway.finish_latest_job()
            await reconciler.tick()
        stuck = await repository.get_run("run1")
        raise AssertionError(f"never reached {phase}, stuck at {stuck.phase}")

    # five ingest steps at two ticks apiece, plus the DIARIZING node's own
    # tick, leaves no room to spare in the default budget
    parked = await run_until(VoiceRunPhase.AWAITING_REVIEW, max_ticks=16)
    assert parked.phase is VoiceRunPhase.AWAITING_REVIEW

    # the human step: approve, which the reconciler never does on its own.
    # The speaker map goes to the factory, so nothing is set on the run here.
    parked.phase = VoiceRunPhase.COMMITTING
    await repository.update_run(parked)

    finished = await run_until(VoiceRunPhase.READY)
    assert finished.phase is VoiceRunPhase.READY

    stages = [job["stage"] for job in gateway.started_jobs]
    assert stages == [
        "youtube-download",
        "youtube-transcribe",
        "youtube-chunk",
        "youtube-diarize",
        "youtube-review",
        "youtube-commit",
        "resample",
        "preprocess",
        "train",
        "export",
    ]


@pytest.mark.asyncio
async def test_a_failed_job_fails_the_run_with_its_stage(
    reconciler, repository, gateway
):
    await repository.create_run(make_run(VoiceRunPhase.DOWNLOADING))
    await reconciler.tick()
    gateway.finish_latest_job("failed")

    await reconciler.tick()

    stored = await repository.get_run("run1")
    assert stored.phase is VoiceRunPhase.FAILED
    assert "failed" in stored.error


@pytest.mark.asyncio
async def test_a_permanent_factory_error_fails_the_run(reconciler, repository, gateway):
    """A 4xx means the request was wrong, so repeating it changes nothing."""
    await repository.create_run(make_run(VoiceRunPhase.DOWNLOADING))
    gateway.fail_with = VoiceFactoryError("422 unknown stage")

    await reconciler.tick()

    stored = await repository.get_run("run1")
    assert stored.phase is VoiceRunPhase.FAILED
    assert "unknown stage" in stored.error


@pytest.mark.asyncio
async def test_ingest_producing_no_clips_fails_the_run(reconciler, repository, gateway):
    gateway.clips = []
    await repository.create_run(make_run(VoiceRunPhase.DIARIZING))

    await reconciler.tick()

    stored = await repository.get_run("run1")
    assert stored.phase is VoiceRunPhase.FAILED
    assert "no clips" in stored.error


@pytest.mark.asyncio
async def test_committing_walks_its_three_stages_in_order(
    reconciler, repository, gateway
):
    await repository.create_run(make_run(VoiceRunPhase.COMMITTING))

    for _ in range(6):
        current = await repository.get_run("run1")
        if current.phase is VoiceRunPhase.TRAINING:
            break
        if gateway.started_jobs:
            gateway.finish_latest_job()
        await reconciler.tick()

    assert [job["stage"] for job in gateway.started_jobs] == [
        "youtube-commit",
        "resample",
        "preprocess",
    ]
    assert (await repository.get_run("run1")).phase is VoiceRunPhase.TRAINING


@pytest.mark.asyncio
async def test_a_run_deleted_mid_tick_is_dropped(reconciler, repository, gateway):
    await repository.create_run(make_run(VoiceRunPhase.DOWNLOADING))
    runs = await repository.list_active_runs()
    await repository.delete_run("run1")

    # the reconciler already holds a copy, as it would across an await
    assert await reconciler._advance(runs[0]) is False


# --- repository -----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_active_runs_excludes_resting_phases(repository):
    for index, phase in enumerate(
        [
            VoiceRunPhase.DOWNLOADING,
            VoiceRunPhase.AWAITING_REVIEW,
            VoiceRunPhase.TRAINING,
            VoiceRunPhase.READY,
            VoiceRunPhase.FAILED,
        ]
    ):
        await repository.create_run(make_run(phase, id=f"run{index}"))

    active = await repository.list_active_runs()

    assert {run.phase for run in active} == {
        VoiceRunPhase.DOWNLOADING,
        VoiceRunPhase.TRAINING,
    }


@pytest.mark.asyncio
async def test_update_run_reports_false_for_a_missing_run(repository):
    assert await repository.update_run(make_run(VoiceRunPhase.TRAINING)) is False


# --- transient failures ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_transient_error_holds_the_phase_and_counts(
    reconciler, repository, gateway
):
    """The GPU host can reboot mid-training. That must not kill the run."""
    await repository.create_run(make_run(VoiceRunPhase.TRAINING, voyicer_job_id="job7"))
    gateway.fail_with = VoiceFactoryTransientError("connection refused")

    await reconciler.tick()

    stored = await repository.get_run("run1")
    assert stored.phase is VoiceRunPhase.TRAINING
    assert stored.error_count == 1
    assert "connection refused" in stored.error


@pytest.mark.asyncio
async def test_a_successful_call_resets_the_error_count(
    reconciler, repository, gateway
):
    await repository.create_run(
        make_run(VoiceRunPhase.TRAINING, voyicer_job_id="job7", error_count=2)
    )
    gateway.job_states["job7"] = "running"

    await reconciler.tick()

    stored = await repository.get_run("run1")
    assert stored.error_count == 0
    assert stored.error is None


@pytest.mark.asyncio
async def test_a_run_fails_only_at_the_error_threshold(reconciler, repository, gateway):
    await repository.create_run(make_run(VoiceRunPhase.TRAINING, voyicer_job_id="job7"))
    gateway.fail_with = VoiceFactoryTransientError("connection refused")

    for _ in range(MAX_ERRORS - 1):
        await reconciler.tick()
    assert (await repository.get_run("run1")).phase is VoiceRunPhase.TRAINING

    await reconciler.tick()

    stored = await repository.get_run("run1")
    assert stored.phase is VoiceRunPhase.FAILED
    assert stored.failed_from_phase is VoiceRunPhase.TRAINING


@pytest.mark.asyncio
async def test_a_failed_run_records_the_phase_it_fell_over_in(
    reconciler, repository, gateway
):
    await repository.create_run(make_run(VoiceRunPhase.DOWNLOADING))
    await reconciler.tick()
    gateway.finish_latest_job("failed")

    await reconciler.tick()

    stored = await repository.get_run("run1")
    assert stored.phase is VoiceRunPhase.FAILED
    assert stored.failed_from_phase is VoiceRunPhase.DOWNLOADING


@pytest.mark.asyncio
async def test_a_failure_two_ingest_steps_in_keeps_its_place(
    voice_client, reconciler, repository, gateway
):
    """The bug this guards: retry must not fall back to re-downloading the
    video because one later step, like a missing ffmpeg, failed."""
    await repository.create_run(make_run(VoiceRunPhase.DOWNLOADING))
    await reconciler.tick()  # starts youtube-download
    gateway.finish_latest_job()
    await reconciler.tick()  # download succeeded, advances to youtube-transcribe
    await reconciler.tick()  # starts youtube-transcribe
    failed_job_id = gateway.started_jobs[-1]["job_id"]
    gateway.finish_latest_job("failed")

    await reconciler.tick()  # youtube-transcribe failed

    stored = await repository.get_run("run1")
    assert stored.phase is VoiceRunPhase.FAILED
    assert stored.ingest_stage_index == 1
    assert stored.failed_job_id == failed_job_id
    assert stored.voyicer_job_id is None
    assert "youtube-transcribe" in stored.error

    response = voice_client.post("/api/voice/runs/run1/retry")

    assert response.status_code == 200
    body = response.json()
    # DOWNLOADING, not youtube-download: the failed step lives one level
    # down, in ingest_stage_index, same as commit_stage_index does for
    # COMMITTING
    assert body["phase"] == VoiceRunPhase.DOWNLOADING
    assert body["ingest_stage_index"] == 1
    assert body["failed_job_id"] is None

    await reconciler.tick()
    assert gateway.started_jobs[-1]["stage"] == "youtube-transcribe"


@pytest.mark.asyncio
async def test_get_run_logs_falls_back_to_the_failed_job(voice_client, repository):
    """The running job id is gone once a run fails, but its log is the one
    thing that says why - so the route must not go blank."""
    await repository.create_run(
        make_run(VoiceRunPhase.FAILED, failed_job_id="dead-job")
    )

    response = voice_client.get("/api/voice/runs/run1/logs")

    assert response.status_code == 200
    assert response.json()["content"] == "hello"


@pytest.mark.asyncio
async def test_retry_restores_the_previous_phase(voice_client, repository):
    await repository.create_run(
        make_run(
            VoiceRunPhase.FAILED,
            failed_from_phase=VoiceRunPhase.TRAINING,
            voyicer_job_id="dead-job",
            error="the host went away",
            error_count=20,
        )
    )

    response = voice_client.post("/api/voice/runs/run1/retry")

    assert response.status_code == 200
    body = response.json()
    assert body["phase"] == VoiceRunPhase.TRAINING
    assert body["voyicer_job_id"] is None
    assert body["error"] is None
    assert body["error_count"] == 0
    assert body["failed_from_phase"] is None


@pytest.mark.asyncio
async def test_retry_rejects_a_run_that_did_not_fail(voice_client, repository):
    await repository.create_run(make_run(VoiceRunPhase.TRAINING))

    assert voice_client.post("/api/voice/runs/run1/retry").status_code == 409


# --- webhook ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_webhook_wakes_the_reconciler_without_touching_the_phase(
    voice_client, repository, reconciler, webhook_token
):
    await repository.create_run(make_run(VoiceRunPhase.TRAINING, voyicer_job_id="job7"))

    response = voice_client.post(
        "/api/voice/jobs/job7/events",
        json={"job_id": "job7", "type": "progress", "epoch": 42, "loss": 31.2},
        headers={"X-Voice-Factory-Token": webhook_token},
    )

    assert response.status_code == 204
    assert reconciler._pending_wakes == {"run1"}
    stored = await repository.get_run("run1")
    assert stored.phase is VoiceRunPhase.TRAINING
    assert stored.current_epoch == 42
    assert stored.current_loss == 31.2


@pytest.mark.asyncio
async def test_the_webhook_reports_204_for_an_unknown_job(
    voice_client, repository, reconciler, webhook_token
):
    """The factory runs jobs this service never started."""
    response = voice_client.post(
        "/api/voice/jobs/nobodys-job/events",
        json={"job_id": "nobodys-job", "type": "finished", "state": "succeeded"},
        headers={"X-Voice-Factory-Token": webhook_token},
    )

    assert response.status_code == 204
    assert reconciler._pending_wakes == set()


def test_the_webhook_rejects_a_bad_token(voice_client, webhook_token):
    response = voice_client.post(
        "/api/voice/jobs/job7/events",
        json={"job_id": "job7", "type": "started"},
        headers={"X-Voice-Factory-Token": "not-the-token"},
    )

    assert response.status_code == 401


def test_the_webhook_is_off_without_a_configured_token(voice_client, monkeypatch):
    monkeypatch.setattr(settings, "VOICE_WEBHOOK_TOKEN", None)

    response = voice_client.post(
        "/api/voice/jobs/job7/events",
        json={"job_id": "job7", "type": "started"},
    )

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_a_wake_reconciles_only_the_named_run(reconciler, repository, gateway):
    await repository.create_run(make_run(VoiceRunPhase.DOWNLOADING, id="wanted"))
    await repository.create_run(make_run(VoiceRunPhase.DOWNLOADING, id="ignored"))

    await reconciler.reconcile_run("wanted")

    assert [job.get("character") for job in gateway.started_jobs] == ["janeway"]
    assert (await repository.get_run("ignored")).voyicer_job_id is None


# --- leasing ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_instances_cannot_claim_the_same_run(repository):
    await repository.create_run(make_run(VoiceRunPhase.TRAINING))

    first = await repository.claim_runs("instance-a", lease_seconds=30)
    second = await repository.claim_runs("instance-b", lease_seconds=30)

    assert [run.id for run in first] == ["run1"]
    assert second == []


@pytest.mark.asyncio
async def test_a_released_run_can_be_claimed_again(repository):
    await repository.create_run(make_run(VoiceRunPhase.TRAINING))
    await repository.claim_runs("instance-a", lease_seconds=30)

    await repository.release_run("run1")

    assert [run.id for run in await repository.claim_runs("instance-b", 30)] == ["run1"]


@pytest.mark.asyncio
async def test_an_expired_lease_lets_another_instance_take_over(repository):
    """An API instance that dies mid-run must not strand it."""
    await repository.create_run(make_run(VoiceRunPhase.TRAINING))
    await repository.claim_runs("instance-that-died", lease_seconds=-1)

    assert [run.id for run in await repository.claim_runs("instance-b", 30)] == ["run1"]


# --- events ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_phase_change_publishes_the_complete_run(
    reconciler, repository, event_stream, gateway
):
    gateway.clips = [clip("clip_0001", "SPEAKER_00")]
    await repository.create_run(make_run(VoiceRunPhase.DIARIZING))

    await reconciler.tick()

    published = await published_runs(event_stream)
    assert [run.phase for run in published] == [VoiceRunPhase.AWAITING_REVIEW]
    assert published[0].id == "run1"


@pytest.mark.asyncio
async def test_events_replay_from_a_position(reconciler, repository, event_stream):
    run = make_run(VoiceRunPhase.TRAINING)
    first_id = await event_stream.publish(run)
    run.phase = VoiceRunPhase.EXPORTING
    await event_stream.publish(run)

    later = await event_stream.read_after(first_id, 10)

    assert [event.data.phase for event in later] == [VoiceRunPhase.EXPORTING]
    assert [event.event_type for event in later] == [EVENT_RUN_UPDATED]


@pytest.mark.asyncio
async def test_a_redis_failure_does_not_roll_back_postgres(
    reconciler, repository, gateway, fake_redis
):
    gateway.clips = [clip("clip_0001", "SPEAKER_00")]
    await repository.create_run(make_run(VoiceRunPhase.DIARIZING))
    await fake_redis.aclose()

    await reconciler.tick()

    assert (await repository.get_run("run1")).phase is VoiceRunPhase.AWAITING_REVIEW


@pytest.mark.asyncio
async def test_publishing_without_redis_is_a_no_op():
    stream = VoiceEventStream(
        redis=None, blocking_redis=None, stream_key="unused", max_length=10
    )

    assert await stream.publish(make_run(VoiceRunPhase.TRAINING)) is None
    assert await stream.current_position() == STREAM_START
    assert await stream.read_after(STREAM_START, 10) == []


def test_the_blocking_client_outlasts_the_heartbeat_window(monkeypatch):
    """The SSE tail read must not be cut off by its own socket timeout.

    This is the one invariant the rest of the suite cannot see: fakeredis has
    no socket, and the SSE tests shorten the heartbeat to milliseconds, so a
    socket timeout below the block window passes every other test and then
    times out every 15s in Docker.
    """
    monkeypatch.setattr(settings, "REDIS_URL", "redis://redis:6379/0")

    general = build_redis_client(settings)
    blocking = build_blocking_redis_client(
        settings, block_seconds=settings.VOICE_EVENT_HEARTBEAT_SECONDS
    )

    assert (
        blocking.connection_pool.connection_kwargs["socket_timeout"]
        > settings.VOICE_EVENT_HEARTBEAT_SECONDS
    )
    # The general client keeps the short timeout on purpose. Sending the tail
    # read over it is the bug this test exists to catch.
    assert (
        general.connection_pool.connection_kwargs["socket_timeout"]
        == settings.REDIS_SOCKET_TIMEOUT_SECONDS
    )


def test_no_redis_url_builds_no_client(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", None)

    assert build_redis_client(settings) is None
    assert build_blocking_redis_client(settings, block_seconds=15.0) is None


# --- SSE -------------------------------------------------------------------


@pytest.fixture
def fast_heartbeat(monkeypatch):
    """Drop the idle gap to milliseconds, so a heartbeat tick is not a wait."""
    monkeypatch.setattr(settings, "VOICE_EVENT_HEARTBEAT_SECONDS", 0.05)


def _request_with(headers: dict[str, str]) -> Request:
    """The route reads one thing off the request: Accept, for the encoder."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/voice/events",
            "headers": [
                (name.lower().encode(), value.encode())
                for name, value in headers.items()
            ],
        }
    )


@asynccontextmanager
async def open_event_stream(repository, event_stream, headers: dict | None = None):
    """Open the route's stream and hand back a "read one chunk" call.

    The route is called directly rather than over an HTTP client, because both
    clients buffer: TestClient's portal waits for the response task, and
    ASGITransport collects every chunk before it returns. This response only
    ends when the client goes away, so either one waits forever.

    A context manager rather than "read N chunks", so a test can publish while
    the stream is open. That ordering is the whole point: the route captures
    its stream position before it builds the snapshot, so an event published
    beforehand is already behind that position and never replays.
    """
    headers = headers or {}
    response = await get_voice_events(
        request=_request_with(headers),
        repository=repository,
        event_stream=event_stream,
        last_event_id=headers.get("Last-Event-ID"),
    )
    body = response.body_iterator

    async def next_chunk() -> str:
        """The next chunk that carries an event. Heartbeats do not count."""
        async for chunk in body:
            text = chunk if isinstance(chunk, str) else chunk.decode()
            if not text.startswith(":"):
                return text
        raise AssertionError("the event stream ended on its own")

    try:
        yield response, next_chunk
    finally:
        # Closing the generator is the browser going away.
        await body.aclose()


@pytest.mark.asyncio
async def test_the_event_stream_opens_with_a_snapshot(
    fast_heartbeat, repository, event_stream
):
    await repository.create_run(make_run(VoiceRunPhase.TRAINING))

    async with open_event_stream(repository, event_stream) as (response, next_chunk):
        snapshot = await next_chunk()

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert "STATE_SNAPSHOT" in snapshot
    assert '"run1"' in snapshot


@pytest.mark.asyncio
async def test_the_event_stream_sends_run_updates_with_sse_ids(
    fast_heartbeat, repository, event_stream
):
    await repository.create_run(make_run(VoiceRunPhase.TRAINING))

    async with open_event_stream(repository, event_stream) as (_response, next_chunk):
        await next_chunk()  # the snapshot every connection opens with
        event_id = await event_stream.publish(make_run(VoiceRunPhase.EXPORTING))
        update = await next_chunk()

    # the SSE id is the Redis Stream ID, which is what EventSource replays from
    assert f"id: {event_id}\n" in update
    assert EVENT_RUN_UPDATED in update
    assert '"exporting"' in update


@pytest.mark.asyncio
async def test_reconnecting_with_last_event_id_replays_what_was_missed(
    fast_heartbeat, repository, event_stream
):
    await repository.create_run(make_run(VoiceRunPhase.TRAINING))
    seen_id = await event_stream.publish(make_run(VoiceRunPhase.TRAINING))
    await event_stream.publish(make_run(VoiceRunPhase.READY))

    async with open_event_stream(
        repository, event_stream, headers={"Last-Event-ID": seen_id}
    ) as (_response, next_chunk):
        replayed = await next_chunk()

    # a reconnect resumes, so no snapshot, and only what came after seen_id
    assert "STATE_SNAPSHOT" not in replayed
    assert '"ready"' in replayed


# --- what the run table may hold -------------------------------------------


def test_voice_runs_keeps_no_fact_the_factory_owns():
    """CAP-1: one writer per fact. Every column left is something that would
    be lost if the factory's work/ directory were deleted."""
    columns = set(VoiceRunRow.__table__.columns.keys())

    assert columns.isdisjoint(
        {"video_title", "speaker_map", "clip_count", "approved_count"}
    )
    # the join to the factory, and the Voice map that has no factory twin
    assert "video_id" in columns
    assert VoiceRunRow.__table__.columns["video_id"].nullable
    assert "voice_assignments" in columns


def test_voice_assignments_survive_the_row_round_trip():
    """The one thing in this change that cannot be recomputed from the
    factory, so the migration must carry it."""
    run = make_run(
        VoiceRunPhase.AWAITING_REVIEW,
        voice_assignments={"SPEAKER_00": "voice1", "SPEAKER_01": None},
    )

    restored = _run_from_row(_row_from_run(run))

    assert restored.voice_assignments == {"SPEAKER_00": "voice1", "SPEAKER_01": None}
    assert restored.video_id == "vid_abc123"


# --- titles come from the factory ------------------------------------------


@pytest.mark.asyncio
async def test_a_title_resolves_from_the_factory_for_the_video_it_still_holds(gateway):
    gateway.videos = [{"video_id": "vid_abc123", "title": "Janeway speaks"}]

    titles = await resolve_video_titles(gateway, ["vid_abc123"])

    assert titles == {"vid_abc123": "Janeway speaks"}


@pytest.mark.asyncio
async def test_an_orphaned_run_resolves_no_title(gateway):
    """A run whose video the factory no longer lists is orphaned. It gets no
    invented name, which is what lets the videos view mark it."""
    gateway.videos = [{"video_id": "vid_abc123", "title": "Janeway speaks"}]

    titles = await resolve_video_titles(gateway, ["vid_gone"])

    assert titles == {}


@pytest.mark.asyncio
async def test_titles_degrade_to_nothing_when_the_factory_is_unreachable(gateway):
    """Postgres owns the run, so a listing must still answer. Only the name is
    lost. The videos view reads the factory directly and shows an error
    instead - it never falls back to a stored copy."""
    gateway.fail_with = VoiceFactoryError("connection refused")

    titles = await resolve_video_titles(gateway, ["vid_abc123"])

    assert titles == {}
