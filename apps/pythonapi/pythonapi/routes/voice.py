"""HTTP layer for the voice-model pipeline.

Thin, like every other router here: it validates input, delegates to the gateway
or the repository, and shapes the response. The pipeline runs on the voice
factory host and the phase transitions belong to VoiceRunReconciler.
"""

import uuid
from contextlib import suppress
from datetime import UTC, datetime

from ag_ui.core import CustomEvent, EventType, StateSnapshotEvent
from ag_ui.encoder import EventEncoder
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from pythonapi.config import settings
from pythonapi.core.speaker_board import build_speaker_board
from pythonapi.core.video_titles import resolve_video_titles
from pythonapi.core.voice_events import (
    EVENT_RUN_LOG,
    EVENT_RUN_UPDATED,
    VoiceEvent,
    VoiceEventStream,
)
from pythonapi.core.voice_factory_gateway import (
    VoiceFactoryError,
    VoiceFactoryGateway,
)
from pythonapi.dependencies import (
    get_required_voice_contribution_repository,
    get_required_voice_event_stream,
    get_required_voice_factory_gateway,
    get_required_voice_repository,
    get_required_voice_run_reconciler,
    get_required_voice_run_repository,
    get_voice_factory_gateway,
)
from pythonapi.models.voice import (
    JobLog,
    SpeakerAssignmentRequest,
    SpeakerBoard,
    TrainingProgress,
    VoiceLogChunk,
    VoiceRun,
    VoiceRunPhase,
    VoiceRunRequest,
    VoiceRunResponse,
    VoiceWebhookEvent,
)
from pythonapi.models.voices import (
    RunAssignRequest,
    RunAssignResponse,
    VoiceContribution,
)
from pythonapi.repositories.voice_contributions import VoiceContributionRepository
from pythonapi.repositories.voice_runs import VoiceRunRepository
from pythonapi.repositories.voices import VoiceRepository
from pythonapi.workers.voice_run_reconciler import VoiceRunReconciler

router = APIRouter(prefix="/voice", tags=["Voice"])

# Header the voice factory signs its webhooks with.
WEBHOOK_TOKEN_HEADER = "X-Voice-Factory-Token"

# How many replayed events one reconnect may catch up on. The stream is bounded
# well below this, so the cap only guards against a pathological Last-Event-ID.
REPLAY_LIMIT = 500


def _unavailable(error: VoiceFactoryError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"The voice factory did not answer: {error}",
    )


async def _load_run(repository: VoiceRunRepository, run_id: str) -> VoiceRun:
    run = await repository.get_run(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Voice run not found")
    return run


# Video search, characters, the video list, per-video speakers, clip decisions,
# clip audio, and commit all moved to routes/voice_factory_proxy.py. The factory
# owns every one of them and nothing here read their fields, so a typed route
# was a second definition of a shape this service does not own.


@router.get("/videos/{video_id}/clips", response_model=SpeakerBoard)
async def get_video_speaker_board(
    video_id: str,
    gateway: VoiceFactoryGateway = Depends(get_required_voice_factory_gateway),
):
    """Clips grouped by speaker, for the review screen.

    Keyed on the video, because the clips are: a video ingested for one
    character and claimed by another has one set of clips and one review, and
    no run has to exist for a person to read them.
    """
    try:
        video_clips = await gateway.get_clips(video_id)
    except VoiceFactoryError as error:
        raise _unavailable(error) from error
    return build_speaker_board(video_clips)


@router.post(
    "/runs",
    response_model=VoiceRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_run(
    run_request: VoiceRunRequest,
    gateway: VoiceFactoryGateway = Depends(get_required_voice_factory_gateway),
    repository: VoiceRunRepository = Depends(get_required_voice_run_repository),
):
    """Start a run and return at once.

    Resolving the video id is the only thing done inline: it costs one request,
    downloads nothing, and every later call needs the id to find the run's
    directory. Everything after this is the reconciler's job.
    """
    try:
        video_id = await gateway.resolve_video_id(run_request.source_url)
    except VoiceFactoryError as error:
        raise _unavailable(error) from error

    now = datetime.now(UTC)
    run = VoiceRun(
        id=uuid.uuid4().hex,
        primary_character=run_request.primary_character,
        source_url=run_request.source_url,
        video_id=video_id,
        phase=VoiceRunPhase.DOWNLOADING,
        diarize=run_request.diarize,
        num_speakers=run_request.num_speakers,
        created_at=now,
        updated_at=now,
    )
    await repository.create_run(run)
    return VoiceRunResponse(id=run.id, phase=run.phase)


@router.get("/runs", response_model=list[VoiceRun])
async def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    repository: VoiceRunRepository = Depends(get_required_voice_run_repository),
):
    return await repository.list_runs(limit=limit, offset=offset)


@router.get("/runs/{run_id}", response_model=VoiceRun)
async def get_run(
    run_id: str,
    repository: VoiceRunRepository = Depends(get_required_voice_run_repository),
):
    return await _load_run(repository, run_id)


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(
    run_id: str,
    gateway: VoiceFactoryGateway = Depends(get_required_voice_factory_gateway),
    repository: VoiceRunRepository = Depends(get_required_voice_run_repository),
):
    """Delete a run. Cancels its job first so nothing keeps running headless."""
    run = await _load_run(repository, run_id)
    if run.voyicer_job_id:
        # the run goes away either way; a stale job is the lesser problem
        with suppress(VoiceFactoryError):
            await gateway.cancel_job(run.voyicer_job_id)
    await repository.delete_run(run_id)


@router.post("/runs/{run_id}/approve", response_model=VoiceRun)
async def approve_run(
    run_id: str,
    assignment: SpeakerAssignmentRequest,
    gateway: VoiceFactoryGateway = Depends(get_required_voice_factory_gateway),
    repository: VoiceRunRepository = Depends(get_required_voice_run_repository),
):
    """End the review and start training.

    This is the only transition a person makes. Everything else is the
    reconciler's.
    """
    run = await _load_run(repository, run_id)
    if run.phase is not VoiceRunPhase.AWAITING_REVIEW:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This run is {run.phase}, so it is not waiting for review",
        )
    if not any(assignment.speaker_map.values()):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Assign at least one speaker to a character",
        )

    try:
        await gateway.set_speaker_map(run.video_id, assignment.speaker_map)
    except VoiceFactoryError as error:
        raise _unavailable(error) from error

    run.phase = VoiceRunPhase.COMMITTING
    run.commit_stage_index = 0
    run.voyicer_job_id = None
    run.error = None
    await repository.update_run(run)
    return run


async def _require_awaiting_review(
    repository: VoiceRunRepository, run_id: str
) -> VoiceRun:
    run = await _load_run(repository, run_id)
    if run.phase is not VoiceRunPhase.AWAITING_REVIEW:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This run is {run.phase}, so it is not waiting for review",
        )
    return run


@router.post(
    "/runs/{run_id}/assign",
    response_model=RunAssignResponse,
    status_code=status.HTTP_201_CREATED,
)
async def assign_run(
    run_id: str,
    assign_request: RunAssignRequest,
    repository: VoiceRunRepository = Depends(get_required_voice_run_repository),
    voice_repository: VoiceRepository = Depends(get_required_voice_repository),
    contribution_repository: VoiceContributionRepository = Depends(
        get_required_voice_contribution_repository
    ),
    gateway: VoiceFactoryGateway | None = Depends(get_voice_factory_gateway),
):
    """Map a run's speaker labels to Voices.

    Only assignment: it stores the mapping and writes one immutable
    voice_contributions row per assigned speaker. It does not commit the run
    or start training - those are POST .../commit and POST /voices/{id}/train,
    called separately, so relabeling a clip's speaker never has a side effect
    beyond recording it.

    The contribution rows are the durable record and they are written from
    Postgres alone, so this keeps working without a voice factory configured.
    The gateway is optional and supplies one thing: the video's title, which
    the factory owns. Without it the rows are the same, unnamed.
    """
    run = await _require_awaiting_review(repository, run_id)

    for voice_id in assign_request.assignments.values():
        if voice_id is None:
            continue
        if await voice_repository.get_voice(voice_id) is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"Voice {voice_id!r} not found"
            )

    assigned_speakers = {
        speaker_label: voice_id
        for speaker_label, voice_id in assign_request.assignments.items()
        if voice_id is not None
    }
    if not assigned_speakers:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Assign at least one speaker"
        )

    run.voice_assignments = assign_request.assignments

    titles = await resolve_video_titles(gateway, [run.video_id])

    now = datetime.now(UTC)
    contributions = [
        VoiceContribution(
            id=uuid.uuid4().hex,
            voice_id=voice_id,
            run_id=run.id,
            video_id=run.video_id,
            video_title=titles.get(run.video_id),
            speaker_label=speaker_label,
            created_at=now,
        )
        for speaker_label, voice_id in assigned_speakers.items()
    ]
    for contribution in contributions:
        await contribution_repository.create_contribution(contribution)

    await repository.update_run(run)
    return RunAssignResponse(
        run_id=run.id,
        voice_assignments=run.voice_assignments,
        contributions=contributions,
    )


@router.post(
    "/runs/{run_id}/commit",
    response_model=VoiceRun,
    status_code=status.HTTP_200_OK,
)
async def commit_run(
    run_id: str,
    repository: VoiceRunRepository = Depends(get_required_voice_run_repository),
):
    """End review once every speaker the operator cares about is assigned.

    Separate from assign_run on purpose: assigning a speaker must not finish
    the run by itself. This is the one call that does, and it does only that
    - no voice phase change, no training. Training starts when the operator
    calls POST /voices/{id}/train, per voice, from the training panel.
    """
    run = await _require_awaiting_review(repository, run_id)
    if not any(
        voice_id is not None for voice_id in run.voice_assignments.values()
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Assign at least one speaker before committing",
        )

    run.phase = VoiceRunPhase.COMMITTED
    await repository.update_run(run)
    return run


@router.get("/runs/{run_id}/logs", response_model=JobLog)
async def get_run_logs(
    run_id: str,
    offset: int = Query(default=0, ge=0),
    gateway: VoiceFactoryGateway = Depends(get_required_voice_factory_gateway),
    repository: VoiceRunRepository = Depends(get_required_voice_run_repository),
):
    run = await _load_run(repository, run_id)
    # A failed run has no running job, and its log is exactly what a person
    # opens the run to read. So fall back to the job that failed.
    job_id = run.voyicer_job_id or run.failed_job_id
    if not job_id:
        return JobLog(offset=0, content="", state=run.phase.value)
    try:
        payload = await gateway.get_job_logs(job_id, offset)
    except VoiceFactoryError as error:
        raise _unavailable(error) from error
    return JobLog(**payload)


@router.get("/runs/{run_id}/training", response_model=TrainingProgress)
async def get_training_progress(
    run_id: str,
    gateway: VoiceFactoryGateway = Depends(get_required_voice_factory_gateway),
    repository: VoiceRunRepository = Depends(get_required_voice_run_repository),
):
    run = await _load_run(repository, run_id)
    try:
        return await gateway.get_training_progress(run.primary_character)
    except VoiceFactoryError as error:
        raise _unavailable(error) from error


@router.post("/runs/{run_id}/retry", response_model=VoiceRun)
async def retry_run(
    run_id: str,
    repository: VoiceRunRepository = Depends(get_required_voice_run_repository),
    reconciler: VoiceRunReconciler = Depends(get_required_voice_run_reconciler),
):
    """Put a failed run back on the step it fell over on.

    The run keeps everything it already produced - clips, review decisions,
    checkpoints - because all of that lives on the voice factory host. It also
    keeps ingest_stage_index and commit_stage_index, which is what makes this a
    resume rather than a restart: a run that failed transcoding starts again at
    the download step, not at a fresh ingest. Only the dead job and the error go.
    """
    run = await _load_run(repository, run_id)
    if run.phase is not VoiceRunPhase.FAILED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This run is {run.phase}, so there is nothing to retry",
        )

    run.phase = run.failed_from_phase or VoiceRunPhase.DOWNLOADING
    run.failed_from_phase = None
    run.failed_job_id = None
    run.voyicer_job_id = None
    run.error = None
    run.error_count = 0
    await repository.update_run(run)
    reconciler.wake(run.id)
    return run


@router.post("/jobs/{job_id}/events", status_code=status.HTTP_204_NO_CONTENT)
async def post_job_event(
    job_id: str,
    event: VoiceWebhookEvent,
    repository: VoiceRunRepository = Depends(get_required_voice_run_repository),
    reconciler: VoiceRunReconciler = Depends(get_required_voice_run_reconciler),
    token: str | None = Header(default=None, alias=WEBHOOK_TOKEN_HEADER),
):
    """Accept one job event from the voice factory.

    This is a report, never a command. It records progress and wakes the
    reconciler, which then asks the factory what really happened and owns the
    phase change. So a lost webhook costs latency and nothing else: the
    reconcile timer catches the same change on its next pass.

    Answers 204 for a job no run claims. The factory runs jobs this service
    never started, and a webhook failure must not stop one.
    """
    if not settings.VOICE_WEBHOOK_TOKEN:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Webhooks are not configured. Set VOICE_WEBHOOK_TOKEN.",
        )
    if token != settings.VOICE_WEBHOOK_TOKEN:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook token")

    run = await repository.find_run_by_job_id(job_id)
    if run is None:
        return None

    if event.epoch is not None or event.loss is not None:
        await repository.record_progress(run.id, event.epoch, event.loss)
    reconciler.wake(run.id)
    return None


@router.get("/events")
async def get_voice_events(
    request: Request,
    repository: VoiceRunRepository = Depends(get_required_voice_run_repository),
    event_stream: VoiceEventStream = Depends(get_required_voice_event_stream),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Stream voice run state to the browser, over one connection for the page.

    Reuses the AG-UI encoder the agent endpoint already speaks, so the browser
    has one event grammar rather than two. Every event carries an SSE `id:`,
    which is the Redis Stream ID, so a reconnecting EventSource sends back
    Last-Event-ID on its own and picks up exactly where it stopped.
    """
    encoder = EventEncoder(accept=request.headers.get("accept"))
    heartbeat_milliseconds = int(settings.VOICE_EVENT_HEARTBEAT_SECONDS * 1000)

    async def event_stream_body():
        if last_event_id:
            # A reconnect. The client already has state, so replay rather than
            # start over, and only fall back to a snapshot if nothing replays.
            position = last_event_id
            replayed = await event_stream.read_after(position, REPLAY_LIMIT)
            for event in replayed:
                yield _encode_voice_event(encoder, event)
            if replayed:
                position = replayed[-1].event_id
        else:
            # A fresh connection. Capture the stream position first, then read
            # the snapshot: anything published while the snapshot is being
            # built then arrives as a replayed event rather than being lost.
            position = await event_stream.current_position()
            runs = await repository.list_runs(limit=200)
            yield encoder.encode(
                StateSnapshotEvent(
                    type=EventType.STATE_SNAPSHOT,
                    snapshot={"runs": [run.model_dump(mode="json") for run in runs]},
                )
            )
            for event in await event_stream.read_after(position, REPLAY_LIMIT):
                yield _encode_voice_event(encoder, event)
                position = event.event_id

        async for batch in event_stream.follow(position, heartbeat_milliseconds):
            if not batch:
                # An SSE comment. Keeps a proxy in the middle from deciding the
                # connection went idle and closing it.
                yield ": ping\n\n"
                continue
            for event in batch:
                yield _encode_voice_event(encoder, event)

    return StreamingResponse(
        event_stream_body(),
        media_type=encoder.get_content_type(),
        headers={
            "Cache-Control": "no-cache",
            # Tells nginx-style reverse proxies not to buffer the stream, which
            # would hold every update back until the connection closed.
            "X-Accel-Buffering": "no",
        },
    )


def _encode_voice_event(encoder: EventEncoder, event: VoiceEvent) -> str:
    """One run update or log chunk, with the SSE id the browser replays from.

    The AG-UI encoder writes the `data:` line only, so the `id:` line goes on
    the front here. That is what makes EventSource send Last-Event-ID for us.
    """
    name = EVENT_RUN_LOG if isinstance(event.data, VoiceLogChunk) else EVENT_RUN_UPDATED
    encoded = encoder.encode(
        CustomEvent(
            type=EventType.CUSTOM,
            name=name,
            value=event.data.model_dump(mode="json"),
        )
    )
    return f"id: {event.event_id}\n{encoded}"
