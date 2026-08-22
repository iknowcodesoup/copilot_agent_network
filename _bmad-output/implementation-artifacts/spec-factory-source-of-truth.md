---
title: 'Factory Is The Source Of Truth'
type: 'refactor'
created: '2026-08-20'
status: 'in-review'
review_loop_iteration: 0
baseline_commit: '1378689d2f1943bb20a47dde13d9ebfe64f10d38' # copilot_agent_network
baseline_commit_voice_factory: '1404adfec6b1065b85fc97dafab434a0a99fb15a' # star-trek-voyicer
context:
  - '{project-root}/_bmad-output/specs/spec-factory-source-of-truth/data-model.md'
  - '{project-root}/_bmad-output/specs/spec-factory-source-of-truth/verification.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Two services write the same facts and no rule says which one is right. The voice factory computes clip counts, speaker labels, and keep decisions from `review.csv`. The orchestrator keeps a second copy in `voice_runs` that nothing reconciles. On 2026-08-20 a path bug pointed `WORK_DIR` at a directory that does not exist, `GET /videos` answered `{"videos": []}` with HTTP 200, and the dashboard kept rendering cards from the stale Postgres copy. The copy hid the outage.

**Approach:** Give every fact exactly one writer. The factory owns what it can recompute from `work/`. The orchestrator owns what would be lost if `work/` were deleted. Make a missing work directory raise instead of reporting empty, give the factory a `meta.json` per video so it owns the title, add video-keyed clip routes so no run lookup can break a read, drop the four duplicated columns, and point the videos view at the factory.

## Boundaries & Constraints

**Always:**
- The factory keeps its filesystem and gains no database.
- `voice_runs.video_id` stays, and stays nullable. It is the only join between the two systems.
- `voice_assignments` stays in Postgres. It maps a speaker label to a Voice id, and the factory has no Voice concept. It is not a mirror of `speaker_map`.
- The browser never calls the factory. Clips, audio, and video lists keep proxying through `/api/voice/...`, so there is one origin and one CORS entry.
- A missing `WORK_DIR` raises. A present `WORK_DIR` with no `work/youtube/` subdirectory returns an empty list. That is a fresh install, not a fault.
- SQLAlchemy 2.0 async only. No raw SQL.
- Order the work so the system runs after every step. The factory must serve a fact before the orchestrator stops copying it.

**Ask First:**
- Dropping or recreating the `voice_runs` table. `Base.metadata.create_all` does not alter an existing table, so this needs a deliberate destructive step. HALT and get human approval before running it.
- Any change to `current_epoch` or `current_loss`. They are an open question recorded in `data-model.md` and are out of scope here.
- Deleting a route or a column that the Execution list below does not name.

**Never:**
- Do not change the LangGraph phase machine. `voice_runs.phase` stays the state machine and `VoiceRunReconciler` stays its only writer.
- Do not change the lease columns, the error counters, or the retry route.
- Do not adopt GraphQL. The problem is ownership, not query shape.
- Do not merge the two repositories. The GPU boundary is why they are split.
- Do not report a missing directory as an empty collection anywhere.
- Do not add Playwright, e2e, or any UI test for `agentic-executor`. Standing project decision, because the UX is being redesigned.
- Do not delete `voice_event_stream.tsx` or `query-provider.tsx`. They are unmounted, but they are the documented SSE architecture, not dead weight.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Work dir missing | `WORK_DIR` points at a path that does not exist | `GET /videos` and `GET /characters` return 500 naming the path | `HTTPException(500, ...)` naming `WORK_DIR` |
| Fresh install | `WORK_DIR` exists, `work/youtube/` does not | `GET /videos` returns `{"videos": []}` with HTTP 200 | N/A |
| Video with meta | `meta.json` present in the video directory | `video_summary()` merges title, url, duration, channel | N/A |
| Video without meta | `meta.json` absent | Fields return null, `video_id` stands in for the title, no raise | N/A |
| Clips by video id | `video_id` exists, no `voice_runs` row references it | `GET /api/voice/videos/{video_id}/clips` returns its speaker groups | N/A |
| Orphaned run | `voice_runs` row whose `video_id` is absent from `GET /videos` | The videos view marks the run orphaned and offers no review action | N/A |
| Factory unreachable | `VOICE_FACTORY_URL` set, service stopped | The videos view shows an error state | Must not fall back to Postgres |
| Factory not configured | `VOICE_FACTORY_URL` unset | Every `/api/voice` route answers 503, as today | Unchanged |
| Speaker label unmapped | `speaker_map` holds no entry for a label | `assigned_character` is null | N/A |

</frozen-after-approval>

## Code Map

### star-trek-voyicer (`apps/jeanlucrecord/`)

- `src/voice_factory/infrastructure/filesystem_layout.py` -- 36 lines. `APP_DIR:21` counts four parents from `__file__`. `WORK_DIR:22`. Already imports `HTTPException, status` at `:13`. `check_name:30-36` is the naming and raise model to copy. The comment at `:15-20` names the empty-list hazard and must be rewritten.
- `src/voice_factory/routes/videos.py` -- routes at `:28 get_videos`, `:47 get_video_speakers`, `:69 get_clips`, `:83 patch_clips`, `:148 get_clip_audio`. The early return at `:36-38` guards `WORK_DIR / "youtube"`, NOT `WORK_DIR`. `get_clips` returns `speaker_map` at `:78`, loaded inline at `:72-75`. `kept_count` computed at `:58`.
- `src/voice_factory/routes/characters.py` -- `:12-13` is the true `WORK_DIR` guard and the primary `require_work_dir()` call site.
- `src/voice_factory/core/clip_review.py` -- `video_summary(video_directory):51` returns exactly four keys at `:59-64`. `clip_count` counted at `:57-58`. Helpers `video_dir:25`, `review_path:42`, `clip_from_row:67`.
- `src/voice_factory/core/youtube_ingest.py` -- artifact-name constants at `:12-16`, where `META_NAME` belongs. `write_json:23-27`. `resolve_video_id:42-52` runs yt-dlp with `--print "%(id)s"` and discards title, duration, and channel. `download_audio:62` first creates the video directory.
- `src/voice_factory/core/youtube_search.py` -- `:49-59` already normalizes exactly the fields `meta.json` needs: `video_id`, `title:52`, `duration_sec`, `channel`, `thumbnail_url`, `url`. Reuse this shape.
- `src/voice_factory/repositories/speaker_map_repository.py` -- the pattern to copy. Filename constant `:10`, sync `read_speaker_map:13` returning `{}` when absent, `write_speaker_map_file:20` doing mkdir plus `indent=2` and returning the Path. This layer holds no try/except anywhere.
- `tests/routes/conftest.py` -- `work_dir:18-21` monkeypatches `filesystem_layout.WORK_DIR` to `tmp_path`. `tmp_path` always exists, so a missing-directory test must set it to `tmp_path / "nope"`. `client:24-27`.
- `tests/routes/test_videos.py` -- `build_video:24-31` is the fixture helper to extend with a `meta.json`. `:167` asserts the exact `video_summary` key set. `:185` asserts `{"videos": []}`.
- `justfile` -- `serve-jeanlucrecord:30`, `test-jeanlucrecord:34`.

### copilot_agent_network — backend (`apps/pythonapi/`)

- `pythonapi/models/orm.py` -- `VoiceRunRow:71`. Drop `video_title:91`, `speaker_map:95-96`, `clip_count:109`, `approved_count:110`. Nineteen columns stay. Indexes at `:81-85` are unaffected. No Alembic. `create_all` runs at `infrastructure/postgres_client.py:28`.
- `pythonapi/models/voice.py` -- `VoiceRun:102`. Drop `video_title:114`, `speaker_map:118-119`, `clip_count:130`, `approved_count:131`. `VideoSummary:64` has no title field. `VideoSpeakerSummary:77`. `SpeakerBoard:191` and `SpeakerGroup:176` are orchestrator-composed and stay. `ClipDecision:197` and `SpeakerAssignmentRequest:208`, whose `speaker_map:214` is the approve payload, both stay.
- `pythonapi/repositories/voice_runs.py` -- `_row_from_run:359` writes the four at `:365,:369,:374,:375`. `_run_from_row:389` reads them at `:395,:399,:404,:405`. `update_run:309` writes them at `:316,:320,:325,:326`. `InMemoryVoiceRunRepository:85` stores whole objects and needs no edit.
- `pythonapi/repositories/voice_contributions.py` -- `_contribution_from_row:86` reads `run_row.video_title` at `:94`. The join is at `:65-67`.
- `pythonapi/models/voices.py` -- `VoiceContribution:47` carries `video_title:59` as a denormalized projection.
- `pythonapi/routes/voice.py` -- router prefix at `:66`. Run-keyed clip routes to retire: `get_speaker_board:248`, `update_clips:287`, `get_clip_audio:467`. Video pass-throughs: `list_videos:119`, `get_video_speakers:134`. Four-column sites: `run.speaker_map.get:270`, the recount at `:309-310` and its response body at `:312`, `run.speaker_map =` at `:344`, and `video_title=run.video_title` at `:419`. `VoiceRun` is built at `:200`. Helpers `_unavailable:81`, `_load_run:88`.
- `pythonapi/core/voice_factory_gateway.py` -- `VoiceFactoryGateway:97`. `list_videos:186`, `get_video_speakers:195`, `get_clips:199` which calls `GET /videos/{id}/clips` and discards the `speaker_map` key the factory returns, `set_speaker_map:211` which is write-only with no reader, `stream_clip_audio:241`. Errors `VoiceFactoryError:72` and `VoiceFactoryTransientError:89`.
- `pythonapi/core/voice_pipeline_graph.py` -- `:263-264` assign `clip_count` and `approved_count` from `gateway.get_clips` at `:257`. The `if not clips:` guard at `:265-266` must survive.
- `pythonapi/core/voice_agent_tools.py` -- `list_voice_runs:92` returns `video_title` at `:110`. `get_voice_run:119` dumps the whole run at `:130`, and its docstring at `:122` promises clip counts.
- `apps/pythonapi/tests/` -- `test_voice.py` holds `FakeVoiceFactoryGateway:56` and fixtures `gateway:222` and `voice_client:255-260`, plus helpers `clip():194` and `make_run():206`. It breaks at `:503`, `:698`, `:702`, `:1089`. `test_voice_assign_commit.py` breaks at `:37`, `:109`, `:367`. `test_voices_train.py:66`. `test_agent_tools.py:307`. `conftest.py` is 31 lines and holds no gateway fixture.

### copilot_agent_network — front end (`apps/agentic-executor/`)

- `src/components/videos-view.tsx` -- 117 lines, the whole file is in scope. The run-to-video adapter sits at `:19-32`, including `videoId: run.videoId ?? run.id` at `:23` and `title: run.videoTitle ?? "Untitled video"` at `:24`. Cards at `:67-76` are keyed by `run.id` but index positionally into `videos[index]`. That coupling breaks the moment the lists come from two endpoints. `clipCount={run.clipCount}` at `:72`. Detail section `:80-114`, second title fallback `:84`, `<ClipTable runId={selectedRun.id} />` at `:112`.
- `src/components/studio-provider.tsx` -- `:103-112` is an exact duplicate of the videos-view adapter, with the same two fallbacks at `:105-106`. `videoForRun:147-150` breaks for runs with a null `videoId`. `clips:125-138` flattens the speaker board. Ten consumers, at `page.tsx:13,:48,:72` and seven components.
- `src/lib/voice_api.ts` -- no `useVideos()` exists. Hook table at `:188-432`. Query keys at `voiceQueryKeys:152-163`. A videos key must NOT nest under `["voice","runs"]`, or the invalidate in `useStartRun:267` will clear it. Base URLs at `:28-31`. `request<T>():111-138` converts snake case to camel at `:137`. `STREAM_KEEPS_THIS_FRESH:183-186` is `staleTime: Infinity`. Do not apply it to the videos query.
- `src/components/clip-table.tsx` -- the review screen. `ClipTable({runId}):15`, `useSpeakerBoard(runId):17`. It sets `videoId: runId` at `:102`, which is a deliberate lie that the video-keyed routes remove.
- `src/components/clip-row.tsx` -- `clipAudioUrl(clip.runId, clip.clipId):126` is run-scoped and must become video-scoped.
- `src/lib/derive.ts` -- LIVE, through `chat-panel.tsx:5` to `assistant.ts:5`. `runTitle():74` reads `run.videoTitle` and must be fixed, not deleted.
- `src/lib/types.ts` -- `VideoResult:40-47`, `VoiceRun:49-82` with the four fields at `:54,:58,:66,:67`, `SpeakerBoard:131-135`, `VoiceContribution:208-216`, `StudioClip:239-248`.
- `src/components/video-card.tsx` -- the `clipCount` prop at `:20,:26,:69`.
- Dead and safe to delete: `src/lib/store.ts`, imported only by `simulator.ts`; `src/lib/simulator.ts`, imported by nothing; `src/lib/types_live.ts`, imported by nothing. `guessTitle` lives at `store.ts:190-191`.
- `src/lib/voice_api.test.tsx` -- the only test file in the app. The fixture at `:103` carries `videoTitle`.

## Tasks & Acceptance

**Execution:**

Steps 1 and 2 land in `star-trek-voyicer`. Steps 3 to 7 land in `copilot_agent_network`. Both repos are already on branch `spec-factory-source-of-truth`.

- [x] `star-trek-voyicer: src/voice_factory/infrastructure/filesystem_layout.py` -- add `require_work_dir() -> Path` that raises `HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, ...)` naming the path when `WORK_DIR` is absent, and rewrite the `:15-20` comment -- this is the first 500 in the service and the fix that makes the 2026-08-20 outage loud
- [x] `star-trek-voyicer: src/voice_factory/routes/characters.py` -- replace the `:12-13` early return with `require_work_dir()` -- this is the true `WORK_DIR` guard
- [x] `star-trek-voyicer: src/voice_factory/routes/videos.py` -- call `require_work_dir()` in `get_videos`, then keep returning `{"videos": []}` when only `work/youtube/` is absent -- a fresh install is empty, not broken
- [x] `star-trek-voyicer: tests/routes/test_videos.py` -- add a missing-`WORK_DIR` test that sets the fixture to `tmp_path / "nope"`, and keep the `:185` empty-list assertion for the fresh-install case -- the two cases now differ and both need cover
- [x] `star-trek-voyicer: src/voice_factory/repositories/video_meta_repository.py` -- NEW. `META_FILENAME = "meta.json"`, a sync `read_video_meta(video_dir) -> dict` returning `{}` when absent, and `write_video_meta_file(video_dir, meta) -> Path` -- match `speaker_map_repository.py` exactly
- [x] `star-trek-voyicer: src/voice_factory/core/youtube_ingest.py` -- add `META_NAME` to the `:12-16` constants and capture title, url, duration, and channel at ingest instead of discarding them at `resolve_video_id:46` -- reuse the field shape at `youtube_search.py:49-59`
- [x] `star-trek-voyicer: src/voice_factory/core/clip_review.py` -- merge `read_video_meta()` into `video_summary():51`, so an absent file yields null fields and `video_id` as the title -- existing videos must keep working with no backfill
- [x] `star-trek-voyicer: tests/routes/test_videos.py` -- extend `build_video:24-31` to write a `meta.json`, cover both the present and absent cases, and update the `:167` key-set assertion -- the return shape changes
- [x] `apps/pythonapi/pythonapi/core/voice_factory_gateway.py` -- widen `get_clips:199` to return the `speaker_map` the factory already sends at its `routes/videos.py:78` -- this is the missing read path, because `set_speaker_map:211` is write-only
- [x] `apps/pythonapi/pythonapi/core/` -- extract the speaker grouping out of `routes/voice.py:248-283` into one function -- both the run-keyed and video-keyed paths must call it, not two copies
- [x] `apps/pythonapi/pythonapi/routes/voice.py` -- add `GET /videos/{video_id}/clips`, `PATCH /videos/{video_id}/clips`, and `GET /videos/{video_id}/clips/{clip_id}/audio` beside the run-keyed routes, and fill `assigned_character` from the factory's `speaker_map` rather than `run.speaker_map:270` -- keep both working until step 6 moves the front end
- [x] `apps/pythonapi/pythonapi/routes/voice.py` -- make `list_videos:119` and `get_video_speakers:134` return the factory payload unchanged, dropping the `response_model` re-modelling -- CAP-6, and the route stays so there is one origin
- [x] `apps/pythonapi/pythonapi/core/voice_pipeline_graph.py` -- delete the `:263-264` assignments and keep the `:265-266` empty-clips guard -- stop writing before you stop reading
- [x] `apps/pythonapi/pythonapi/routes/voice.py` -- delete the `:309-310` recount, drop `approved_count` from the `:312` response body, drop `run.speaker_map =` at `:344`, and resolve `video_title=` at `:419` from the factory -- the second half of stop-writing
- [x] `apps/pythonapi/pythonapi/repositories/voice_runs.py` -- remove all four columns from `_row_from_run:359`, `_run_from_row:389`, and `update_run:309` -- nine line sites, all listed in the Code Map
- [x] `apps/pythonapi/pythonapi/repositories/voice_contributions.py` -- stop reading `run_row.video_title:94` and resolve the title from the factory at read time -- the join keeps carrying `video_id` only
- [x] `apps/pythonapi/pythonapi/models/voice.py` -- remove the four fields from `VoiceRun:102` and reword the `voice_assignments` comment at `:120-123` that contrasts it with `speaker_map` -- the contrast no longer parses once the field is gone
- [x] `apps/pythonapi/pythonapi/models/voices.py` -- resolve `VoiceContribution.video_title:59` from the factory -- it is a projection of the dropped column
- [x] `apps/pythonapi/pythonapi/core/voice_agent_tools.py` -- point `:110` at the resolved title and correct the `get_voice_run` docstring at `:122` -- it promises clip counts that no longer exist
- [x] `apps/pythonapi/pythonapi/models/orm.py` -- drop the four columns from `VoiceRunRow:71` -- last, after every reader and writer is gone
- [x] `apps/pythonapi/` -- migrate per `data-model.md` by dropping and recreating `voice_runs` through SQLAlchemy `drop_all`, never raw SQL. HALT for human approval first -- `create_all` does not alter an existing table. Approved and run 2026-08-21 against the dev database inside the `pythonapi` container. `voice_contributions` has an unmigrated `run_id` FK into `voice_runs` that `data-model.md` did not flag; both tables were dropped and recreated together via SQLAlchemy's FK-ordered `drop_all`/`create_all` so `voice_contributions`' own schema was unaffected. One live row in each table was lost: run `f3a44782...` (phase `awaiting_review`, video `CTGlPCWlViw`) and its `voice_contributions` row (`SPEAKER_03` -> Voice `c08bfe46...`). Verified after: zero rows for the four dropped columns, `voice_assignments` still present.
- [x] `apps/pythonapi/tests/` -- update `test_voice.py:503,:698,:702,:1089`, `test_voice_assign_commit.py:37,:109,:367`, `test_voices_train.py:66`, and `test_agent_tools.py:307`, then add cover for the missing-`WORK_DIR` case, the video-keyed board matching the run-keyed one, a video with no run, an orphaned run, and `voice_assignments` surviving the migration -- Python tests only
- [x] `apps/agentic-executor/src/lib/voice_api.ts` -- add `useVideos()` against `GET /api/voice/videos`, with its own query key outside `["voice","runs"]` and without `STREAM_KEEPS_THIS_FRESH` -- `useStartRun:267` would otherwise clear it, and `staleTime: Infinity` would freeze it
- [x] `apps/agentic-executor/src/components/videos-view.tsx` -- list from `useVideos()`, delete the `:19-32` adapter and the `:23` id fallback, join runs onto videos by `video_id`, render an unmatched run as orphaned with no review action, and show an error state on a failed fetch -- CAP-2 and CAP-3, and it must never fall back to Postgres
- [x] `apps/agentic-executor/src/components/studio-provider.tsx` -- replace the duplicate adapter at `:103-112` with the same video source and fix `videoForRun:147-150` for a null `videoId` -- one adapter, not two
- [x] `apps/agentic-executor/src/components/clip-table.tsx` and `clip-row.tsx` -- address clips by `video_id`, remove the `videoId: runId` lie at `clip-table.tsx:102`, and make `clipAudioUrl` video-scoped at `clip-row.tsx:126` -- this points the review screen at the new routes
- [x] `apps/agentic-executor/src/lib/types.ts`, `video-card.tsx`, and `derive.ts` -- remove the four fields from `VoiceRun:49-82`, drop or re-source the `clipCount` prop at `video-card.tsx:20,:26,:69`, and fix `runTitle():74` -- `derive.ts` is LIVE through `chat-panel.tsx` and `assistant.ts`
- [x] `apps/agentic-executor/src/lib/` -- delete `store.ts`, `simulator.ts`, and `types_live.ts` -- `guessTitle` lives at `store.ts:190-191` and the whole cluster is unreachable from `page.tsx`. Do NOT delete `voice_event_stream.tsx` or `query-provider.tsx`
- [x] `apps/agentic-executor/src/lib/voice_api.test.tsx` -- drop `videoTitle` from the `:103` fixture -- keep it matching the API shape
- [x] `apps/pythonapi/pythonapi/routes/voice.py` -- delete the run-keyed `get_speaker_board:248`, `update_clips:287`, and `get_clip_audio:467`, along with their `run_id` to `video_id` translation -- last step, once the front end has moved
- [x] `apps/agentic-executor/src/lib/voice_api.ts` -- remove the run-keyed paths in `useSpeakerBoard` and `useUpdateClips` once the routes are gone -- no caller may outlive its route

**Acceptance Criteria:**
- Given `WORK_DIR` points at a path that does not exist, when a client calls `GET /videos`, then the factory answers 500 naming the path and `GET /health` still answers.
- Given `WORK_DIR` exists but holds no `youtube/` subdirectory, when a client calls `GET /videos`, then it answers 200 with an empty list.
- Given a `voice_runs` row whose `video_id` is absent from `GET /videos`, when the videos view loads, then that run renders as orphaned with no review action, and every other run still works.
- Given the voice factory is stopped, when the videos view loads, then it shows an error state and renders no video cards.
- Given a video that no `voice_runs` row references, when a client calls `GET /api/voice/videos/{video_id}/clips`, then it returns that video's speaker groups.
- Given a video V ingested under character A, when character B claims V, then both show the same title, and no `voice_runs` row supplies it.
- Given a new field added to the factory's `video_summary()`, when a client calls `GET /api/voice/videos`, then the field is visible with no change under `apps/pythonapi/`.
- Given the migration has run, when `voice_runs` is inspected, then none of `video_title`, `speaker_map`, `clip_count`, or `approved_count` exists, and `voice_assignments` is still populated.
- Given the full pipeline, when a run starts from a YouTube URL, then it reaches `AWAITING_REVIEW`, assignment and commit create one `voice_contributions` row per assigned speaker, and the SSE stream still carries a complete `VoiceRun` per event.

## Spec Change Log

## Design Notes

**The ownership rule decides every case.** The factory owns what it can recompute from `work/`. The orchestrator owns what would be lost if `work/` were deleted. Clip counts, keep decisions, and speaker labels survive a dropped database, so they belong to the factory. Phase, leases, retry history, and Voice assignments survive a deleted `work/`, so they belong to the orchestrator. The full column table is in `data-model.md`.

**`speaker_map` and `voice_assignments` are not duplicates.** `speaker_map` maps a speaker label to a character and lives in `speaker_map.json`. `voice_assignments` maps a speaker label to a Voice id and lives in Postgres. The factory has no Voice concept. Dropping the Postgres copy of `speaker_map` does not touch `voice_assignments`.

**The speaker-map read path already exists.** The gateway has no map reader, but the factory returns `speaker_map` in its `GET /videos/{id}/clips` payload at `routes/videos.py:78`. `get_clips` discards it today. Widening that one return value supplies `assigned_character` with no extra call.

**`video_title` is an empty column, not a stale mirror.** Nothing ever writes it. `voice_agent_tools.py:110` and `voice_contributions.py:94` already return `None` in production, and only tests populate it. Dropping it makes an existing dead path explicit.

**The videos view holds a hidden coupling.** `videos-view.tsx:67-76` maps over `runList` but indexes into `videos[index]`. That works only because both are the same list. Joining two endpoints by `video_id` is not an optimization here. It is what stops the view from pairing a run with the wrong video.

**Why `meta.json` and not `voice_runs.video_title`.** A video is ingested once and shared across every character that claims it. Store the title on the run and only the first claimant has it. Store it with the video and every claimant sees the same name. `data-model.md` records the alternative and why it was rejected.

## Verification

**Commands:**
- `uv run --no-project python .claude/skills/litert-subagent/scripts/run_ci_task.py --task test-pythonapi` -- expected: the four known Presidio spaCy PII failures and nothing else. A fifth failure is a real regression.
- `uv run --no-project python .claude/skills/litert-subagent/scripts/run_ci_task.py --task lint-pythonapi` -- expected: clean
- `uv run --no-project python .claude/skills/litert-subagent/scripts/run_ci_task.py --task lint-web` -- expected: clean
- `uv run --no-project python .claude/skills/litert-subagent/scripts/run_ci_task.py --task typecheck-web` -- expected: clean
- `just test-jeanlucrecord` -- run in `star-trek-voyicer`. No delegation task is defined, so a person runs this.

**Manual checks (if no CLI):**
- Read the column table in `data-model.md` against `models/orm.py:VoiceRunRow`. Every remaining column must appear in the table with an owner, and none may be marked factory.
- Search `apps/pythonapi/` for `speaker_map`, `clip_count`, `approved_count`, and `video_title`. Outside a migration, the only surviving `speaker_map` hits must be gateway calls or the approve request payload.
- Start the factory with `just serve-jeanlucrecord`, confirm the videos view lists the three ingested videos with real titles, then stop it and confirm an error state rather than cards.
- Rename a directory under `work/youtube/` while a `voice_runs` row still points at the old id. That run must render orphaned, and every other run must still work. Rename it back.
- Add a throwaway field to `video_summary()`, restart the factory, and confirm the field appears at `GET /api/voice/videos` with no edit under `apps/pythonapi/`. Remove it.
- Confirm the open question on `current_epoch` and `current_loss` is still recorded in `data-model.md`. It must not be silently dropped.
