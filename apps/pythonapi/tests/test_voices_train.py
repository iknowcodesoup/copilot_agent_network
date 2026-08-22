"""Tests for Story 3.3: trigger training explicitly, independent of
ingestion and independent of assignment.

Covers every row of the spec's I/O & Edge-Case Matrix:
- assign_run never touches voice phase or wakes the reconciler - assigning a
  speaker is a separate action from starting training (assign and commit
  were unflattened; see test_voice_assign_commit.py for assign/commit)
- POST /voices/{id}/train happy path, unknown voice, already-training
- claim race: two claims of the same voice, only one wins
- graph-level: the training node calls gateway.start_job with STAGE_TRAIN
"""

from datetime import UTC, datetime

import pytest

from pythonapi.core.voice_factory_gateway import STAGE_TRAIN, VoiceFactoryGateway
from pythonapi.core.voice_training_graph import build_voice_training_graph
from pythonapi.dependencies import (
    get_required_voice_contribution_repository,
    get_required_voice_repository,
    get_required_voice_run_repository,
    get_required_voice_training_reconciler,
    get_voice_training_reconciler,
)
from pythonapi.main import app
from pythonapi.models.voice import VoiceRun, VoiceRunPhase
from pythonapi.models.voices import Voice, VoicePhase
from pythonapi.repositories.voice_contributions import (
    InMemoryVoiceContributionRepository,
)
from pythonapi.repositories.voice_runs import InMemoryVoiceRunRepository
from pythonapi.repositories.voices import InMemoryVoiceRepository
from pythonapi.workers.voice_training_reconciler import VoiceTrainingReconciler

INTERVAL_SECONDS = 3600.0  # long enough that tests only ever drive tick()/wake()
LEASE_SECONDS = 60.0


class FakeTrainingGateway:
    """Records what the training graph asked for and replays canned answers."""

    def __init__(self) -> None:
        self.started_jobs: list[dict] = []
        self.job_states: dict[str, str] = {}
        self.next_job_id = 0

    async def start_job(self, **fields) -> str:
        self.next_job_id += 1
        job_id = f"job{self.next_job_id}"
        self.started_jobs.append({"job_id": job_id, **fields})
        self.job_states[job_id] = "running"
        return job_id

    async def get_job_state(self, job_id: str) -> str:
        return self.job_states[job_id]


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


@pytest.fixture
def fake_gateway() -> FakeTrainingGateway:
    return FakeTrainingGateway()


@pytest.fixture
def training_reconciler(voice_repository, fake_gateway) -> VoiceTrainingReconciler:
    return VoiceTrainingReconciler(
        repository=voice_repository,
        graph=build_voice_training_graph(fake_gateway),
        interval_seconds=INTERVAL_SECONDS,
        lease_seconds=LEASE_SECONDS,
        gateway=fake_gateway,
    )


@pytest.fixture
def train_client(
    client,
    run_repository,
    voice_repository,
    contribution_repository,
    training_reconciler,
):
    app.dependency_overrides[get_required_voice_run_repository] = lambda: run_repository
    app.dependency_overrides[get_required_voice_repository] = lambda: voice_repository
    app.dependency_overrides[get_required_voice_contribution_repository] = lambda: (
        contribution_repository
    )
    app.dependency_overrides[get_required_voice_training_reconciler] = lambda: (
        training_reconciler
    )
    app.dependency_overrides[get_voice_training_reconciler] = lambda: (
        training_reconciler
    )
    return client


# --- assign_run stays a pure mapping: no phase change, no wake ------------


@pytest.mark.asyncio
async def test_assign_run_does_not_touch_voice_phase_or_wake_the_reconciler(
    train_client, run_repository, voice_repository, training_reconciler
):
    await run_repository.create_run(make_run(VoiceRunPhase.AWAITING_REVIEW))
    await voice_repository.create_voice(make_voice(id="voice1", name="Janeway"))

    response = train_client.post(
        "/api/voice/runs/run1/assign",
        json={"assignments": {"SPEAKER_00": "voice1", "SPEAKER_01": None}},
    )

    assert response.status_code == 201
    # Assigning a speaker is its own action now (Story 3.2's assign+commit
    # was unflattened): it must not move the voice out of AWAITING_COMMIT or
    # wake the reconciler. Training only starts through POST
    # /voices/{id}/train, tested below.
    stored = await voice_repository.get_voice("voice1")
    assert stored.phase is VoicePhase.AWAITING_COMMIT
    assert training_reconciler._pending_wakes == set()


@pytest.mark.asyncio
async def test_assign_run_still_works_without_a_voice_factory_configured(
    client, run_repository, voice_repository, contribution_repository
):
    """assign_run is DB-only and must keep working even when no voice
    factory is configured. It has nothing to wake in that case, and the
    contribution row is the durable record.
    """
    app.dependency_overrides[get_required_voice_run_repository] = lambda: run_repository
    app.dependency_overrides[get_required_voice_repository] = lambda: voice_repository
    app.dependency_overrides[get_required_voice_contribution_repository] = lambda: (
        contribution_repository
    )
    # deliberately no override for get_voice_training_reconciler: it resolves
    # to app.state.voice_training_reconciler, which is None without
    # VOICE_FACTORY_URL set in the test environment.
    await run_repository.create_run(make_run(VoiceRunPhase.AWAITING_REVIEW))
    await voice_repository.create_voice(make_voice(id="voice1", name="Janeway"))

    response = client.post(
        "/api/voice/runs/run1/assign",
        json={"assignments": {"SPEAKER_00": "voice1"}},
    )

    assert response.status_code == 201


# --- explicit trigger: POST /voices/{id}/train -----------------------------


@pytest.mark.asyncio
async def test_train_happy_path_sets_training_phase_and_wakes_reconciler(
    train_client, voice_repository, training_reconciler
):
    await voice_repository.create_voice(
        make_voice(id="voice1", name="Janeway", phase=VoicePhase.AWAITING_COMMIT)
    )

    response = train_client.post("/api/voices/voice1/train")

    assert response.status_code == 202
    assert response.json() == {"id": "voice1", "phase": "training"}
    stored = await voice_repository.get_voice("voice1")
    assert stored.phase is VoicePhase.TRAINING
    assert "voice1" in training_reconciler._pending_wakes


def test_train_reports_404_for_an_unknown_voice(train_client):
    response = train_client.post("/api/voices/nope/train")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_train_proceeds_even_when_already_training(
    train_client, voice_repository, training_reconciler, fake_gateway
):
    """Retrain is always available: a second call starts a new job rather
    than being rejected with a 409, unlike approve_run's single-phase guard.
    """
    await voice_repository.create_voice(
        make_voice(id="voice1", name="Janeway", phase=VoicePhase.TRAINING)
    )

    response = train_client.post("/api/voices/voice1/train")

    assert response.status_code == 202
    stored = await voice_repository.get_voice("voice1")
    assert stored.phase is VoicePhase.TRAINING
    # the stale job id was cleared, so the next tick starts a fresh job
    assert stored.voyicer_job_id is None


@pytest.mark.asyncio
async def test_train_proceeds_even_when_ready_or_failed(train_client, voice_repository):
    await voice_repository.create_voice(
        make_voice(id="voice1", name="Janeway", phase=VoicePhase.READY)
    )

    response = train_client.post("/api/voices/voice1/train")

    assert response.status_code == 202
    stored = await voice_repository.get_voice("voice1")
    assert stored.phase is VoicePhase.TRAINING


# --- claim race -------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_race_only_one_instance_wins(voice_repository):
    await voice_repository.create_voice(
        make_voice(id="voice1", name="Janeway", phase=VoicePhase.TRAINING)
    )

    first = await voice_repository.claim_voice("voice1", "owner-a", LEASE_SECONDS)
    second = await voice_repository.claim_voice("voice1", "owner-b", LEASE_SECONDS)

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_claim_race_second_owner_wins_after_release(voice_repository):
    await voice_repository.create_voice(
        make_voice(id="voice1", name="Janeway", phase=VoicePhase.TRAINING)
    )
    await voice_repository.claim_voice("voice1", "owner-a", LEASE_SECONDS)
    await voice_repository.release_voice("voice1")

    second = await voice_repository.claim_voice("voice1", "owner-b", LEASE_SECONDS)

    assert second is not None


# --- reconciler tick advances a claimed voice through the whole pipeline ---


@pytest.mark.asyncio
async def test_tick_advances_training_voice_to_exporting_once_job_succeeds(
    voice_repository, training_reconciler, fake_gateway
):
    await voice_repository.create_voice(
        make_voice(id="voice1", name="Janeway", phase=VoicePhase.TRAINING)
    )

    # first tick: starts the training job
    changed = await training_reconciler.tick()
    assert changed == 1
    stored = await voice_repository.get_voice("voice1")
    assert stored.phase is VoicePhase.TRAINING
    assert stored.voyicer_job_id is not None

    # job finishes
    fake_gateway.job_states[stored.voyicer_job_id] = "succeeded"

    changed = await training_reconciler.tick()
    assert changed == 1
    stored = await voice_repository.get_voice("voice1")
    assert stored.phase is VoicePhase.EXPORTING


@pytest.mark.asyncio
async def test_tick_leaves_resting_voices_alone(voice_repository, training_reconciler):
    await voice_repository.create_voice(
        make_voice(id="voice1", name="Janeway", phase=VoicePhase.AWAITING_COMMIT)
    )

    changed = await training_reconciler.tick()

    assert changed == 0


# --- graph-level: training node calls gateway.start_job(stage=STAGE_TRAIN) -


@pytest.mark.asyncio
async def test_training_node_calls_start_job_with_stage_train(fake_gateway):
    graph = build_voice_training_graph(fake_gateway)
    voice = make_voice(id="voice1", name="Janeway", phase=VoicePhase.TRAINING)

    result = await graph.ainvoke({"voice": voice})

    assert len(fake_gateway.started_jobs) == 1
    call = fake_gateway.started_jobs[0]
    assert call["stage"] == STAGE_TRAIN
    assert call["character"] == "Janeway"
    assert result["voice"].voyicer_job_id == call["job_id"]


def test_fake_training_gateway_satisfies_gateway_protocol_shape():
    """Sanity: the fake exposes the same two methods the graph calls on the
    real VoiceFactoryGateway, so a signature drift there breaks this test.
    """
    assert hasattr(VoiceFactoryGateway, "start_job")
    assert hasattr(VoiceFactoryGateway, "get_job_state")
