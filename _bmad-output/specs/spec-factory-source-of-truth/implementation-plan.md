# Implementation Plan

Ordered so the system works after every step. Factory first, because the
orchestrator cannot stop copying a fact until the factory serves it.

Steps 1 and 2 land in `star-trek-voyicer`. Steps 3 to 7 land in
`copilot_agent_network`.

---

## Step 1 — A missing work directory raises

**Repo:** `star-trek-voyicer`

This is the fix that makes the 2026-08-20 outage impossible to repeat quietly.
Do it first and alone. It is small, and it changes failure behaviour that every
later step depends on.

In `infrastructure/filesystem_layout.py`, add a check that distinguishes a
missing `WORK_DIR` from an empty one:

```python
def require_work_dir() -> Path:
    """WORK_DIR, or a 500 naming it.

    An absent WORK_DIR means the app is misconfigured, not that the operator has
    ingested nothing. Reporting it as an empty list is what hid a path-resolution
    bug for a full day.
    """
```

Call it from `routes/videos.py:get_videos()` and
`routes/characters.py:get_characters()`, replacing the
`if not ...exists(): return {"...": []}` early returns.

`GET /health` already returns `WORK_DIR`. Leave it — it is the fastest manual
check and stays useful.

**Done when:** with `WORK_DIR` pointed at a nonexistent path, `GET /videos`
returns 500 naming the path, and `GET /health` still answers.

---

## Step 2 — The factory owns video metadata

**Repo:** `star-trek-voyicer`

Write `meta.json` into the video directory at ingest. Shape is in
`data-model.md`.

- `core/youtube_ingest.py` — write `meta.json` when the video is first fetched.
  The title and duration are already in the yt-dlp metadata that
  `core/youtube_search.py:52` reads; carry them through instead of discarding
  them.
- `core/clip_review.py:video_summary()` — merge `meta.json` into the returned
  dict. Missing file yields null fields and `video_id` as the fallback title.
  Existing videos must keep working with no backfill.
- `repositories/` — add a `video_meta_repository.py` for the read and write, to
  match the existing repository-per-file pattern.

**Done when:** `GET /videos` returns a title for a newly ingested video, and the
three existing videos still list with null titles.

---

## Step 3 — Video-keyed clip and speaker routes

**Repo:** `copilot_agent_network`

Add routes that address a video directly, alongside the existing run-keyed ones.
Do not delete the old routes yet — step 6 moves the front end, and this keeps
both working in between.

In `routes/voice.py`, add:

- `GET /api/voice/videos/{video_id}/clips` — the speaker board, grouped as
  `get_speaker_board` does today
- `PATCH /api/voice/videos/{video_id}/clips`
- `GET /api/voice/videos/{video_id}/clips/{clip_id}/audio`

Move the grouping logic out of the route into `core/`, so both the run-keyed and
video-keyed paths call one function rather than two copies.

Fill `assigned_character` from the `speaker_map` in the factory's
`GET /videos/{id}/clips` payload, not from `run.speaker_map`.

**Done when:** the video-keyed speaker board returns the same payload as the
run-keyed one for a run that has a `video_id`, and also works for a video with no
run.

---

## Step 4 — Make the proxy transparent

**Repo:** `copilot_agent_network`

`VideoSummary` and `VideoSpeakerSummary` in `models/voice.py` re-model what the
factory already shaped. That is the second schema CAP-6 removes: a field added on
the factory is invisible here until someone edits both.

Have the video pass-through routes return the factory payload unchanged. Keep the
route so there is one origin, drop the re-modelling.

Keep strict models for anything the orchestrator writes or interprets —
`VoiceRun`, `SpeakerBoard`, the request bodies. This step only relaxes the
read-only pass-throughs.

**Done when:** adding a field to the factory's `video_summary()` makes it visible
at `GET /api/voice/videos` with no change in `apps/pythonapi/`.

---

## Step 5 — Drop the four columns

**Repo:** `copilot_agent_network`

Order within the step: stop writing, then stop reading, then drop.

1. `core/voice_pipeline_graph.py:263-264` and `routes/voice.py:309-310` — stop
   assigning `clip_count` and `approved_count`.
2. `repositories/voice_runs.py` — remove all four columns from
   `_row_from_run`, `_run_from_row`, and the update path at `:316-326`.
3. `repositories/voice_contributions.py:94` — stop reading
   `run_row.video_title`. Resolve the title from the factory at read time.
4. `models/voice.py` — remove the four fields from `VoiceRun`. Where a caller
   needs a count, read it from the factory's video summary.
5. `models/orm.py:71-128` — drop the four columns from `VoiceRunRow`.
6. Migrate per `data-model.md`.

`core/voice_agent_tools.py:110` returns `run.video_title` to the chat agent.
Point it at the resolved title.

**Done when:** no reference to the four names remains outside a migration, and
the run list still serves the dashboard.

---

## Step 6 — The videos view reads the factory

**Repo:** `copilot_agent_network`

Add a `useVideos()` hook in `lib/voice_api.ts` against `GET /api/voice/videos` —
the endpoint that has existed unused since it was written.

Rewrite `components/videos-view.tsx`:

- List videos from `useVideos()`, not from `useVoiceRuns()`.
- Delete the `videoId: run.videoId ?? run.id` fallback. It fabricates an
  unresolvable id.
- Join runs onto videos by `video_id` to show run phase per video.
- A run whose `video_id` is absent from the video list renders as orphaned, with
  no review action (CAP-3).
- A failed video fetch shows an error state. It must not fall back to Postgres
  (CAP-2).

Remove the `guessTitle` workaround at `lib/store.ts:208`. The title now arrives
from the factory.

Point the review screen at the video-keyed routes from step 3.

**Done when:** stopping the factory produces an error state in the videos view,
and starting it lists three videos with real titles.

---

## Step 7 — Retire the run-keyed clip routes

**Repo:** `copilot_agent_network`

With the front end moved, delete the run-keyed clip, speaker, and audio routes
and the `run_id → video_id` translation. Keep `GET /api/voice/runs/{id}` and the
rest of the run lifecycle.

**Done when:** `routes/voice.py` has no route that loads a run only to read its
`video_id`.

---

## Testing

Python only. Per the standing project decision, **no Playwright, e2e, or UI tests
for `agentic-executor`** — the UX is being redesigned, and tests written against
the current views would be discarded.

Cover in `apps/pythonapi/tests/`:

- a missing `WORK_DIR` raises rather than returning an empty list (step 1)
- the video-keyed speaker board matches the run-keyed one for the same video
- a video with no run is reviewable
- a run whose `video_id` is absent from the factory is reported as orphaned
- `voice_assignments` survives the migration that drops `speaker_map`

Cover in `star-trek-voyicer/tests/`:

- `video_summary()` merges `meta.json` when present
- `video_summary()` returns null fields and does not raise when it is absent

Claude does not run these directly. `litert-subagent` covers `nx` tasks in
`copilot_agent_network`; `star-trek-voyicer` has no equivalent task defined, so
`just test-jeanlucrecord` needs a person or a new task entry.
