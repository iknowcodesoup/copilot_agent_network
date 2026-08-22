# Brownfield Evidence

Every claim below was verified against the working tree on 2026-08-20. File and
line references point at the code as it stood after the `voice_factory` rename.

## 1. The outage: one directory level

`star-trek-voyicer` had its package flattened from `src/jeanlucrecord/` to
`src/`, with `pyproject.toml` patched to keep the import name working. Imports
resolved. Path roots did not.

Every path root counts parents from `__file__`. Removing one directory level
moved each root up one:

| Root | Before flatten | After flatten |
| --- | --- | --- |
| `filesystem_layout.APP_DIR` | `apps/jeanlucrecord` | `apps` |
| `config.APP_ROOT` | `apps/jeanlucrecord` | `apps` |
| `diarization.DIARIZER_DIR` | `apps/jeanlucrecord/diarizer` | `apps/diarizer` |

`WORK_DIR` became `apps/work`, which does not exist.

**The failure was silent by construction.** `routes/videos.py:36-38` reads:

```python
youtube_dir = filesystem_layout.WORK_DIR / "youtube"
if not youtube_dir.exists():
    return {"videos": []}
```

A missing directory and an empty one produce the same HTTP 200. `routes/characters.py`
has the same shape. This is the behaviour CAP-2 and the "no route may report a
missing directory as an empty collection" constraint remove.

Data was never lost: `work/youtube/` holds `CTGlPCWlViw`, `M3CT_qVzL84`, and
`ghSq2qlwrs0`, with 119 and 64 review rows in the first two.

The rename to `src/voice_factory/` restored the original depth, so all four path
roots resolve correctly again with no change to the expressions themselves.

## 2. The videos view never called the factory

`apps/agentic-executor/src/components/videos-view.tsx:14-32` builds its list from
`useVoiceRuns()` — a Postgres read — and synthesises a `VideoResult` per run:

```ts
videoId: run.videoId ?? run.id,
title: run.videoTitle ?? "Untitled video",
```

Two defects here.

`run.videoId ?? run.id` substitutes a **run id** when the run has no video id.
That value can never resolve against the factory, so the card is unreachable by
construction.

The view kept rendering during the outage because Postgres still answered. That
is why the dashboard showed videos while the factory reported none.

## 3. The pass-through routes have no callers

`apps/pythonapi/pythonapi/routes/voice.py:118-145` defines:

- `GET /api/voice/videos`
- `GET /api/voice/videos/{video_id}/speakers`

Both are correct. Both delegate straight to the gateway. A search across
`apps/agentic-executor/` finds **no caller for either**. The endpoint that would
have shown the truth was already built and never wired up.

## 4. `video_title` is written nowhere

`routes/voice.py:200-210` constructs every `VoiceRun`. It sets `source_url` and
the resolved `video_id`. It never sets `video_title`.

A search for any assignment to `video_title` across `apps/pythonapi/` returns
only repository round-trips: `repositories/voice_runs.py:316`, `:365`, `:395`,
and `repositories/voice_contributions.py:94`. Nothing ever gives it a value.

So the column is always `NULL`, and `videos-view.tsx` renders "Untitled video"
for every card. The front end works around this in
`apps/agentic-executor/src/lib/store.ts:208` with a client-side `guessTitle`.

This matters for the design: `video_title` is not a stale mirror. It is an empty
column with a client-side guess standing in for it.

## 5. The factory stores no title either

`core/clip_review.py:video_summary()` returns exactly:

```python
{"video_id": ..., "diarized": ..., "reviewed": ..., "clip_count": ...}
```

A video directory holds `clips/`, `clips.json`, `diarization.json`, `full.wav`,
`review.csv`, and `transcript.json`. No title, url, duration, or channel.

A title appears only in `core/youtube_search.py:52`, from yt-dlp metadata at
search time. It is never persisted.

**Neither service stores the video title.** That is why CAP-5 adds `meta.json`
rather than backfilling a column.

## 6. Which fields are genuinely duplicated

Verified by tracing each writer.

**Factory computes these; Postgres keeps a second copy:**

| Column | Factory source |
| --- | --- |
| `speaker_map` | `speaker_map.json` per video directory, written by `routes/speaker_map.py` |
| `clip_count` | counted from `review.csv` — `core/clip_review.py:video_summary()` |
| `approved_count` | counted from `review.csv` `keep` column |

In the orchestrator these are set at `core/voice_pipeline_graph.py:263-264` and
`routes/voice.py:309-310`, both by counting clips fetched from the factory. They
are caches of a remote computation.

**Orchestrator owns these; the factory has no equivalent:**

`voice_assignments` maps a speaker label to a **Voice id**. The factory has no
Voice concept — `models/voice.py:120` says so directly: "Distinct from
speaker_map: this is DB-only". It must stay.

Also orchestrator-owned: `phase`, `diarize`, `num_speakers`, `voyicer_job_id`,
the stage indices, error state, the lease columns, and timestamps.

## 7. The run-keyed indirection

`routes/voice.py:247-283` serves the speaker board. It takes a `run_id`, loads
the run, reads `run.video_id`, then calls the factory with that video id. The
factory itself is keyed on `video_id` throughout — `routes/videos.py` takes no
character and no run.

The translation buys nothing and adds two failure modes: a run with a null
`video_id` returns 409, and a run pointing at a video the factory has dropped
returns a confusing 503. CAP-4 removes the indirection.

## 8. The rename is invisible to the orchestrator

A search across `apps/pythonapi/` and `apps/agentic-executor/` for
`src/jeanlucrecord`, `jeanlucrecord.app`, or `jeanlucrecord.cli` returns nothing.
The orchestrator reaches the factory only over HTTP.

That is the boundary this spec formalises. It is already correct at the transport
layer. What is missing is the same discipline for the data.
