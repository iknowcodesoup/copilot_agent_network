"""Persistence for voice contributions (Story 3.2).

Same shape as repositories/voices.py: a Protocol contract, an in-memory
double for tests, and a Postgres implementation that opens its own session
per method. voice_contributions is append-only, so this repository offers no
update method - only create_contribution and read queries.
"""

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from pythonapi.models.orm import VoiceContributionRow, VoiceRunRow
from pythonapi.models.voices import VoiceContribution


class VoiceContributionRepository(Protocol):
    """Storage contract for voice contributions - the audit trail (FR19)."""

    async def create_contribution(self, contribution: VoiceContribution) -> None: ...

    async def list_contributions_for_voice(
        self, voice_id: str
    ) -> list[VoiceContribution]:
        """Every contribution committed into this voice, each traceable to
        its run and video (FR22)."""
        ...


class InMemoryVoiceContributionRepository:
    """Dict-backed VoiceContributionRepository. Test double and local dev
    without Postgres."""

    def __init__(self) -> None:
        self._contributions: dict[str, VoiceContribution] = {}

    async def create_contribution(self, contribution: VoiceContribution) -> None:
        self._contributions[contribution.id] = contribution.model_copy(deep=True)

    async def list_contributions_for_voice(
        self, voice_id: str
    ) -> list[VoiceContribution]:
        return [
            contribution.model_copy(deep=True)
            for contribution in self._contributions.values()
            if contribution.voice_id == voice_id
        ]


class PostgresVoiceContributionRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_contribution(self, contribution: VoiceContribution) -> None:
        async with AsyncSession(self._engine) as session:
            session.add(_row_from_contribution(contribution))
            await session.commit()

    async def list_contributions_for_voice(
        self, voice_id: str
    ) -> list[VoiceContribution]:
        async with AsyncSession(self._engine) as session:
            result = await session.execute(
                select(VoiceContributionRow, VoiceRunRow)
                .join(VoiceRunRow, VoiceContributionRow.run_id == VoiceRunRow.id)
                .where(VoiceContributionRow.voice_id == voice_id)
                .order_by(VoiceContributionRow.created_at)
            )
            return [
                _contribution_from_row(contribution_row, run_row)
                for contribution_row, run_row in result.all()
            ]


def _row_from_contribution(contribution: VoiceContribution) -> VoiceContributionRow:
    return VoiceContributionRow(
        id=contribution.id,
        voice_id=contribution.voice_id,
        run_id=contribution.run_id,
        speaker_label=contribution.speaker_label,
        created_at=contribution.created_at,
    )


def _contribution_from_row(
    contribution_row: VoiceContributionRow, run_row: VoiceRunRow
) -> VoiceContribution:
    return VoiceContribution(
        id=contribution_row.id,
        voice_id=contribution_row.voice_id,
        run_id=contribution_row.run_id,
        # video_id only: the factory owns the title, and a caller that wants
        # one resolves it at read time -- see core/video_titles.py
        video_id=run_row.video_id,
        speaker_label=contribution_row.speaker_label,
        created_at=contribution_row.created_at,
    )
