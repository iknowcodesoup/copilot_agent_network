"""Schemas for the voice-model pipeline.

A voice run tracks one source video from download through to an exported model.
The pipeline itself lives in the star-trek-voyicer repo; this service only
orchestrates it and holds the run state.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class VoiceRunPhase(StrEnum):
    """Where one run has reached.

    The reconciler advances a run through these. AWAITING_REVIEW is the
    human-in-the-loop pause: the run stops there until an operator approves the
    clips, for as long as that takes. READY and FAILED are terminal.
    """

    DOWNLOADING = "downloading"
    DIARIZING = "diarizing"
    AWAITING_REVIEW = "awaiting_review"
    COMMITTING = "committing"
    TRAINING = "training"
    EXPORTING = "exporting"
    READY = "ready"
    FAILED = "failed"
    # A run whose speaker->voice assignment has been turned into
    # voice_contributions rows. Terminal, like READY/FAILED, but reached
    # through routes/voice.py's assign_run rather than the reconciler.
    COMMITTED = "committed"


# Phases the reconciler leaves alone. AWAITING_REVIEW waits on a person, and the
# rest are terminal.
RESTING_PHASES = frozenset(
    {
        VoiceRunPhase.AWAITING_REVIEW,
        VoiceRunPhase.READY,
        VoiceRunPhase.FAILED,
        VoiceRunPhase.COMMITTED,
    }
)


class VideoResult(BaseModel):
    """One YouTube search hit."""

    video_id: str
    title: str
    duration_sec: float | None = None
    channel: str | None = None
    thumbnail_url: str | None = None
    url: str


class VideoSearchResponse(BaseModel):
    query: str
    videos: list[VideoResult] = Field(default_factory=list)


# A video and its speakers are the factory's own facts, so this service passes
# both through exactly as the factory shapes them. There is no model here on
# purpose: a field the factory adds must reach the browser with no edit in this
# repository, which a model would silently drop.


class VoiceRunRequest(BaseModel):
    """Start a run against one video."""

    # the dataset every unmapped speaker's clips land in
    primary_character: str = Field(min_length=1, max_length=64)
    source_url: str = Field(min_length=1)
    diarize: bool = True
    num_speakers: int | None = Field(default=None, ge=1, le=20)
    whisper_model: str | None = None
    min_clip_duration: float | None = Field(default=None, gt=0)
    max_clip_duration: float | None = Field(default=None, gt=0)


class VoiceRun(BaseModel):
    """One run's complete state.

    This is what the browser sees, so the lease columns are deliberately absent:
    they belong to the reconciler's mutual exclusion, not to the run. See
    VoiceRunRepository.claim_runs.
    """

    id: str
    primary_character: str
    source_url: str
    # the only join to the factory, which owns the video itself: its title,
    # its clips, and which speaker each clip belongs to
    video_id: str | None = None
    phase: VoiceRunPhase
    diarize: bool = True
    num_speakers: int | None = None
    # speaker label -> Voice id. This is DB-only and Voice-ID-scoped, and the
    # factory has no Voice concept, so nothing there mirrors it. Set by
    # POST .../assign, which stores this mapping and commits it - contribution
    # rows and phase change - in the same call.
    voice_assignments: dict[str, str | None] = Field(default_factory=dict)
    voyicer_job_id: str | None = None
    # which of DOWNLOADING's ordered ingest steps is in flight
    ingest_stage_index: int = 0
    # which of COMMITTING's three ordered stages is in flight
    commit_stage_index: int = 0
    # last training progress the factory reported, over the webhook
    current_epoch: int | None = None
    current_loss: float | None = None
    error: str | None = None
    # consecutive transient factory errors. A successful call resets it, and
    # only VOICE_MAX_CONSECUTIVE_ERRORS in a row fails the run.
    error_count: int = 0
    # the phase a failed run was in when it failed, so a retry can resume there
    failed_from_phase: VoiceRunPhase | None = None
    # the job that failed. Its log is the only record of why, so it outlives
    # voyicer_job_id, which is cleared to stop the next tick polling a dead job.
    failed_job_id: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def strip_timezone(cls, v: datetime) -> datetime:
        # If the datetime has timezone info, strip it
        if v.tzinfo is not None:
            return v.replace(tzinfo=None)
        return v


class VoiceRunResponse(BaseModel):
    """Acknowledgement for a newly started run."""

    id: str
    phase: VoiceRunPhase


class ClipSummary(BaseModel):
    clip_id: str
    keep: bool
    quality_score: float | None = None
    flagged: bool = False
    speaker_label: str | None = None
    speaker_coverage: float | None = None
    # Who this clip is for, chosen per clip by the reviewer. speaker_label is
    # what diarization heard; this is the decision made about it.
    assigned_voice: str | None = None
    duration_sec: float | None = None
    start_sec: float | None = None
    end_sec: float | None = None
    text: str = ""


class VideoClips(BaseModel):
    """One video's clips and its speaker map, as the factory returns them.

    The two arrive in the same payload, so they are read together. The map
    says which character each speaker label belongs to, and the factory owns
    it - speaker_map.json beside the clips is the one copy.
    """

    video_id: str
    # speaker label -> character name. None discards that speaker's clips.
    speaker_map: dict[str, str | None] = Field(default_factory=dict)
    clips: list[ClipSummary] = Field(default_factory=list)


class SpeakerGroup(BaseModel):
    """Every clip pyannote attributed to one speaker.

    speaker_label is None for the rejected group: clips no single speaker holds,
    which means cross-talk or music.
    """

    speaker_label: str | None
    assigned_character: str | None = None
    clip_count: int
    kept_count: int
    total_duration_sec: float
    clips: list[ClipSummary] = Field(default_factory=list)


class SpeakerBoard(BaseModel):
    """Clips grouped by speaker, for the review screen.

    Keyed on the video, because the clips are. run_id is None for a video no
    run has claimed yet, which a second character browsing an already
    ingested video is looking at.
    """

    video_id: str
    run_id: str | None = None
    speakers: list[SpeakerGroup] = Field(default_factory=list)


class ClipDecision(BaseModel):
    clip_id: str
    keep: bool | None = None
    speaker_label: str | None = None
    text: str | None = None


class ClipDecisionRequest(BaseModel):
    decisions: list[ClipDecision] = Field(min_length=1)


class SpeakerAssignmentRequest(BaseModel):
    """Approve the review and start training.

    Maps each speaker label to a character. A label mapped to None is discarded.
    """

    speaker_map: dict[str, str | None]


class CommitRequest(BaseModel):
    """Route reviewed clips from several videos to several characters in one
    call (FR14).

    Maps video_id to that video's {speaker_label: character} entries. A
    character of None discards that speaker's clips, same meaning as
    SpeakerAssignmentRequest. Distinct from SpeakerAssignmentRequest, which
    approves and commits a single run's own video.
    """

    assignments: dict[str, dict[str, str | None]] = Field(min_length=1)


class CommitResponse(BaseModel):
    """How many clips each named character's dataset gained."""

    committed: dict[str, int] = Field(default_factory=dict)


class CheckpointSummary(BaseModel):
    path: str
    name: str
    epoch: int | None = None
    step: int | None = None
    modified_at: datetime | None = None


class TrainingProgress(BaseModel):
    character: str
    preprocessed: bool = False
    running_job_id: str | None = None
    current_epoch: int | None = None
    current_loss: float | None = None
    checkpoints: list[CheckpointSummary] = Field(default_factory=list)


class JobLog(BaseModel):
    offset: int
    content: str
    state: str


class VoiceLogChunk(BaseModel):
    """New job-log content, pushed as it is produced."""

    run_id: str
    job_id: str
    offset: int
    content: str


class VoiceWebhookEventType(StrEnum):
    """What the factory is reporting."""

    STARTED = "started"
    PROGRESS = "progress"
    FINISHED = "finished"


class VoiceWebhookEvent(BaseModel):
    """What the voice factory posts when one of its jobs changes.

    Deliberately small. It says which job changed and what the factory saw; it
    never says what phase the run should move to. The reconciler decides that,
    by asking the factory itself.
    """

    job_id: str
    type: VoiceWebhookEventType
    # present on a progress event during training
    epoch: int | None = None
    loss: float | None = None
    # present on a finished event: the factory's own job state
    state: str | None = None
