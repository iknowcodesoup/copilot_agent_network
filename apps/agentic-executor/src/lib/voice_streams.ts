"use client";

import { Observable, defer, filter, map, retry, share, timer } from "rxjs";
import { voiceApiBase } from "./voice_endpoints";

/*
 * The observable layer.
 *
 * One EventSource becomes one shared Observable, and every consumer subscribes
 * to it rather than opening a connection or holding a copy of the state. The
 * TanStack Query cache stays the cache - see voice_event_stream.tsx, which
 * subscribes here and writes what arrives into it. This file owns the stream;
 * it never owns state.
 *
 * Why RxJS rather than a hand-rolled EventSource handler: the things that were
 * previously either missing or written by hand - one connection shared by many
 * consumers, retry with backoff, filtering one event kind out of many, and
 * unsubscribing cleanly - are each one operator here. The old handler had a
 * single onmessage that fanned out with if-statements and could not be
 * subscribed to twice.
 */

/* AG-UI event envelopes, as the encoder writes them on the wire. */
const SNAPSHOT_EVENT_TYPE = "STATE_SNAPSHOT";
const CUSTOM_EVENT_TYPE = "CUSTOM";
const RUN_UPDATED_EVENT_NAME = "voice.run.updated";
const RUN_LOG_EVENT_NAME = "voice.run.log";

/* Reconnect backoff. EventSource retries on its own, but only for a transport
   drop; an error the browser considers terminal closes the stream for good. */
const RETRY_BASE_MILLISECONDS = 1_000;
const RETRY_MAX_MILLISECONDS = 30_000;

export interface AgentUiEvent {
  type: string;
  name?: string;
  value?: unknown;
  snapshot?: { runs?: unknown[] };
}

/*
 * FastAPI speaks snake_case and this app speaks camelCase, the same conversion
 * every response in voice_api.ts gets. The stream carries the same shapes, so
 * it needs the same conversion.
 */
function toCamelCase(value: string): string {
  return value.replace(/_([a-z0-9])/g, (_, character: string) =>
    character.toUpperCase(),
  );
}

export function convertKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((item) => convertKeys(item));
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, entry]) => [
        toCamelCase(key),
        convertKeys(entry),
      ]),
    );
  }
  return value;
}

/*
 * One SSE connection as an Observable.
 *
 * defer() so nothing connects until someone subscribes, and share() so the
 * second subscriber joins the first one's connection instead of opening a
 * second. refCount closes the socket when the last subscriber leaves, which is
 * what makes unmounting the dashboard actually hang up.
 *
 * The error path is deliberate: EventSource reports a drop through onerror
 * without ending the stream, and reconnects itself. Only a socket it has given
 * up on (readyState CLOSED) is worth raising, because that is the one case
 * retry() below must handle.
 */
function serverSentEvents(url: string): Observable<MessageEvent> {
  return defer(
    () =>
      new Observable<MessageEvent>((subscriber) => {
        const source = new EventSource(url);
        source.onmessage = (event) => subscriber.next(event);
        source.onerror = () => {
          if (source.readyState === EventSource.CLOSED) {
            subscriber.error(new Error("Voice event stream closed"));
          }
          // otherwise EventSource is already reconnecting on its own
        };
        return () => source.close();
      }),
  ).pipe(
    retry({
      delay: (_error, retryCount) =>
        timer(
          Math.min(
            RETRY_BASE_MILLISECONDS * 2 ** (retryCount - 1),
            RETRY_MAX_MILLISECONDS,
          ),
        ),
    }),
    share({ resetOnRefCountZero: true }),
  );
}

/*
 * Frames the server pushed, parsed. An unreadable frame is dropped rather than
 * taking the connection down with it, which is why this maps to null and
 * filters instead of throwing.
 */
export const voiceEvents$: Observable<AgentUiEvent> = serverSentEvents(
  `${voiceApiBase}/events`,
).pipe(
  map((event) => {
    try {
      return JSON.parse(event.data) as AgentUiEvent;
    } catch {
      return null;
    }
  }),
  filter((event): event is AgentUiEvent => event !== null),
  share(),
);

/* One event kind, already converted. The three below are what the server
   publishes today; a new kind is a new export here and nothing else. */
export const runSnapshots$ = voiceEvents$.pipe(
  map((event) =>
    event.type === SNAPSHOT_EVENT_TYPE ? event.snapshot?.runs : undefined,
  ),
  filter((runs): runs is unknown[] => runs !== undefined),
  map((runs) => convertKeys(runs)),
);

export const runUpdates$ = voiceEvents$.pipe(
  filter(
    (event) =>
      event.type === CUSTOM_EVENT_TYPE && event.name === RUN_UPDATED_EVENT_NAME,
  ),
  map((event) => convertKeys(event.value)),
);

export const runLogs$ = voiceEvents$.pipe(
  filter(
    (event) =>
      event.type === CUSTOM_EVENT_TYPE && event.name === RUN_LOG_EVENT_NAME,
  ),
  map((event) => convertKeys(event.value)),
);

/*
 * Nothing else belongs here.
 *
 * This layer carries what the server pushes: job progress. Logs and run phase
 * changes come from job_runner, over the webhook and the Redis stream, because
 * a training run takes days and nobody can hold a request open for it.
 *
 * A create or an update is not that. It is a request with a response, and the
 * response carries the new state - so the caller writes what it got back into
 * the cache and is done. Routing a PATCH through an event stream would add a
 * second path to the same fact. See voice_api.ts.
 */
