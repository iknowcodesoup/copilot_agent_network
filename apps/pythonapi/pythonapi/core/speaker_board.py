"""Group one video's clips by speaker, for the review screen.

Composed here rather than by the factory, because the grouping is a view: the
clips and the speaker map are the factory's facts, and this turns them into
the shape the board renders. One function, so the video-keyed route and any
other caller cannot drift into two different boards over the same clips.
"""

from collections import defaultdict

from pythonapi.models.voice import (
    ClipSummary,
    SpeakerBoard,
    SpeakerGroup,
    VideoClips,
)


def build_speaker_board(
    video_clips: VideoClips, run_id: str | None = None
) -> SpeakerBoard:
    """Every clip grouped under the speaker it belongs to.

    assigned_character comes from the factory's speaker map, which is the one
    copy of that fact. run_id names the run looking at this video, and is None
    when no run claims it.
    """
    grouped: dict[str | None, list[ClipSummary]] = defaultdict(list)
    for clip in video_clips.clips:
        grouped[clip.speaker_label].append(clip)

    speakers = [
        SpeakerGroup(
            speaker_label=speaker_label,
            assigned_character=video_clips.speaker_map.get(speaker_label)
            if speaker_label
            else None,
            clip_count=len(group),
            kept_count=sum(1 for clip in group if clip.keep),
            total_duration_sec=sum(clip.duration_sec or 0.0 for clip in group),
            clips=group,
        )
        # None sorts last: the rejected group is the least interesting
        for speaker_label, group in sorted(
            grouped.items(), key=lambda item: (item[0] is None, item[0] or "")
        )
    ]
    return SpeakerBoard(
        video_id=video_clips.video_id, run_id=run_id, speakers=speakers
    )
