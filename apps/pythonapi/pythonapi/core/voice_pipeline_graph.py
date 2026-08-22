"""LangGraph that advances one voice run by one phase.

The graph is deliberately stateless. Every tick loads a run from Postgres, runs
exactly one node, and writes the resulting phase back. The `voice_runs.phase`
column is the durable state, not a LangGraph checkpointer, which is what lets a
run sit in AWAITING_REVIEW for days and survive a restart or redeploy.

Each node answers one question: has the control API job for this phase finished,
and if so what comes next? Nodes never block on a job. They start it, record the
job id, and return.

A node never fails a run for an unreachable factory. Training runs for days and
the GPU host can reboot inside that, so a transient error comes back as
`transient_error` and the phase stays put. The reconciler counts those and only
gives up after VOICE_MAX_CONSECUTIVE_ERRORS in a row. A permanent error - a job
that really failed, a contract the factory rejected - still fails the run here.
"""

import logging
from typing import TypedDict

from langgraph.graph import END, StateGraph

from pythonapi.core.voice_factory_gateway import (
    JOB_STATE_CANCELLED,
    JOB_STATE_FAILED,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    STAGE_EXPORT,
    STAGE_PREPROCESS,
    STAGE_RESAMPLE,
    STAGE_TRAIN,
    STAGE_YOUTUBE_CHUNK,
    STAGE_YOUTUBE_COMMIT,
    STAGE_YOUTUBE_DIARIZE,
    STAGE_YOUTUBE_DOWNLOAD,
    STAGE_YOUTUBE_REVIEW,
    STAGE_YOUTUBE_TRANSCRIBE,
    VoiceFactoryError,
    VoiceFactoryGateway,
    VoiceFactoryTransientError,
)
from pythonapi.models.voice import VoiceRun, VoiceRunPhase

logger = logging.getLogger(__name__)


class VoiceRunState(TypedDict, total=False):
    """What one tick reads and writes. Mirrors the persisted VoiceRun."""

    run: VoiceRun
    # set when the tick changed something worth writing back
    changed: bool
    # set when the factory could not answer. The phase is untouched and the
    # reconciler decides whether this run has had too many of these.
    transient_error: str | None


def build_voice_pipeline_graph(gateway: VoiceFactoryGateway):
    """Compile the graph. Called once, in main.py's lifespan."""
    builder = StateGraph(VoiceRunState)

    builder.add_node(VoiceRunPhase.DOWNLOADING.value, _ingest_node_factory(gateway))
    builder.add_node(VoiceRunPhase.DIARIZING.value, _diarizing_node_factory(gateway))
    builder.add_node(VoiceRunPhase.COMMITTING.value, _committing_node_factory(gateway))
    builder.add_node(VoiceRunPhase.TRAINING.value, _training_node_factory(gateway))
    builder.add_node(VoiceRunPhase.EXPORTING.value, _exporting_node_factory(gateway))

    builder.set_conditional_entry_point(
        _route_by_phase,
        {
            VoiceRunPhase.DOWNLOADING.value: VoiceRunPhase.DOWNLOADING.value,
            VoiceRunPhase.DIARIZING.value: VoiceRunPhase.DIARIZING.value,
            VoiceRunPhase.COMMITTING.value: VoiceRunPhase.COMMITTING.value,
            VoiceRunPhase.TRAINING.value: VoiceRunPhase.TRAINING.value,
            VoiceRunPhase.EXPORTING.value: VoiceRunPhase.EXPORTING.value,
            END: END,
        },
    )
    # one node per tick: the reconciler calls the graph again on its next pass
    for phase in (
        VoiceRunPhase.DOWNLOADING,
        VoiceRunPhase.DIARIZING,
        VoiceRunPhase.COMMITTING,
        VoiceRunPhase.TRAINING,
        VoiceRunPhase.EXPORTING,
    ):
        builder.add_edge(phase.value, END)

    return builder.compile()


def _route_by_phase(state: VoiceRunState) -> str:
    phase = state["run"].phase
    if phase in _NODE_PHASES:
        return phase.value
    # AWAITING_REVIEW waits on a person; READY and FAILED are terminal
    return END


_NODE_PHASES = frozenset(
    {
        VoiceRunPhase.DOWNLOADING,
        VoiceRunPhase.DIARIZING,
        VoiceRunPhase.COMMITTING,
        VoiceRunPhase.TRAINING,
        VoiceRunPhase.EXPORTING,
    }
)


def _advance(run: VoiceRun, phase: VoiceRunPhase) -> VoiceRunState:
    run.phase = phase
    run.voyicer_job_id = None
    return {"run": run, "changed": True}


def _hold(run: VoiceRun) -> VoiceRunState:
    """Nothing to do this tick. The phase and the job both stand."""
    return {"run": run, "changed": False}


def _fail(run: VoiceRun, message: str) -> VoiceRunState:
    logger.warning("voice run %s failed: %s", run.id, message)
    # remember where it got to, so a manual retry can put it back. The stage
    # indexes are deliberately untouched: they are the finer half of that same
    # answer, and a retry has to resume on the step that failed.
    run.failed_from_phase = run.phase
    run.phase = VoiceRunPhase.FAILED
    run.error = message
    # The job's log is the only record of why this failed, so keep the id.
    # Clearing voyicer_job_id is what stops the next tick polling a dead job.
    run.failed_job_id = run.voyicer_job_id
    run.voyicer_job_id = None
    return {"run": run, "changed": True}


def _defer(run: VoiceRun, message: str) -> VoiceRunState:
    """The factory could not answer. Try the same thing again next tick."""
    return {"run": run, "changed": False, "transient_error": message}


async def _poll_job(
    gateway: VoiceFactoryGateway, run: VoiceRun, next_phase: VoiceRunPhase
) -> VoiceRunState | None:
    """Check the running job. None means it is still going, so leave it alone."""
    try:
        state = await gateway.get_job_state(run.voyicer_job_id)
    except VoiceFactoryTransientError as error:
        return _defer(run, f"Could not read job {run.voyicer_job_id}: {error}")
    except VoiceFactoryError as error:
        return _fail(run, f"Could not read job {run.voyicer_job_id}: {error}")

    if state == JOB_STATE_RUNNING:
        return None
    if state == JOB_STATE_SUCCEEDED:
        return _advance(run, next_phase)
    if state in (JOB_STATE_FAILED, JOB_STATE_CANCELLED):
        return _fail(run, f"Job {run.voyicer_job_id} {state}. See its log for detail.")
    return _fail(run, f"Job {run.voyicer_job_id} reported unknown state {state!r}")


# The ingest steps, in the order the factory runs them. Each is its own control
# API job. Diarization drops out for a run that did not ask for it, so
# ingest_stage_index always points at a step that will really run.
INGEST_STAGES = (
    STAGE_YOUTUBE_DOWNLOAD,
    STAGE_YOUTUBE_TRANSCRIBE,
    STAGE_YOUTUBE_CHUNK,
    STAGE_YOUTUBE_DIARIZE,
    STAGE_YOUTUBE_REVIEW,
)


def ingest_stages_for(run: VoiceRun) -> tuple[str, ...]:
    """The ingest steps this run has to walk, in order."""
    if run.diarize:
        return INGEST_STAGES
    return tuple(stage for stage in INGEST_STAGES if stage != STAGE_YOUTUBE_DIARIZE)


def _ingest_start_fields(run: VoiceRun, stage: str) -> dict:
    """What one ingest step needs beyond its name.

    Every step resolves the video directory from the URL, so all of them get
    it. Only diarization can use a speaker count.
    """
    fields = {
        "character": run.primary_character,
        "stage": stage,
        "youtube_url": run.source_url,
    }
    if stage == STAGE_YOUTUBE_DIARIZE:
        fields["num_speakers"] = run.num_speakers
    return fields


def _ingest_node_factory(gateway: VoiceFactoryGateway):
    """DOWNLOADING: walk the ingest steps, one factory job per tick.

    Download, transcribe, chunk, diarize, and review each run as their own job.
    `ingest_stage_index` survives a failure, so a retry resumes on the step that
    fell over rather than downloading the video again. Same shape and the same
    reason as _committing_node_factory below.
    """

    async def node(state: VoiceRunState) -> VoiceRunState:
        run = state["run"]
        stages = ingest_stages_for(run)
        stage_index = min(run.ingest_stage_index, len(stages) - 1)
        stage = stages[stage_index]

        if run.voyicer_job_id is None:
            try:
                job_id = await gateway.start_job(**_ingest_start_fields(run, stage))
            except VoiceFactoryTransientError as error:
                return _defer(run, f"Could not start {stage}: {error}")
            except VoiceFactoryError as error:
                return _fail(run, f"Could not start {stage}: {error}")
            run.voyicer_job_id = job_id
            run.ingest_stage_index = stage_index
            return {"run": run, "changed": True}

        try:
            job_state = await gateway.get_job_state(run.voyicer_job_id)
        except VoiceFactoryTransientError as error:
            return _defer(run, f"Could not read job {run.voyicer_job_id}: {error}")
        except VoiceFactoryError as error:
            return _fail(run, f"Could not read job {run.voyicer_job_id}: {error}")

        if job_state == JOB_STATE_RUNNING:
            return _hold(run)
        if job_state != JOB_STATE_SUCCEEDED:
            return _fail(run, f"Step {stage} {job_state}. See its log for detail.")

        if stage_index + 1 < len(stages):
            run.ingest_stage_index = stage_index + 1
            run.voyicer_job_id = None
            return {"run": run, "changed": True}
        run.ingest_stage_index = 0
        return _advance(run, VoiceRunPhase.DIARIZING)

    return node


def _diarizing_node_factory(gateway: VoiceFactoryGateway):
    """DIARIZING: the ingest job already finished, so collect the clips.

    Reads back what ingest produced and parks the run for human review.
    """

    async def node(state: VoiceRunState) -> VoiceRunState:
        run = state["run"]
        if not run.video_id:
            return _fail(run, "No video id recorded for this run")
        try:
            video_clips = await gateway.get_clips(run.video_id)
        except VoiceFactoryTransientError as error:
            return _defer(run, f"Could not read clips: {error}")
        except VoiceFactoryError as error:
            return _fail(run, f"Could not read clips: {error}")

        # the clips are read to check that ingest produced any, not to count
        # them into the run: the count is the factory's, recomputed from
        # review.csv whenever anyone asks
        if not video_clips.clips:
            return _fail(run, "Ingest produced no clips. Try a different video.")
        return _advance(run, VoiceRunPhase.AWAITING_REVIEW)

    return node


def _committing_node_factory(gateway: VoiceFactoryGateway):
    """COMMITTING: merge approved clips, then resample and preprocess.

    Three stages run back to back here. Each tick starts the next one, so the
    run walks through them one reconciler pass at a time.
    """
    ordered_stages = (STAGE_YOUTUBE_COMMIT, STAGE_RESAMPLE, STAGE_PREPROCESS)

    async def node(state: VoiceRunState) -> VoiceRunState:
        run = state["run"]
        stage_index = min(run.commit_stage_index, len(ordered_stages) - 1)

        if run.voyicer_job_id is None:
            stage = ordered_stages[stage_index]
            try:
                job_id = await gateway.start_job(
                    character=run.primary_character, stage=stage
                )
            except VoiceFactoryTransientError as error:
                return _defer(run, f"Could not start {stage}: {error}")
            except VoiceFactoryError as error:
                return _fail(run, f"Could not start {stage}: {error}")
            run.voyicer_job_id = job_id
            return {"run": run, "changed": True}

        try:
            job_state = await gateway.get_job_state(run.voyicer_job_id)
        except VoiceFactoryTransientError as error:
            return _defer(run, f"Could not read job {run.voyicer_job_id}: {error}")
        except VoiceFactoryError as error:
            return _fail(run, f"Could not read job {run.voyicer_job_id}: {error}")

        if job_state == JOB_STATE_RUNNING:
            return _hold(run)
        if job_state != JOB_STATE_SUCCEEDED:
            stage = ordered_stages[stage_index]
            return _fail(run, f"Stage {stage} {job_state}. See its log for detail.")

        if stage_index + 1 < len(ordered_stages):
            run.commit_stage_index = stage_index + 1
            run.voyicer_job_id = None
            return {"run": run, "changed": True}
        run.commit_stage_index = 0
        return _advance(run, VoiceRunPhase.TRAINING)

    return node


def _training_node_factory(gateway: VoiceFactoryGateway):
    """TRAINING: fine-tune the model. Takes hours to days."""

    async def node(state: VoiceRunState) -> VoiceRunState:
        run = state["run"]
        if run.voyicer_job_id is None:
            try:
                job_id = await gateway.start_job(
                    character=run.primary_character, stage=STAGE_TRAIN
                )
            except VoiceFactoryTransientError as error:
                return _defer(run, f"Could not start training: {error}")
            except VoiceFactoryError as error:
                return _fail(run, f"Could not start training: {error}")
            run.voyicer_job_id = job_id
            return {"run": run, "changed": True}

        result = await _poll_job(gateway, run, VoiceRunPhase.EXPORTING)
        return result if result is not None else _hold(run)

    return node


def _exporting_node_factory(gateway: VoiceFactoryGateway):
    """EXPORTING: write the ONNX model, then the run is done."""

    async def node(state: VoiceRunState) -> VoiceRunState:
        run = state["run"]
        if run.voyicer_job_id is None:
            try:
                job_id = await gateway.start_job(
                    character=run.primary_character,
                    stage=STAGE_EXPORT,
                    # No run ever wrote this column, so the value was already
                    # always None - the column removal (Story 3.1) just makes
                    # that explicit.
                    checkpoint=None,
                )
            except VoiceFactoryTransientError as error:
                return _defer(run, f"Could not start export: {error}")
            except VoiceFactoryError as error:
                return _fail(run, f"Could not start export: {error}")
            run.voyicer_job_id = job_id
            return {"run": run, "changed": True}

        result = await _poll_job(gateway, run, VoiceRunPhase.READY)
        return result if result is not None else _hold(run)

    return node
