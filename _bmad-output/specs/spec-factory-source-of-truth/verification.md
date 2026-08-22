# Verification

## Capability checks

Each one is a manual sequence a person can run against a live stack. Ports and
commands follow `CLAUDE.md`.

### CAP-1 — one writer per fact

Read `data-model.md`'s column table against `models/orm.py:VoiceRunRow`. Every
column present must appear in the table with an owner. No column may be marked
**factory**.

Then search `apps/pythonapi/` for `speaker_map`, `clip_count`, `approved_count`,
and `video_title`. Outside a migration file, the only surviving `speaker_map`
matches must be gateway calls to the factory, never a `voice_runs` field.

### CAP-2 — a blind factory shows an error

```powershell
just serve-jeanlucrecord    # in star-trek-voyicer
# confirm the videos view lists three videos
# stop the factory
```

The videos view must show an error state. It must not render cards.

Repeat with the factory running but `WORK_DIR` pointed somewhere empty. Before
step 1 this returns 200 and an empty list. After, it returns 500 naming the path.
That difference is the whole point of step 1.

### CAP-3 — an orphan is visible

Rename one directory under `work/youtube/` while a `voice_runs` row still points
at the old id. Reload the videos view.

That run must render as orphaned, with no review action. Every other run must
still work. Rename it back.

### CAP-4 — clips are addressable without a run

Pick a `video_id` present in `GET /api/voice/videos` that no `voice_runs` row
references. `GET /api/voice/videos/{video_id}/clips` must return its speaker
groups.

Then confirm the run-keyed and video-keyed boards agree for a video that does
have a run.

### CAP-5 — a reused video keeps its title

Ingest a video under character A. Confirm the title in the videos view. Claim the
same video under character B.

Both must show the same title. Neither may read it from a `voice_runs` row —
confirm by checking that `voice_runs` has no `video_title` column.

### CAP-6 — the proxy adds nothing

Add a throwaway field to `video_summary()` in `core/clip_review.py`. Restart the
factory. Confirm the field appears at `GET /api/voice/videos` with no edit in
`apps/pythonapi/`. Remove it.

## Regression checks

The pipeline this spec does not touch must keep working end to end.

- Start a run from a YouTube URL. It must reach `AWAITING_REVIEW`.
- Assign speakers to voices and commit. `voice_assignments` must survive, and a
  `voice_contributions` row must be created per assigned speaker.
- Confirm `voice_runs.phase` still drives the reconciler, and that
  `VoiceRunReconciler` is still its only writer.
- Confirm the lease columns still serialise two API instances.
- Confirm `POST /api/voice/runs/{id}/retry` still restores `failed_from_phase`.
- Confirm the SSE stream at `GET /api/voice/events` still carries a complete
  `VoiceRun` per event.

## Test suites

```powershell
# copilot_agent_network — via litert-subagent, per Critical Rule 6
uv run --no-project python .claude/skills/litert-subagent/scripts/run_ci_task.py --task test-pythonapi
uv run --no-project python .claude/skills/litert-subagent/scripts/run_ci_task.py --task lint-pythonapi

# star-trek-voyicer — no delegation task defined; run directly
just test-jeanlucrecord
```

**Known baseline.** Four PII tests fail in `apps/pythonapi/tests/` because
Presidio cannot download its spaCy model in this environment. Those four are
expected and unrelated to this spec. A fifth failure is a real regression.

## Migration check

After the drop-and-recreate in `data-model.md`:

```sql
-- expect zero rows
SELECT column_name FROM information_schema.columns
WHERE table_name = 'voice_runs'
  AND column_name IN ('video_title','speaker_map','clip_count','approved_count');
```

Then confirm `voice_assignments` is still present and populated for any run that
had assignments before the migration. That column is the one thing in this change
that cannot be recomputed from the factory.

## Definition of done

- Every capability check above passes.
- Every regression check above passes.
- `test-pythonapi` shows the four known PII failures and nothing else.
- `just test-jeanlucrecord` passes.
- No Playwright, e2e, or UI test was added for `agentic-executor`.
- `data-model.md`'s open question on `current_epoch` and `current_loss` is either
  resolved in a follow-up spec or still recorded as open. It must not be silently
  dropped.
