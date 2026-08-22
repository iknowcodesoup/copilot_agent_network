"""Schemas for the durable Voice entity (Story 3.1).

A Voice is the trained-model identity: independent of any single video, it
receives clip contributions from one or more voice runs (Story 3.2) and
tracks its own training phase, separate from a run's ingest phase
(VoiceRunPhase in models/voice.py).
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class VoicePhase(StrEnum):
    """Where one voice has reached in training.

    READY and FAILED are terminal. AWAITING_COMMIT is where every voice
    starts, since no contribution has committed clips to it yet.
    """

    AWAITING_COMMIT = "awaiting_commit"
    TRAINING = "training"
    EXPORTING = "exporting"
    READY = "ready"
    FAILED = "failed"


# Phases the training reconciler leaves alone. AWAITING_COMMIT waits on a
# contribution or an explicit train call; READY and FAILED are terminal.
# TRAINING/EXPORTING are the only claimable phases.
RESTING_PHASES = frozenset(
    {
        VoicePhase.AWAITING_COMMIT,
        VoicePhase.READY,
        VoicePhase.FAILED,
    }
)


class VoiceRequest(BaseModel):
    """Create a voice by name."""

    name: str = Field(min_length=1, max_length=64)


class VoiceContribution(BaseModel):
    """One video's clips, committed under one speaker label, into one voice.

    The audit trail Story 3.2 introduces (FR19): every row traces back to the
    run and video that produced it. Append-only - see
    repositories/voice_contributions.py.
    """

    id: str
    voice_id: str
    run_id: str
    video_id: str | None = None
    # not stored: the factory owns the title, so a reader resolves it from
    # video_id at read time -- see core/video_titles.py. None when the factory
    # is unset or no longer holds that video.
    video_title: str | None = None
    speaker_label: str
    created_at: datetime

    @field_validator("created_at", mode="after")
    @classmethod
    def strip_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is not None:
            return v.replace(tzinfo=None)
        return v


class Voice(BaseModel):
    """One voice's complete state.

    contributions is always empty until an assign call creates a
    contribution row for this voice.
    """

    id: str
    name: str
    phase: VoicePhase
    checkpoint_path: str | None = None
    # the control API job backing the current phase, if one is running.
    # Mirrors VoiceRun.voyicer_job_id: durable, so a restart mid-training
    # resumes polling the same job instead of starting a second one.
    voyicer_job_id: str | None = None
    contributions: list[VoiceContribution] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def strip_timezone(cls, v: datetime) -> datetime:
        # If the datetime has timezone info, strip it
        if v.tzinfo is not None:
            return v.replace(tzinfo=None)
        return v


class VoiceResponse(BaseModel):
    """Acknowledgement for a newly created voice."""

    id: str
    phase: VoicePhase


class RunAssignRequest(BaseModel):
    """Map a run's speaker labels to Voice ids and commit immediately.

    A voice id of None discards that speaker, same meaning as
    speaker_map's None. One call writes the mapping, creates one
    voice_contributions row per assigned speaker, and advances the run to
    COMMITTED - there is no separate draft state, so a repeat call on an
    already-committed run is rejected (409), unlike the old two-step flow's
    assign, which could be called more than once.
    """

    assignments: dict[str, str | None]


class RunAssignResponse(BaseModel):
    """What one assign call did: the mapping stored and the contribution
    rows it created in the same request."""

    run_id: str
    voice_assignments: dict[str, str | None]
    contributions: list[VoiceContribution] = Field(default_factory=list)
