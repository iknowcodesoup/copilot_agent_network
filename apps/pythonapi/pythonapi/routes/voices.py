"""HTTP layer for the durable Voice entity (Story 3.1).

Thin, like every other router here: it validates input and delegates to the
repository. No training or contribution logic lives here yet - that starts
in Story 3.2/3.3.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status

from pythonapi.core.video_titles import resolve_video_titles
from pythonapi.core.voice_factory_gateway import VoiceFactoryGateway
from pythonapi.dependencies import (
    get_required_voice_contribution_repository,
    get_required_voice_repository,
    get_required_voice_training_reconciler,
    get_voice_factory_gateway,
)
from pythonapi.models.voices import Voice, VoicePhase, VoiceRequest, VoiceResponse
from pythonapi.repositories.voice_contributions import VoiceContributionRepository
from pythonapi.repositories.voices import VoiceRepository
from pythonapi.workers.voice_training_reconciler import VoiceTrainingReconciler

router = APIRouter(prefix="/voices", tags=["Voices"])


async def _load_voice(repository: VoiceRepository, voice_id: str) -> Voice:
    voice = await repository.get_voice(voice_id)
    if voice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Voice not found")
    return voice


@router.post("", response_model=VoiceResponse, status_code=status.HTTP_201_CREATED)
async def create_voice(
    voice_request: VoiceRequest,
    repository: VoiceRepository = Depends(get_required_voice_repository),
):
    """Create a voice by name.

    Names are unique (FR22): the combobox (Story 3.5) and "fetch by name"
    both depend on a name uniquely identifying one voice, so a duplicate is
    rejected rather than creating a second row.
    """
    existing = await repository.get_voice_by_name(voice_request.name)
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A voice named {voice_request.name!r} already exists",
        )

    now = datetime.now(UTC)
    voice = Voice(
        id=uuid.uuid4().hex,
        name=voice_request.name,
        phase=VoicePhase.AWAITING_COMMIT,
        created_at=now,
        updated_at=now,
    )
    await repository.create_voice(voice)
    return VoiceResponse(id=voice.id, phase=voice.phase)


@router.get("", response_model=list[Voice])
async def search_voices(
    query: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=50),
    repository: VoiceRepository = Depends(get_required_voice_repository),
):
    """List or search voices by name, for the assign-speaker combobox
    (Story 3.5).

    An empty query matches every voice, same as search_videos's contract
    elsewhere in this service - the combobox opens with an empty query and
    shows something instead of nothing.
    """
    return await repository.search_voices(query, limit)


@router.get("/{voice_id}", response_model=Voice)
async def get_voice(
    voice_id: str,
    repository: VoiceRepository = Depends(get_required_voice_repository),
    contribution_repository: VoiceContributionRepository = Depends(
        get_required_voice_contribution_repository
    ),
    gateway: VoiceFactoryGateway | None = Depends(get_voice_factory_gateway),
):
    """One voice and the contributions committed into it.

    A contribution row records which video it came from, never what that video
    is called: the factory owns the title. It is resolved here, in one call
    for the whole list, and stays None when the factory is unset or no longer
    holds the video.
    """
    voice = await _load_voice(repository, voice_id)
    contributions = await contribution_repository.list_contributions_for_voice(
        voice_id
    )
    titles = await resolve_video_titles(
        gateway, [contribution.video_id for contribution in contributions]
    )
    for contribution in contributions:
        contribution.video_title = titles.get(contribution.video_id)
    voice.contributions = contributions
    return voice


@router.post(
    "/{voice_id}/train",
    response_model=VoiceResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def train_voice(
    voice_id: str,
    repository: VoiceRepository = Depends(get_required_voice_repository),
    training_reconciler: VoiceTrainingReconciler = Depends(
        get_required_voice_training_reconciler
    ),
):
    """Start training, on demand, whatever the voice's current phase.

    Retrain is always available (Story 3.3): unlike approve_run's single-
    allowed-phase 409 guard, this always sets TRAINING and wakes the
    reconciler, so an operator can kick off a fresh run even while one is
    already in flight. Only an unknown voice is rejected.
    """
    voice = await _load_voice(repository, voice_id)
    voice.phase = VoicePhase.TRAINING
    voice.voyicer_job_id = None
    await repository.update_voice(voice)
    training_reconciler.wake(voice_id)
    return VoiceResponse(id=voice.id, phase=voice.phase)
