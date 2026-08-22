"""Resolve video titles from the voice factory, at read time.

The factory owns the title: it lives in meta.json beside the clips, so every
character that claims the same video reads the same name. Nothing here keeps a
copy, which is why a title is looked up rather than stored.

One list call answers for every id a caller holds, so a page of runs costs one
request rather than one per run.
"""

import logging
from collections.abc import Iterable

from pythonapi.core.voice_factory_gateway import (
    VoiceFactoryError,
    VoiceFactoryGateway,
)

logger = logging.getLogger(__name__)


async def resolve_video_titles(
    gateway: VoiceFactoryGateway | None, video_ids: Iterable[str | None]
) -> dict[str, str]:
    """Title by video id, for the ids that the factory still knows.

    An id the factory does not list is left out: that run points at a video
    that is gone, and the caller shows it as orphaned rather than inventing a
    name for it.

    A missing or unreachable factory yields no titles instead of raising. The
    caller's own facts live in Postgres and must still answer - a name is the
    only thing lost. Where a stale name would mislead, such as the videos
    view, the caller reads the factory directly instead of calling this.
    """
    wanted = {video_id for video_id in video_ids if video_id}
    if gateway is None or not wanted:
        return {}
    try:
        videos = await gateway.list_videos()
    except VoiceFactoryError as error:
        logger.warning("Could not read video titles from the voice factory: %s", error)
        return {}
    return {
        video["video_id"]: video.get("title") or video["video_id"]
        for video in videos
        if video.get("video_id") in wanted
    }
