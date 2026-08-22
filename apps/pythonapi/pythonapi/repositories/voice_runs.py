"""Persistence for voice runs.

Same shape as orders.py: a Protocol contract, an in-memory double for tests, and
a Postgres implementation that opens its own session per method.

The lease methods are the one place this file does more than store and fetch.
Several API instances can run at once, and two of them reconciling the same run
would start the same factory job twice. claim_runs takes ownership in a single
atomic UPDATE, so the database decides who wins. The lease carries an expiry
rather than a lock, so an instance that dies never strands a run - no separate
lock service, and nothing to clean up by hand.
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from pythonapi.models.orm import VoiceRunRow
from pythonapi.models.voice import RESTING_PHASES, VoiceRun, VoiceRunPhase


def _utc_now() -> datetime:
    """Now, as naive UTC.

    Every datetime in this file comes from here. The Postgres columns are
    TIMESTAMP WITHOUT TIME ZONE, so a stored value is always naive, and Python
    refuses to compare a naive datetime against an aware one. One helper is
    what keeps a single aware value from reaching a comparison.
    """
    return datetime.now(UTC).replace(tzinfo=None)


class VoiceRunRepository(Protocol):
    """Storage contract for voice runs."""

    async def create_run(self, run: VoiceRun) -> None: ...

    async def get_run(self, run_id: str) -> VoiceRun | None: ...

    async def list_runs(self, limit: int = 50, offset: int = 0) -> list[VoiceRun]: ...

    async def list_active_runs(self) -> list[VoiceRun]:
        """Runs the reconciler still has work to do on."""
        ...

    async def find_run_by_job_id(self, job_id: str) -> VoiceRun | None:
        """The run backing one factory job. The webhook has only the job id."""
        ...

    async def claim_runs(
        self, owner: str, lease_seconds: float, limit: int = 50
    ) -> list[VoiceRun]:
        """Take ownership of every active run no one else holds."""
        ...

    async def claim_run(
        self, run_id: str, owner: str, lease_seconds: float
    ) -> VoiceRun | None:
        """Take ownership of one run. None when it rests or someone holds it."""
        ...

    async def release_run(self, run_id: str) -> None:
        """Give up ownership so the next pass, here or elsewhere, can take it."""
        ...

    async def record_progress(
        self, run_id: str, epoch: int | None, loss: float | None
    ) -> VoiceRun | None:
        """Store training progress. None when nothing changed or no such run.

        Progress is the one field the webhook writes directly. It is a reading,
        not a transition, so it does not belong to the reconciler.
        """
        ...

    async def update_run(self, run: VoiceRun) -> bool:
        """Persist a run. False when it no longer exists."""
        ...

    async def delete_run(self, run_id: str) -> bool: ...


class InMemoryVoiceRunRepository:
    """Dict-backed VoiceRunRepository. Test double and local dev without
    Postgres."""

    def __init__(self) -> None:
        self._runs: dict[str, VoiceRun] = {}
        # run id -> (expiry, owner). Kept beside the runs rather than on them,
        # for the same reason the Postgres lease columns stay off VoiceRun: a
        # lease is the reconciler's, not the run's.
        self._leases: dict[str, tuple[datetime, str]] = {}

    async def create_run(self, run: VoiceRun) -> None:
        self._runs[run.id] = run.model_copy(deep=True)

    async def get_run(self, run_id: str) -> VoiceRun | None:
        run = self._runs.get(run_id)
        return run.model_copy(deep=True) if run else None

    async def list_runs(self, limit: int = 50, offset: int = 0) -> list[VoiceRun]:
        ordered = sorted(
            self._runs.values(), key=lambda run: run.created_at, reverse=True
        )
        return [run.model_copy(deep=True) for run in ordered[offset : offset + limit]]

    async def list_active_runs(self) -> list[VoiceRun]:
        return [
            run.model_copy(deep=True)
            for run in self._runs.values()
            if run.phase not in RESTING_PHASES
        ]

    async def find_run_by_job_id(self, job_id: str) -> VoiceRun | None:
        for run in self._runs.values():
            if run.voyicer_job_id == job_id:
                return run.model_copy(deep=True)
        return None

    async def claim_runs(
        self, owner: str, lease_seconds: float, limit: int = 50
    ) -> list[VoiceRun]:
        claimed = []
        for run in sorted(self._runs.values(), key=lambda run: run.created_at):
            if len(claimed) >= limit:
                break
            if run.phase in RESTING_PHASES or self._is_held(run.id):
                continue
            self._take_lease(run.id, owner, lease_seconds)
            claimed.append(run.model_copy(deep=True))
        return claimed

    async def claim_run(
        self, run_id: str, owner: str, lease_seconds: float
    ) -> VoiceRun | None:
        run = self._runs.get(run_id)
        if run is None or run.phase in RESTING_PHASES or self._is_held(run_id):
            return None
        self._take_lease(run_id, owner, lease_seconds)
        return run.model_copy(deep=True)

    async def release_run(self, run_id: str) -> None:
        self._leases.pop(run_id, None)

    async def record_progress(
        self, run_id: str, epoch: int | None, loss: float | None
    ) -> VoiceRun | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        if (epoch is None or run.current_epoch == epoch) and (
            loss is None or run.current_loss == loss
        ):
            return None
        if epoch is not None:
            run.current_epoch = epoch
        if loss is not None:
            run.current_loss = loss
        run.updated_at = _utc_now()
        return run.model_copy(deep=True)

    async def update_run(self, run: VoiceRun) -> bool:
        if run.id not in self._runs:
            return False
        run.updated_at = _utc_now()
        self._runs[run.id] = run.model_copy(deep=True)
        return True

    async def delete_run(self, run_id: str) -> bool:
        self._leases.pop(run_id, None)
        return self._runs.pop(run_id, None) is not None

    def _is_held(self, run_id: str) -> bool:
        lease = self._leases.get(run_id)
        return lease is not None and lease[0] > _utc_now()

    def _take_lease(self, run_id: str, owner: str, lease_seconds: float) -> None:
        self._leases[run_id] = (_utc_now() + timedelta(seconds=lease_seconds), owner)


class PostgresVoiceRunRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_run(self, run: VoiceRun) -> None:
        async with AsyncSession(self._engine) as session:
            session.add(_row_from_run(run))
            await session.commit()

    async def get_run(self, run_id: str) -> VoiceRun | None:
        async with AsyncSession(self._engine) as session:
            row = await session.get(VoiceRunRow, run_id)
            return _run_from_row(row) if row else None

    async def list_runs(self, limit: int = 50, offset: int = 0) -> list[VoiceRun]:
        async with AsyncSession(self._engine) as session:
            result = await session.execute(
                select(VoiceRunRow)
                .order_by(VoiceRunRow.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            return [_run_from_row(row) for row in result.scalars()]

    async def list_active_runs(self) -> list[VoiceRun]:
        resting = [phase.value for phase in RESTING_PHASES]
        async with AsyncSession(self._engine) as session:
            result = await session.execute(
                select(VoiceRunRow)
                .where(VoiceRunRow.phase.not_in(resting))
                .order_by(VoiceRunRow.created_at)
            )
            return [_run_from_row(row) for row in result.scalars()]

    async def find_run_by_job_id(self, job_id: str) -> VoiceRun | None:
        async with AsyncSession(self._engine) as session:
            result = await session.execute(
                select(VoiceRunRow).where(VoiceRunRow.voyicer_job_id == job_id).limit(1)
            )
            row = result.scalar_one_or_none()
            return _run_from_row(row) if row else None

    async def claim_runs(
        self, owner: str, lease_seconds: float, limit: int = 50
    ) -> list[VoiceRun]:
        """Claim every free active run in one atomic UPDATE.

        The WHERE clause is the mutual exclusion: two instances running this at
        the same moment cannot both match the same row, because the first one to
        commit moves leased_until into the future. RETURNING then tells us which
        rows we actually won, so nothing has to be read back and re-checked.
        """
        claimable = (
            select(VoiceRunRow.id)
            .where(VoiceRunRow.phase.not_in(_resting_phase_values()), _lease_is_free())
            .order_by(VoiceRunRow.created_at)
            .limit(limit)
            .scalar_subquery()
        )
        return await self._claim_where(
            VoiceRunRow.id.in_(claimable), owner, lease_seconds
        )

    async def claim_run(
        self, run_id: str, owner: str, lease_seconds: float
    ) -> VoiceRun | None:
        claimed = await self._claim_where(
            VoiceRunRow.id == run_id, owner, lease_seconds
        )
        return claimed[0] if claimed else None

    async def release_run(self, run_id: str) -> None:
        async with AsyncSession(self._engine) as session:
            await session.execute(
                update(VoiceRunRow)
                .where(VoiceRunRow.id == run_id)
                .values(leased_until=None, lease_owner=None)
            )
            await session.commit()

    async def record_progress(
        self, run_id: str, epoch: int | None, loss: float | None
    ) -> VoiceRun | None:
        async with AsyncSession(self._engine) as session:
            row = await session.get(VoiceRunRow, run_id)
            if row is None:
                return None
            unchanged = (epoch is None or row.current_epoch == epoch) and (
                loss is None or row.current_loss == loss
            )
            if unchanged:
                return None
            if epoch is not None:
                row.current_epoch = epoch
            if loss is not None:
                row.current_loss = loss
            row.updated_at = _utc_now()
            await session.commit()
            await session.refresh(row)
            return _run_from_row(row)

    async def _claim_where(
        self, run_filter, owner: str, lease_seconds: float
    ) -> list[VoiceRun]:
        """The one UPDATE behind both claim methods.

        Everything stays inside the session: RETURNING rows are read, turned
        into plain VoiceRun copies, and only then committed. Reading them after
        the session closed would hand back detached rows.
        """
        expiry = _utc_now() + timedelta(seconds=lease_seconds)
        async with AsyncSession(self._engine) as session:
            result = await session.execute(
                update(VoiceRunRow)
                .where(
                    run_filter,
                    VoiceRunRow.phase.not_in(_resting_phase_values()),
                    _lease_is_free(),
                )
                .values(leased_until=expiry, lease_owner=owner)
                .returning(VoiceRunRow)
            )
            runs = [_run_from_row(row) for row in result.scalars()]
            await session.commit()
            return runs

    async def update_run(self, run: VoiceRun) -> bool:
        async with AsyncSession(self._engine) as session:
            row = await session.get(VoiceRunRow, run.id)
            if row is None:
                return False
            row.primary_character = run.primary_character
            row.video_id = run.video_id
            row.phase = run.phase.value
            row.diarize = run.diarize
            row.num_speakers = run.num_speakers
            row.voice_assignments = dict(run.voice_assignments)
            row.voyicer_job_id = run.voyicer_job_id
            row.ingest_stage_index = run.ingest_stage_index
            row.commit_stage_index = run.commit_stage_index
            row.current_epoch = run.current_epoch
            row.current_loss = run.current_loss
            row.error = run.error
            row.error_count = run.error_count
            row.failed_from_phase = (
                run.failed_from_phase.value if run.failed_from_phase else None
            )
            row.failed_job_id = run.failed_job_id
            row.updated_at = _utc_now()
            await session.commit()
            return True

    async def delete_run(self, run_id: str) -> bool:
        async with AsyncSession(self._engine) as session:
            row = await session.get(VoiceRunRow, run_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True


def _resting_phase_values() -> list[str]:
    return [phase.value for phase in RESTING_PHASES]


def _lease_is_free():
    """No one holds this run, or whoever did has gone away."""
    now = _utc_now()
    return (VoiceRunRow.leased_until.is_(None)) | (VoiceRunRow.leased_until < now)


def _row_from_run(run: VoiceRun) -> VoiceRunRow:
    return VoiceRunRow(
        id=run.id,
        primary_character=run.primary_character,
        source_url=run.source_url,
        video_id=run.video_id,
        phase=run.phase.value,
        diarize=run.diarize,
        num_speakers=run.num_speakers,
        voice_assignments=dict(run.voice_assignments),
        voyicer_job_id=run.voyicer_job_id,
        ingest_stage_index=run.ingest_stage_index,
        commit_stage_index=run.commit_stage_index,
        current_epoch=run.current_epoch,
        current_loss=run.current_loss,
        error=run.error,
        error_count=run.error_count,
        failed_from_phase=(
            run.failed_from_phase.value if run.failed_from_phase else None
        ),
        failed_job_id=run.failed_job_id,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _run_from_row(row: VoiceRunRow) -> VoiceRun:
    return VoiceRun(
        id=row.id,
        primary_character=row.primary_character,
        source_url=row.source_url,
        video_id=row.video_id,
        phase=VoiceRunPhase(row.phase),
        diarize=row.diarize,
        num_speakers=row.num_speakers,
        voice_assignments=row.voice_assignments or {},
        voyicer_job_id=row.voyicer_job_id,
        ingest_stage_index=row.ingest_stage_index,
        commit_stage_index=row.commit_stage_index,
        current_epoch=row.current_epoch,
        current_loss=row.current_loss,
        error=row.error,
        error_count=row.error_count,
        failed_from_phase=(
            VoiceRunPhase(row.failed_from_phase) if row.failed_from_phase else None
        ),
        failed_job_id=row.failed_job_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
