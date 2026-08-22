"""Tests for the assign and commit actions, kept separate on purpose.

Assigning a speaker to a Voice only writes the contribution row - it does not
advance the run or touch the voice's training phase. Committing a run is a
separate call. Covers the happy path (single and multiple speakers/voices),
empty/all-null assignment, unknown voice, wrong phase, unknown run for
assign; the equivalent matrix for commit; plus GET /voices/{id} returning
real contributions after an assign with no commit.
"""

from datetime import UTC, datetime

import pytest

from pythonapi.dependencies import (
    get_required_voice_contribution_repository,
    get_required_voice_repository,
    get_required_voice_run_repository,
    get_voice_factory_gateway,
)
from pythonapi.main import app
from pythonapi.models.voice import VoiceRun, VoiceRunPhase
from pythonapi.models.voices import Voice, VoicePhase
from pythonapi.repositories.voice_contributions import (
    InMemoryVoiceContributionRepository,
)
from pythonapi.repositories.voice_runs import InMemoryVoiceRunRepository
from pythonapi.repositories.voices import InMemoryVoiceRepository


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


def make_voice(**overrides) -> Voice:
    now = datetime.now(UTC)
    fields = {
        "id": "voice1",
        "name": "Janeway",
        "phase": VoicePhase.AWAITING_COMMIT,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(overrides)
    return Voice(**fields)


@pytest.fixture
def run_repository() -> InMemoryVoiceRunRepository:
    return InMemoryVoiceRunRepository()


@pytest.fixture
def voice_repository() -> InMemoryVoiceRepository:
    return InMemoryVoiceRepository()


@pytest.fixture
def contribution_repository() -> InMemoryVoiceContributionRepository:
    return InMemoryVoiceContributionRepository()


class FakeTitleGateway:
    """Only what a title lookup needs. assign and get_voice ask the factory
    for the video's name and for nothing else."""

    def __init__(self) -> None:
        self.videos: list[dict] = [
            {"video_id": "vid_abc123", "title": "Janeway speaks"}
        ]

    async def list_videos(self) -> list[dict]:
        return list(self.videos)


@pytest.fixture
def title_gateway() -> FakeTitleGateway:
    return FakeTitleGateway()


@pytest.fixture
def assign_client(
    client, run_repository, voice_repository, contribution_repository, title_gateway
):
    app.dependency_overrides[get_required_voice_run_repository] = lambda: run_repository
    app.dependency_overrides[get_required_voice_repository] = lambda: voice_repository
    app.dependency_overrides[get_required_voice_contribution_repository] = lambda: (
        contribution_repository
    )
    app.dependency_overrides[get_voice_factory_gateway] = lambda: title_gateway
    return client


# --- happy path -------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_single_speaker_writes_one_contribution_and_nothing_else(
    assign_client, run_repository, voice_repository, contribution_repository
):
    await run_repository.create_run(make_run(VoiceRunPhase.AWAITING_REVIEW))
    await voice_repository.create_voice(make_voice(id="voice1", name="Janeway"))

    response = assign_client.post(
        "/api/voice/runs/run1/assign",
        json={"assignments": {"SPEAKER_00": "voice1"}},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["run_id"] == "run1"
    assert body["voice_assignments"] == {"SPEAKER_00": "voice1"}
    assert len(body["contributions"]) == 1
    contribution = body["contributions"][0]
    assert contribution["run_id"] == "run1"
    assert contribution["voice_id"] == "voice1"
    assert contribution["speaker_label"] == "SPEAKER_00"
    assert contribution["video_id"] == "vid_abc123"
    # resolved from the factory's meta.json, not from any voice_runs column
    assert contribution["video_title"] == "Janeway speaks"

    stored_run = await run_repository.get_run("run1")
    assert stored_run.phase is VoiceRunPhase.AWAITING_REVIEW
    assert stored_run.voice_assignments == {"SPEAKER_00": "voice1"}

    stored_voice = await voice_repository.get_voice("voice1")
    assert stored_voice.phase is VoicePhase.AWAITING_COMMIT

    voice1_contributions = await contribution_repository.list_contributions_for_voice(
        "voice1"
    )
    assert [row.speaker_label for row in voice1_contributions] == ["SPEAKER_00"]


@pytest.mark.asyncio
async def test_assign_multiple_speakers_multiple_voices_leaves_voices_untouched(
    assign_client, run_repository, voice_repository, contribution_repository
):
    await run_repository.create_run(make_run(VoiceRunPhase.AWAITING_REVIEW))
    await voice_repository.create_voice(make_voice(id="voice1", name="Janeway"))
    await voice_repository.create_voice(make_voice(id="voice2", name="Chakotay"))

    response = assign_client.post(
        "/api/voice/runs/run1/assign",
        json={
            "assignments": {
                "SPEAKER_00": "voice1",
                "SPEAKER_01": "voice2",
                "SPEAKER_02": None,
            }
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert len(body["contributions"]) == 2
    speaker_labels = {row["speaker_label"] for row in body["contributions"]}
    assert speaker_labels == {"SPEAKER_00", "SPEAKER_01"}

    stored_run = await run_repository.get_run("run1")
    assert stored_run.phase is VoiceRunPhase.AWAITING_REVIEW

    voice1 = await voice_repository.get_voice("voice1")
    voice2 = await voice_repository.get_voice("voice2")
    assert voice1.phase is VoicePhase.AWAITING_COMMIT
    assert voice2.phase is VoicePhase.AWAITING_COMMIT

    voice1_contributions = await contribution_repository.list_contributions_for_voice(
        "voice1"
    )
    voice2_contributions = await contribution_repository.list_contributions_for_voice(
        "voice2"
    )
    assert [row.speaker_label for row in voice1_contributions] == ["SPEAKER_00"]
    assert [row.speaker_label for row in voice2_contributions] == ["SPEAKER_01"]


# --- edge cases ---------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_rejects_an_empty_assignment_and_stores_nothing(
    assign_client, run_repository, contribution_repository
):
    await run_repository.create_run(make_run(VoiceRunPhase.AWAITING_REVIEW))

    response = assign_client.post(
        "/api/voice/runs/run1/assign",
        json={"assignments": {}},
    )

    assert response.status_code == 400
    stored = await run_repository.get_run("run1")
    assert stored.phase is VoiceRunPhase.AWAITING_REVIEW
    assert stored.voice_assignments == {}
    assert await contribution_repository.list_contributions_for_voice("voice1") == []


@pytest.mark.asyncio
async def test_assign_rejects_an_all_null_assignment_and_stores_nothing(
    assign_client, run_repository, contribution_repository
):
    await run_repository.create_run(make_run(VoiceRunPhase.AWAITING_REVIEW))

    response = assign_client.post(
        "/api/voice/runs/run1/assign",
        json={"assignments": {"SPEAKER_00": None}},
    )

    assert response.status_code == 400
    stored = await run_repository.get_run("run1")
    assert stored.phase is VoiceRunPhase.AWAITING_REVIEW
    assert stored.voice_assignments == {}
    assert await contribution_repository.list_contributions_for_voice("voice1") == []


@pytest.mark.asyncio
async def test_assign_rejects_an_unknown_voice_id_and_stores_nothing(
    assign_client, run_repository, voice_repository, contribution_repository
):
    await run_repository.create_run(make_run(VoiceRunPhase.AWAITING_REVIEW))
    await voice_repository.create_voice(make_voice(id="voice1", name="Janeway"))

    response = assign_client.post(
        "/api/voice/runs/run1/assign",
        json={"assignments": {"SPEAKER_00": "voice1", "SPEAKER_01": "nope"}},
    )

    assert response.status_code == 404
    stored = await run_repository.get_run("run1")
    assert stored.phase is VoiceRunPhase.AWAITING_REVIEW
    assert stored.voice_assignments == {}
    assert await contribution_repository.list_contributions_for_voice("voice1") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        VoiceRunPhase.DOWNLOADING,
        VoiceRunPhase.DIARIZING,
        VoiceRunPhase.COMMITTING,
        VoiceRunPhase.TRAINING,
        VoiceRunPhase.READY,
        VoiceRunPhase.FAILED,
        VoiceRunPhase.COMMITTED,
    ],
)
async def test_assign_rejects_a_run_that_is_not_awaiting_review(
    assign_client, run_repository, contribution_repository, phase
):
    await run_repository.create_run(make_run(phase))

    response = assign_client.post(
        "/api/voice/runs/run1/assign",
        json={"assignments": {"SPEAKER_00": "voice1"}},
    )

    assert response.status_code == 409
    assert await contribution_repository.list_contributions_for_voice("voice1") == []


def test_assign_reports_404_for_an_unknown_run(assign_client):
    response = assign_client.post(
        "/api/voice/runs/nope/assign",
        json={"assignments": {"SPEAKER_00": "voice1"}},
    )

    assert response.status_code == 404


# --- commit -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_advances_an_assigned_run_to_committed(
    assign_client, run_repository, voice_repository
):
    await run_repository.create_run(
        make_run(
            VoiceRunPhase.AWAITING_REVIEW,
            voice_assignments={"SPEAKER_00": "voice1"},
        )
    )
    await voice_repository.create_voice(make_voice(id="voice1", name="Janeway"))

    response = assign_client.post("/api/voice/runs/run1/commit")

    assert response.status_code == 200
    assert response.json()["phase"] == "committed"

    stored_run = await run_repository.get_run("run1")
    assert stored_run.phase is VoiceRunPhase.COMMITTED

    stored_voice = await voice_repository.get_voice("voice1")
    assert stored_voice.phase is VoicePhase.AWAITING_COMMIT


@pytest.mark.asyncio
async def test_commit_rejects_a_run_with_no_assignments(
    assign_client, run_repository
):
    await run_repository.create_run(make_run(VoiceRunPhase.AWAITING_REVIEW))

    response = assign_client.post("/api/voice/runs/run1/commit")

    assert response.status_code == 400
    stored = await run_repository.get_run("run1")
    assert stored.phase is VoiceRunPhase.AWAITING_REVIEW


@pytest.mark.asyncio
async def test_commit_rejects_a_run_with_only_null_assignments(
    assign_client, run_repository
):
    await run_repository.create_run(
        make_run(VoiceRunPhase.AWAITING_REVIEW, voice_assignments={"SPEAKER_00": None})
    )

    response = assign_client.post("/api/voice/runs/run1/commit")

    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        VoiceRunPhase.DOWNLOADING,
        VoiceRunPhase.DIARIZING,
        VoiceRunPhase.COMMITTING,
        VoiceRunPhase.TRAINING,
        VoiceRunPhase.READY,
        VoiceRunPhase.FAILED,
        VoiceRunPhase.COMMITTED,
    ],
)
async def test_commit_rejects_a_run_that_is_not_awaiting_review(
    assign_client, run_repository, phase
):
    await run_repository.create_run(
        make_run(phase, voice_assignments={"SPEAKER_00": "voice1"})
    )

    response = assign_client.post("/api/voice/runs/run1/commit")

    assert response.status_code == 409


def test_commit_reports_404_for_an_unknown_run(assign_client):
    response = assign_client.post("/api/voice/runs/nope/commit")

    assert response.status_code == 404


# --- fetch voice after assign ---------------------------------------------


@pytest.mark.asyncio
async def test_get_voice_lists_real_contributions_after_an_assign_with_no_commit(
    assign_client, run_repository, voice_repository
):
    await run_repository.create_run(make_run(VoiceRunPhase.AWAITING_REVIEW))
    await voice_repository.create_voice(make_voice(id="voice1", name="Janeway"))
    assign_client.post(
        "/api/voice/runs/run1/assign",
        json={"assignments": {"SPEAKER_00": "voice1"}},
    )

    response = assign_client.get("/api/voices/voice1")

    assert response.status_code == 200
    body = response.json()
    assert len(body["contributions"]) == 1
    contribution = body["contributions"][0]
    assert contribution["run_id"] == "run1"
    assert contribution["video_id"] == "vid_abc123"
    # resolved from the factory's meta.json, not from any voice_runs column
    assert contribution["video_title"] == "Janeway speaks"
    assert contribution["speaker_label"] == "SPEAKER_00"
    assert contribution["voice_id"] == "voice1"
    assert contribution["created_at"]


@pytest.mark.asyncio
async def test_assign_still_records_a_contribution_without_a_voice_factory(
    client, run_repository, voice_repository, contribution_repository
):
    """The contribution rows are Postgres's own record, so assigning must keep
    working with VOICE_FACTORY_URL unset. Only the title is lost."""
    app.dependency_overrides[get_required_voice_run_repository] = lambda: run_repository
    app.dependency_overrides[get_required_voice_repository] = lambda: voice_repository
    app.dependency_overrides[get_required_voice_contribution_repository] = lambda: (
        contribution_repository
    )
    app.dependency_overrides[get_voice_factory_gateway] = lambda: None
    await run_repository.create_run(make_run(VoiceRunPhase.AWAITING_REVIEW))
    await voice_repository.create_voice(make_voice(id="voice1", name="Janeway"))

    response = client.post(
        "/api/voice/runs/run1/assign",
        json={"assignments": {"SPEAKER_00": "voice1"}},
    )

    assert response.status_code == 201
    contribution = response.json()["contributions"][0]
    assert contribution["video_id"] == "vid_abc123"
    assert contribution["video_title"] is None


@pytest.mark.asyncio
async def test_a_second_character_reading_the_same_video_sees_the_same_title(
    assign_client, run_repository, voice_repository, title_gateway
):
    """CAP-5: the title belongs to the video, so every run that points at the
    same video reads the one name the factory holds."""
    await run_repository.create_run(make_run(VoiceRunPhase.AWAITING_REVIEW))
    await run_repository.create_run(
        make_run(
            VoiceRunPhase.AWAITING_REVIEW,
            id="run2",
            primary_character="chakotay",
        )
    )
    await voice_repository.create_voice(make_voice(id="voice1", name="Janeway"))
    await voice_repository.create_voice(make_voice(id="voice2", name="Chakotay"))

    first = assign_client.post(
        "/api/voice/runs/run1/assign",
        json={"assignments": {"SPEAKER_00": "voice1"}},
    )
    second = assign_client.post(
        "/api/voice/runs/run2/assign",
        json={"assignments": {"SPEAKER_01": "voice2"}},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert (
        first.json()["contributions"][0]["video_title"]
        == second.json()["contributions"][0]["video_title"]
        == "Janeway speaks"
    )


@pytest.mark.asyncio
async def test_get_voice_still_returns_empty_contributions_before_any_assign(
    assign_client, voice_repository
):
    await voice_repository.create_voice(make_voice(id="voice1", name="Janeway"))

    response = assign_client.get("/api/voices/voice1")

    assert response.status_code == 200
    assert response.json()["contributions"] == []
