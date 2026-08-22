"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from "@tanstack/react-query";
import {
  ClipDecision,
  ClipSummary,
  CommitStageLabels,
  IngestStageLabels,
  JobLog,
  PhaseLabels,
  RunAssignResponse,
  SpeakerBoard,
  TrainingProgress,
  VideoResult,
  VideoSummary,
  VoiceDetail,
  VoicePhase,
  VoiceRun,
  VoiceRunPhase,
  VoiceSummary,
} from "./types";

export * from "./types";
export * from "./voice_endpoints";

import { voiceApiBase, voiceFactoryBase, voicesApiBase } from "./voice_endpoints";

/* Phases where the pipeline is doing something on its own. */
const activePhases: ReadonlySet<VoiceRunPhase> = new Set([
  "downloading",
  "diarizing",
  "committing",
  "training",
  "exporting",
]);

export function isActive(phase: VoiceRunPhase): boolean {
  return activePhases.has(phase);
}

/* Where a retry on this run would resume. Falls back to the phase's own label
   when the phase has no sub-steps to name. */
export function resumeStepLabel(run: VoiceRun): string {
  const resumePhase = run.failedFromPhase ?? "downloading";
  if (resumePhase === "downloading") {
    return IngestStageLabels[run.ingestStageIndex] ?? PhaseLabels.downloading;
  }
  if (resumePhase === "committing") {
    return CommitStageLabels[run.commitStageIndex] ?? PhaseLabels.committing;
  }
  return PhaseLabels[resumePhase];
}

/*
 * FastAPI speaks snake_case and this app speaks camelCase. Converting at the
 * boundary keeps every component in one convention, so no component has to
 * remember which side of the wire a field came from.
 */
function toCamelCase(value: string): string {
  return value.replace(/_([a-z0-9])/g, (_, character) =>
    character.toUpperCase(),
  );
}

function toSnakeCase(value: string): string {
  return value.replace(/[A-Z]/g, (character) => `_${character.toLowerCase()}`);
}

function convertKeys(
  value: unknown,
  convert: (key: string) => string,
): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => convertKeys(item, convert));
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, entry]) => [
        convert(key),
        convertKeys(entry, convert),
      ]),
    );
  }
  return value;
}

export class VoiceApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "VoiceApiError";
  }
}

export async function request<T>(
  path: string,
  init?: RequestInit,
  base: string = voiceApiBase,
): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: init?.body
      ? { "Content-Type": "application/json", ...init?.headers }
      : init?.headers,
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ?? detail;
    } catch {
      // a non-JSON error body is still worth reporting by status alone
    }
    throw new VoiceApiError(detail, response.status);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return convertKeys(await response.json(), toCamelCase) as T;
}

export function jsonBody(payload: unknown): string {
  return JSON.stringify(convertKeys(payload, toSnakeCase));
}

/*
 * speaker_map keys are speaker labels like SPEAKER_00, not field names. Running
 * them through the case converter would rewrite them, so they stay untouched.
 */
function speakerMapBody(speakerMap: Record<string, string | null>): string {
  return JSON.stringify({ speaker_map: speakerMap });
}

export const voiceQueryKeys = {
  runs: ["voice", "runs"] as const,
  run: (runId: string) => ["voice", "runs", runId] as const,
  /* Deliberately outside ["voice","runs"]: the videos list is the factory's
     answer, not a run's, and useStartRun invalidates the whole run subtree. */
  videos: ["voice", "videos"] as const,
  speakers: (videoId: string) =>
    ["voice", "videos", videoId, "clips"] as const,
  training: (runId: string) => ["voice", "runs", runId, "training"] as const,
  log: (runId: string) => ["voice", "runs", runId, "log"] as const,
  search: (query: string) => ["voice", "search", query] as const,
  characters: ["voice", "characters"] as const,
  voices: (query: string) => ["voice", "voices", query] as const,
  voiceList: ["voice", "voiceList"] as const,
  voiceDetail: (voiceId: string) => ["voice", "voiceDetail", voiceId] as const,
};

/* Keyed on the video, so playing a clip never depends on a run lookup. */
export function clipAudioUrl(videoId: string, clipId: string): string {
  return `${voiceFactoryBase}/videos/${videoId}/clips/${clipId}/audio`;
}

/*
 * None of the run hooks below poll. The server pushes every state change over
 * the voice event stream, which writes straight into this cache - see
 * voice_event_stream.tsx. The initial fetch here is what fills the cache before
 * the stream connects, and the fallback if it never does.
 */
/*
 * The stream owns these two entries once it is connected, so they never go
 * stale on their own. Without that, the default staleTime of 0 refetches on
 * every remount and every window focus, and each of those reads can land after
 * a newer pushed event and put older state back on the screen. A mutation's
 * invalidateQueries still forces a refetch, which is the one time a read here
 * knows something the stream has not sent yet.
 */
const STREAM_KEEPS_THIS_FRESH = {
  staleTime: Infinity,
  refetchOnWindowFocus: false,
} as const;

export function useVoiceRuns() {
  return useQuery({
    queryKey: voiceQueryKeys.runs,
    queryFn: () => request<VoiceRun[]>("/runs"),
    ...STREAM_KEEPS_THIS_FRESH,
  });
}

export function useVoiceRun(
  runId: string,
  options?: Partial<UseQueryOptions<VoiceRun, VoiceApiError>>,
) {
  return useQuery<VoiceRun, VoiceApiError>({
    queryKey: voiceQueryKeys.run(runId),
    queryFn: () => request<VoiceRun>(`/runs/${runId}`),
    ...STREAM_KEEPS_THIS_FRESH,
    ...options,
  });
}

/* Every ingested video the factory holds.

   No STREAM_KEEPS_THIS_FRESH here on purpose: the event stream carries runs,
   never videos, so staleTime: Infinity would freeze this list until a reload.
   The factory is the only source - a failure surfaces as an error rather than
   falling back to anything stored here. */
export function useVideos() {
  return useQuery({
    queryKey: voiceQueryKeys.videos,
    /* The factory answers {videos: [...]}. The removed Python route used to
       unwrap it; the proxy forwards it verbatim, so unwrap it here. */
    queryFn: async () =>
      (
        await request<{ videos: VideoSummary[] }>(
          "/videos",
          undefined,
          voiceFactoryBase,
        )
      ).videos,
  });
}

/* Clips grouped by speaker, keyed on the video. A video with no run has a
   board too, which is what lets a second character review one. */
export function useSpeakerBoard(videoId: string, enabled: boolean) {
  return useQuery({
    queryKey: voiceQueryKeys.speakers(videoId),
    queryFn: () => request<SpeakerBoard>(`/videos/${videoId}/clips`),
    enabled,
  });
}

export function useTrainingProgress(runId: string, enabled: boolean) {
  return useQuery({
    queryKey: voiceQueryKeys.training(runId),
    queryFn: () => request<TrainingProgress>(`/runs/${runId}/training`),
    enabled,
  });
}

export function useJobLog(runId: string, enabled: boolean) {
  return useQuery({
    queryKey: voiceQueryKeys.log(runId),
    queryFn: () => request<JobLog>(`/runs/${runId}/logs`),
    enabled,
  });
}

export function useVideoSearch(query: string) {
  return useQuery({
    queryKey: voiceQueryKeys.search(query),
    queryFn: () =>
      request<{ query: string; videos: VideoResult[] }>(
        `/search?query=${encodeURIComponent(query)}&limit=12`,
        undefined,
        voiceFactoryBase,
      ),
    enabled: query.trim().length > 0,
    // a search costs a real yt-dlp call, so keep results around
    staleTime: 5 * 60_000,
  });
}

export function useCharacters() {
  return useQuery({
    queryKey: voiceQueryKeys.characters,
    /* {characters: [...]} on the wire; the removed route unwrapped it. */
    queryFn: async () =>
      (
        await request<{ characters: string[] }>(
          "/characters",
          undefined,
          voiceFactoryBase,
        )
      ).characters,
    staleTime: 60_000,
  });
}

export function useStartRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: {
      primaryCharacter: string;
      sourceUrl: string;
      diarize: boolean;
      numSpeakers?: number | null;
    }) =>
      request<{ id: string; phase: VoiceRunPhase }>("/runs", {
        method: "POST",
        body: jsonBody(payload),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: voiceQueryKeys.runs });
    },
  });
}

/* Write review decisions straight through to the factory's review.csv, which
   stays the one source of truth for them. Nothing is counted back into a run:
   the counts are the factory's, so the videos list is refreshed instead. */
export function useUpdateClips(videoId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (decisions: ClipDecision[]) =>
      request<{ videoId: string; updated: number; clips: ClipSummary[] }>(
        `/videos/${videoId}/clips`,
        { method: "PATCH", body: jsonBody({ decisions }) },
        voiceFactoryBase,
      ),
    /* The response carries the clips as they now stand, so write them in
       rather than asking for them again. */
    onSuccess: ({ clips }) => {
      const edited = new Map(clips.map((clip) => [clip.clipId, clip]));
      queryClient.setQueryData<SpeakerBoard>(
        voiceQueryKeys.speakers(videoId),
        (board) =>
          board && {
            ...board,
            speakers: board.speakers.map((speaker) => ({
              ...speaker,
              clips: speaker.clips.map(
                (clip) => edited.get(clip.clipId) ?? clip,
              ),
            })),
          },
      );
      /* Keeping or excluding a clip moves the factory's own counts, and only
         it can recompute them. */
      queryClient.invalidateQueries({ queryKey: voiceQueryKeys.videos });
    },
  });
}

/* Rename a video. The title lives in the factory's meta.json beside the clips,
   so every character that claims the video reads the same name. Nothing is
   stored here - the videos list is refetched instead. */
export function useRenameVideo(videoId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (title: string) =>
      request<VideoSummary>(
        `/videos/${videoId}`,
        { method: "PATCH", body: jsonBody({ title }) },
        voiceFactoryBase,
      ),
    /* PATCH answers with the renamed video, so the list takes it as given. */
    onSuccess: (video) =>
      queryClient.setQueryData<VideoSummary[]>(
        voiceQueryKeys.videos,
        (videos) =>
          videos?.map((existing) =>
            existing.videoId === video.videoId ? video : existing,
          ),
      ),
  });
}

export function useApproveRun(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (speakerMap: Record<string, string | null>) =>
      request<VoiceRun>(`/runs/${runId}/approve`, {
        method: "POST",
        body: speakerMapBody(speakerMap),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: voiceQueryKeys.run(runId) });
      queryClient.invalidateQueries({ queryKey: voiceQueryKeys.runs });
    },
  });
}

export function useDeleteRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) =>
      request<void>(`/runs/${runId}`, { method: "DELETE" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: voiceQueryKeys.runs });
    },
  });
}

/* Put a failed run back in the phase it fell over in. */
export function useRetryRun(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () =>
      request<VoiceRun>(`/runs/${runId}/retry`, { method: "POST" }),
    onSuccess: (run) => {
      queryClient.setQueryData(voiceQueryKeys.run(runId), run);
      queryClient.invalidateQueries({ queryKey: voiceQueryKeys.runs });
    },
  });
}

/* Search voices by name, for the assign-speaker combobox (Story 3.5). An
   empty query lists every voice, so a fresh combobox shows something rather
   than nothing. enabled defaults to true; the combobox passes false while
   it is closed, so it costs no request until the operator opens it. */
export function useVoices(query: string, enabled = true) {
  return useQuery({
    queryKey: voiceQueryKeys.voices(query),
    queryFn: () =>
      request<VoiceSummary[]>(
        `?query=${encodeURIComponent(query)}&limit=20`,
        undefined,
        voicesApiBase,
      ),
    enabled,
    staleTime: 10_000,
  });
}

/* Create a voice by name, for the combobox's inline-create path. Names are
   unique (FR22), so the caller handles a 409 by treating it as a match
   rather than an error. */
export function useCreateVoice() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) =>
      request<{ id: string; phase: VoicePhase }>(
        "",
        { method: "POST", body: jsonBody({ name }) },
        voicesApiBase,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["voice", "voices"] });
      // voiceList (Story 3.6's Voices dashboard) is a separate cache key
      // with no shared prefix - without this, a voice created here stays
      // invisible on that view until something else refetches it.
      queryClient.invalidateQueries({ queryKey: voiceQueryKeys.voiceList });
    },
  });
}

/* Map a run's speaker labels to Voices. Only assignment: it writes the
   voice_contributions rows and nothing else. It does not commit the run or
   start training - see useCommitRun and useTrainVoice for those, called
   separately so relabeling a clip's speaker never has a side effect beyond
   recording it. */
export function useAssignRun(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (assignments: Record<string, string | null>) =>
      request<RunAssignResponse>(`/runs/${runId}/assign`, {
        method: "POST",
        body: JSON.stringify({ assignments }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: voiceQueryKeys.run(runId) });
      queryClient.invalidateQueries({ queryKey: voiceQueryKeys.runs });
      queryClient.invalidateQueries({ queryKey: voiceQueryKeys.voiceList });
    },
  });
}

/* End review once every speaker the operator cares about is assigned.
   Separate from useAssignRun on purpose (Story 3.2's flattened assign+commit
   is now unflattened): assigning a speaker must not finish the run by
   itself. This is the one call that does, and it does only that - no voice
   phase change, no training. */
export function useCommitRun(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () =>
      request<VoiceRun>(`/runs/${runId}/commit`, { method: "POST" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: voiceQueryKeys.run(runId) });
      queryClient.invalidateQueries({ queryKey: voiceQueryKeys.runs });
    },
  });
}

/* Every voice, for the Voices view's card grid (Story 3.6). limit=50 is the
   route's max, and an empty query matches everything, same contract
   useVoices already relies on for the assign-speaker combobox. */
export function useVoiceList() {
  return useQuery({
    queryKey: voiceQueryKeys.voiceList,
    queryFn: () =>
      request<VoiceSummary[]>("?query=&limit=50", undefined, voicesApiBase),
  });
}

/* One voice's full detail, including its contribution audit trail - the
   single fetch voice_card.tsx's popover and view-clips modal both read
   from. */
export function useVoiceDetail(voiceId: string) {
  return useQuery({
    queryKey: voiceQueryKeys.voiceDetail(voiceId),
    queryFn: () =>
      request<VoiceDetail>(`/${voiceId}`, undefined, voicesApiBase),
  });
}

/* Start or restart training, whatever the voice's current phase (Story 3.3's
   train_voice: always accepted). The card refetches both this voice's
   detail and the list afterward so the phase shows without a page
   reload. */
export function useTrainVoice(voiceId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () =>
      request<{ id: string; phase: VoicePhase }>(
        `/${voiceId}/train`,
        { method: "POST" },
        voicesApiBase,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: voiceQueryKeys.voiceDetail(voiceId),
      });
      queryClient.invalidateQueries({ queryKey: voiceQueryKeys.voiceList });
    },
  });
}
