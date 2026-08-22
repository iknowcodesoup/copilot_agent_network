"""One door to the voice factory, for everything the factory owns.

The factory owns videos, clips, review decisions, characters, and job logs.
This service owns run phase state, the AG-UI event grammar, and the agent. A
route here that retyped a factory shape would be a second definition of data
this service never reads, so there are none: one forwarder carries every
factory-owned call through untouched.

Modelled on openai_proxy.py, for the same reason. LiteLLM's API is not
duplicated in this service either - it is forwarded. A field the factory adds,
or a route it grows, reaches the browser with no change here.

The browser talks to this origin alone, which is the one thing the hop buys:
the factory needs no CORS entry and never faces the network.

Typed gateway calls still exist, and should. VoiceFactoryGateway earns its
models where Python reads the fields - build_speaker_board, the reconciler,
resolve_video_id. This proxy is for the calls nothing here inspects.
"""

from collections.abc import Iterable

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from pythonapi.config import settings

router = APIRouter(prefix="/voice-factory", tags=["Voice Factory"])

# Hop-by-hop headers, plus the ones httpx must set itself from the new body.
_REQUEST_HEADER_BLOCKLIST = {"connection", "content-length", "host"}
_RESPONSE_HEADER_BLOCKLIST = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# Audio is the one factory response worth streaming: a clip is megabytes, and
# buffering it whole would hold a worker for the length of the download.
_STREAMED_CONTENT_PREFIX = "audio/"
_STREAM_CHUNK_SIZE = 64 * 1024


def _copy_headers(
    headers: Iterable[tuple[str, str]], blocked: set[str]
) -> dict[str, str]:
    return {key: value for key, value in headers if key.lower() not in blocked}


def _require_factory_url() -> str:
    """The factory is optional, exactly as it is for every typed route.

    Unset means the feature is off, not that the deployment is broken, so this
    answers 503 rather than raising - same contract as
    get_required_voice_factory_gateway.
    """
    if not settings.VOICE_FACTORY_URL:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The voice factory is not configured. Set VOICE_FACTORY_URL.",
        )
    return settings.VOICE_FACTORY_URL.rstrip("/")


@router.api_route(
    "/{upstream_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def proxy_voice_factory_request(
    request: Request, upstream_path: str
) -> Response:
    """Forward one call to the factory and return its answer unchanged.

    The factory's status is the browser's status. A 404 for an unknown video
    and a 409 for a clip already labelled for another character are both the
    operator's answer to read, so neither is reshaped into a 502 here. Only a
    factory that did not answer at all becomes one.
    """
    base_url = _require_factory_url()
    body = await request.body()
    headers = _copy_headers(request.headers.items(), _REQUEST_HEADER_BLOCKLIST)
    url = f"{base_url}/{upstream_path.lstrip('/')}"

    client = httpx.AsyncClient(timeout=settings.VOICE_FACTORY_TIMEOUT_SECONDS)
    upstream = client.build_request(
        request.method,
        url,
        content=body,
        headers=headers,
        params=request.query_params,
    )
    try:
        upstream_response = await client.send(upstream, stream=True)
    except httpx.HTTPError as error:
        await client.aclose()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"The voice factory did not answer: {error}",
        ) from error

    response_headers = _copy_headers(
        upstream_response.headers.items(), _RESPONSE_HEADER_BLOCKLIST
    )
    media_type = upstream_response.headers.get("content-type")

    if media_type and media_type.startswith(_STREAMED_CONTENT_PREFIX):

        async def stream_upstream():
            try:
                async for chunk in upstream_response.aiter_bytes(_STREAM_CHUNK_SIZE):
                    yield chunk
            finally:
                await upstream_response.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_upstream(),
            status_code=upstream_response.status_code,
            headers=response_headers,
            media_type=media_type,
        )

    try:
        content = await upstream_response.aread()
    finally:
        await upstream_response.aclose()
        await client.aclose()

    return Response(
        content=content,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=media_type,
    )
