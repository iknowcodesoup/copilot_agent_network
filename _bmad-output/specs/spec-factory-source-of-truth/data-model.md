# Data Model

## The ownership rule

One sentence decides every case:

> **The factory owns what it can recompute from `work/`. The orchestrator owns
> what would be lost if `work/` were deleted.**

Clip counts, keep decisions, and speaker labels come from `review.csv`. Delete
the database and they survive. They belong to the factory.

Run phase, lease ownership, retry history, and Voice assignments exist only in
Postgres. Delete `work/` and they are still meaningful. They belong to the
orchestrator.

## `voice_runs`, before and after

| Column | Owner | Change |
| --- | --- | --- |
| `id` | orchestrator | keep |
| `primary_character` | orchestrator | keep |
| `source_url` | orchestrator | keep — the factory never stores it |
| `video_id` | shared key | keep, stays nullable |
| `video_title` | neither today | **drop** — always NULL, see CAP-5 |
| `phase` | orchestrator | keep — this is the state machine |
| `diarize` | orchestrator | keep — a run parameter |
| `num_speakers` | orchestrator | keep — a run parameter |
| `speaker_map` | **factory** | **drop** — mirrors `speaker_map.json` |
| `voice_assignments` | orchestrator | keep — Voice ids, no factory equivalent |
| `voyicer_job_id` | orchestrator | keep |
| `ingest_stage_index` | orchestrator | keep |
| `commit_stage_index` | orchestrator | keep |
| `clip_count` | **factory** | **drop** — counted from `review.csv` |
| `approved_count` | **factory** | **drop** — counted from `review.csv` |
| `current_epoch` | factory | keep for now — see open question |
| `current_loss` | factory | keep for now — see open question |
| `error` | orchestrator | keep |
| `error_count` | orchestrator | keep |
| `failed_from_phase` | orchestrator | keep |
| `failed_job_id` | orchestrator | keep |
| `leased_until` | orchestrator | keep |
| `lease_owner` | orchestrator | keep |
| `created_at` | orchestrator | keep |
| `updated_at` | orchestrator | keep |

Four columns drop. Nineteen stay. This is a smaller change than the "stop
duplicating" framing suggests, because most of `voice_runs` is genuinely
orchestration state.

`voice_contributions` and `voices` are unchanged, except that
`_contribution_from_row` stops reading `run_row.video_title`. The
`VoiceContribution` model keeps `video_id` and resolves `video_title` from the
factory at read time.

## `speaker_map` versus `voice_assignments`

These look alike and are not. Keeping both is correct.

| | `speaker_map` | `voice_assignments` |
| --- | --- | --- |
| Maps | speaker label → character | speaker label → Voice id |
| Stored in | `speaker_map.json`, per video | Postgres, per run |
| Owner | factory | orchestrator |
| Meaning of `None` | discard this speaker's clips | discard this speaker's clips |

The factory needs the character map to route clips into a dataset directory. The
orchestrator needs the Voice map because a Voice is its own concept. Dropping the
Postgres copy of `speaker_map` does not touch `voice_assignments`.

Where the speaker board shows `assigned_character`, it reads the `speaker_map`
the factory already returns in the `GET /videos/{id}/clips` payload
(`routes/videos.py:77`). No extra call.

## New: `meta.json` per video directory

The factory gains one small write. At ingest, `youtube_ingest` writes
`work/youtube/{video_id}/meta.json`:

```json
{
  "video_id": "CTGlPCWlViw",
  "title": "...",
  "url": "https://www.youtube.com/watch?v=CTGlPCWlViw",
  "duration_sec": 1893.0,
  "channel": "...",
  "ingested_at": "2026-08-12T19:42:00Z"
}
```

`video_summary()` merges it into its return value. Absent, the fields come back
null and `video_id` stands in for the title, so existing videos keep working
without a backfill.

### Why here and not in Postgres

A title belongs to the video, and the factory owns videos.

The deciding case is reuse. `routes/videos.py` is explicit that a video is
ingested once and shared across every character that later claims it. Store the
title on the run and only the first claimant has it — the second character sees
"Untitled video" for a video the system already knows. Store it with the video
and both see the same name.

**Alternative considered.** Populate `voice_runs.video_title` at run creation
from the search result. Cheaper, no factory change, and it fixes the "Untitled
video" symptom immediately. Rejected because it re-creates the exact pattern this
spec removes: a fact about a video, held per run, that nothing reconciles. It
also leaves a video ingested outside a run with no title at all.

## Migration

`Base.metadata.create_all` does not alter an existing table, so dropping columns
needs a deliberate step.

Development: drop and recreate `voice_runs`. The table holds run state for three
videos, all re-creatable, and the clips themselves live on the factory.

The four dropped columns carry nothing worth preserving:

- `video_title` is `NULL` in every row.
- `speaker_map`, `clip_count`, and `approved_count` are recomputable from the
  factory at any time.

If a run must survive, keep `id`, `source_url`, `video_id`, `phase`, and
`voice_assignments`. That is enough to reconstruct one.

Alembic remains a separate task. This spec does not introduce it, but it is the
correct home for this change if it lands first.

## Open question: `current_epoch` and `current_loss`

These mirror the factory's training log, which `GET /characters/{c}/training`
serves. By the ownership rule they should drop.

They are kept because they behave differently from the other three. A finished
job stops serving progress, but the dashboard still wants to show the last epoch
reached. That makes them a display snapshot, not a cache of live state.

Resolve this when the training view is next touched. Either the factory serves
final training state per character after a job ends, in which case both columns
drop, or they stay and are documented as an intentional snapshot. Do not decide
it inside this spec.
