"use client";

import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Subscription } from "rxjs";
import { voiceQueryKeys, type JobLog, type VoiceRun } from "./voice_api";
import { runLogs$, runSnapshots$, runUpdates$ } from "./voice_streams";

/*
 * Where the stream meets the cache.
 *
 * voice_streams.ts owns the connection and hands out one observable per event
 * kind. This file is the only subscriber that writes: it puts what arrives into
 * the TanStack Query cache under the keys the ordinary hooks already read, so
 * every voice component keeps its existing useVoiceRun/useVoiceRuns call and
 * gets live data without changing a line.
 *
 * Nothing here counts events or tracks a cursor. EventSource sends back the
 * last `id:` it saw as Last-Event-ID and the server treats that as a replay
 * position, so reconnect handling lives in the transport, not in this file.
 */

/*
 * Drop any fetch still in the air for this key before writing to it.
 *
 * The REST hooks in voice_api.ts and this stream write the same cache entries.
 * A push always carries newer state than a read that started earlier, so a
 * fetch that resolves afterwards would put the older answer back. Cancelling is
 * TanStack's own answer to that race, and it is safe to fire and forget: with
 * nothing in flight it does nothing at all.
 */
function cancelFetchesFor(
  queryClient: QueryClient,
  queryKey: readonly unknown[],
): void {
  void queryClient.cancelQueries({ queryKey, exact: true });
}

/*
 * Replace the run, or add it when it is new. Written as a replace rather than a
 * merge on purpose: every event carries the complete run, so applying the same
 * one twice lands on the same result. A duplicate after a reconnect is then
 * simply not a problem worth guarding against.
 */
function applyRunUpdate(queryClient: QueryClient, run: VoiceRun): void {
  cancelFetchesFor(queryClient, voiceQueryKeys.run(run.id));
  cancelFetchesFor(queryClient, voiceQueryKeys.runs);
  queryClient.setQueryData(voiceQueryKeys.run(run.id), run);
  queryClient.setQueryData<VoiceRun[]>(voiceQueryKeys.runs, (runs) => {
    if (!runs) return [run];
    const index = runs.findIndex((existing) => existing.id === run.id);
    if (index === -1) return [run, ...runs];
    const next = runs.slice();
    next[index] = run;
    return next;
  });
}

/*
 * Append one pushed log chunk to the cache useJobLog reads. Guards against a
 * replayed chunk after a reconnect the same way applyRunUpdate does not need
 * to: a log is appended, not replaced, so a duplicate would double the text
 * without this check.
 */
function applyLogChunk(
  queryClient: QueryClient,
  chunk: { runId: string; offset: number; content: string },
): void {
  const key = voiceQueryKeys.log(chunk.runId);
  cancelFetchesFor(queryClient, key);
  queryClient.setQueryData<JobLog>(key, (prev) => {
    if (prev && chunk.offset <= prev.offset) return prev;
    return {
      offset: chunk.offset,
      content: (prev?.content ?? "") + chunk.content,
      state: prev?.state ?? "running",
    };
  });
}

function applySnapshot(queryClient: QueryClient, runs: VoiceRun[]): void {
  cancelFetchesFor(queryClient, voiceQueryKeys.runs);
  queryClient.setQueryData(voiceQueryKeys.runs, runs);
  for (const run of runs) {
    cancelFetchesFor(queryClient, voiceQueryKeys.run(run.id));
    queryClient.setQueryData(voiceQueryKeys.run(run.id), run);
  }
}

/*
 * One subscription per event kind, all torn down together.
 *
 * Job progress only. A create or an update carries its new state back in its
 * own response, so it needs nothing from here.
 */
function useVoiceEventStream(): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    const subscription = new Subscription();

    subscription.add(
      runSnapshots$.subscribe((runs) =>
        applySnapshot(queryClient, runs as VoiceRun[]),
      ),
    );
    subscription.add(
      runUpdates$.subscribe((run) =>
        applyRunUpdate(queryClient, run as VoiceRun),
      ),
    );
    subscription.add(
      runLogs$.subscribe((log) =>
        applyLogChunk(
          queryClient,
          log as { runId: string; offset: number; content: string },
        ),
      ),
    );
    return () => subscription.unsubscribe();
  }, [queryClient]);
}

/*
 * Renders nothing. Mounted once inside QueryProvider in page.tsx, so one
 * connection serves the whole dashboard.
 */
export function VoiceLiveState() {
  useVoiceEventStream();
  return null;
}
