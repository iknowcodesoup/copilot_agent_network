"""SQLAlchemy ORM table definitions - the Postgres schema for document/chunk
metadata and orders. Chunk embedding vectors are out of scope here; those
live in Qdrant only (see repositories/qdrant.py).
"""

from datetime import datetime

from sqlalchemy import ARRAY, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DocumentRow(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str]
    filename: Mapped[str]
    raw_content: Mapped[bytes]
    # Docling-extracted full text, PII-masked. Empty until PROCESSING completes.
    content: Mapped[str] = mapped_column(default="")
    status: Mapped[str]
    chunk_count: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None]
    created_at: Mapped[datetime]


class ChunkRow(Base):
    __tablename__ = "chunks"
    __table_args__ = (Index("idx_chunks_document_id", "document_id"),)

    id: Mapped[str] = mapped_column(primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE")
    )
    chunk_index: Mapped[int]
    text: Mapped[str]
    headings: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    page_no: Mapped[int | None]


class PiiVaultRow(Base):
    """Persisted, encrypted PII vault: surrogate token -> real value. Values
    are Fernet-encrypted before storage (see repositories/pii_vault.py) -
    plaintext PII never touches this schema.
    """

    __tablename__ = "pii_vault"
    __table_args__ = (Index("idx_pii_vault_entity_type", "entity_type"),)

    token: Mapped[str] = mapped_column(primary_key=True)
    entity_type: Mapped[str]
    encrypted_value: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    item_id: Mapped[int]
    status: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class VoiceRunRow(Base):
    """One source video on its way to a fine-tuned voice model.

    The `phase` column is the state machine itself, not a status label:
    VoiceRunReconciler reads it to decide what to do next, so a run survives a
    restart mid-pipeline. Audio, clips, and review decisions all stay on the
    voice factory host - only the run state lives here.

    Every column below is something that would be lost if the factory's work/
    directory were deleted. Anything the factory can recompute from work/ -
    the video's title, its clip counts, its speaker map - is read from the
    factory instead, so no fact here has two writers.
    """

    __tablename__ = "voice_runs"
    __table_args__ = (
        Index("idx_voice_runs_phase", "phase"),
        # the factory webhook arrives with a job id and nothing else
        Index("idx_voice_runs_voyicer_job_id", "voyicer_job_id"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    primary_character: Mapped[str]
    source_url: Mapped[str]
    # the only join to the voice factory, which owns the video: its title, its
    # clips, its counts, and the speaker map that names their characters. None
    # until the run resolves it.
    video_id: Mapped[str | None]
    phase: Mapped[str]
    diarize: Mapped[bool] = mapped_column(default=True)
    num_speakers: Mapped[int | None]
    # speaker label -> Voice id (Story 3.2), or None to discard that speaker.
    # DB-only, and the factory has no Voice concept, so nothing there mirrors
    # it - see VoiceRun.voice_assignments.
    voice_assignments: Mapped[dict] = mapped_column(JSONB, default=dict)
    # the control API job backing the current phase, if one is running
    voyicer_job_id: Mapped[str | None]
    # DOWNLOADING runs the ingest steps in order (download, transcribe, chunk,
    # diarize, review). This is which one is in flight, and it is what makes a
    # retry resume on the failed step instead of on the download.
    ingest_stage_index: Mapped[int] = mapped_column(default=0)
    # COMMITTING runs three stages in order (commit, resample, preprocess).
    # This is which one is in flight.
    commit_stage_index: Mapped[int] = mapped_column(default=0)
    # last training progress the factory reported over its webhook
    current_epoch: Mapped[int | None]
    current_loss: Mapped[float | None]
    error: Mapped[str | None]
    # consecutive transient factory errors; a successful call resets it
    error_count: Mapped[int] = mapped_column(default=0)
    # the phase a failed run was in, so a retry can put it back there
    failed_from_phase: Mapped[str | None]
    # the job that failed. Kept after voyicer_job_id is cleared, because its log
    # is the only place that says why the run stopped.
    failed_job_id: Mapped[str | None]
    # Mutual exclusion for multiple API instances. An instance claims a run by
    # setting these in one atomic UPDATE, and the lease expires on its own, so
    # an instance that dies never strands a run. No separate lock service.
    leased_until: Mapped[datetime | None]
    lease_owner: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class VoiceRow(Base):
    """One durable voice: the trained-model identity a run's clips
    contribute to (Story 3.1 introduces the entity only - no contribution
    link exists until Story 3.2).

    `phase` tracks training progress and is independent of any one
    VoiceRunRow's `phase`, which tracks a single video's ingest.
    """

    __tablename__ = "voices"
    __table_args__ = (
        Index("idx_voices_phase", "phase"),
        Index("idx_voices_name", "name", unique=True),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    phase: Mapped[str]
    checkpoint_path: Mapped[str | None]
    # the control API job backing the current phase, if one is running
    voyicer_job_id: Mapped[str | None]
    # Mutual exclusion for multiple API instances, same pattern as
    # VoiceRunRow above: an instance claims a voice by setting these in one
    # atomic UPDATE, and the lease expires on its own.
    leased_until: Mapped[datetime | None]
    lease_owner: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class VoiceContributionRow(Base):
    """One video's clips committed into one voice, under one speaker label.

    Append-only (Story 3.2): no update method exists on the repository, only
    create_contribution and read queries. This is the audit trail FR19 asks
    for - which video contributed which speaker to which voice, and when.
    """

    __tablename__ = "voice_contributions"
    __table_args__ = (
        Index("idx_voice_contributions_voice_id", "voice_id"),
        Index("idx_voice_contributions_run_id", "run_id"),
        UniqueConstraint(
            "voice_id", "run_id", "speaker_label", name="uq_voice_contributions_key"
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    voice_id: Mapped[str] = mapped_column(ForeignKey("voices.id", ondelete="CASCADE"))
    run_id: Mapped[str] = mapped_column(ForeignKey("voice_runs.id", ondelete="CASCADE"))
    speaker_label: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
