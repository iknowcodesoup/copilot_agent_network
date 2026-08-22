"use client";

import { useEffect, useMemo, useState } from "react";
import { Film, Pencil } from "lucide-react";
import { useRenameVideo, useVideos, useVoiceRuns } from "@/lib/voice_api";
import type { VideoSummary, VoiceRun } from "@/lib/types";
import { VideoCard } from "./video-card";
import { ClipTable } from "./clip-table";
import { StatusPill } from "./status-pill";
import { useStudio } from "./studio-provider";

/*
 * Two lists, one join key.
 *
 * The videos come from the voice factory, which owns them, and the runs come
 * from Postgres, which owns the pipeline state. They are joined on videoId,
 * never by position: a run whose video the factory no longer lists is orphaned
 * and must say so, rather than quietly pairing with whichever video happened to
 * sit at the same index.
 */
interface VideoRow {
  video: VideoSummary;
  run: VoiceRun | null;
}

/* Click the title to correct it. The factory owns the name - it lives in
   meta.json beside the clips - so the rename is visible to every character
   that claims the same video, and nothing is stored on this side. */
function VideoTitle({ video }: { video: VideoSummary }) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(video.title);
  const renameVideo = useRenameVideo(video.videoId);

  useEffect(() => {
    if (!editing) setTitle(video.title);
  }, [video.title, editing]);

  const save = () => {
    setEditing(false);
    const next = title.trim();
    if (next && next !== video.title) renameVideo.mutate(next);
  };

  if (editing)
    return (
      <input
        autoFocus
        value={title}
        onChange={(event) => setTitle(event.target.value)}
        onBlur={save}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.nativeEvent.isComposing) {
            event.preventDefault();
            save();
          }
          if (event.key === "Escape") {
            setTitle(video.title);
            setEditing(false);
          }
        }}
        className="min-w-0 flex-1 rounded-md border border-input bg-background px-2 py-1 text-sm font-semibold outline-none"
      />
    );

  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      className="group flex min-w-0 items-center gap-1.5 rounded-md px-1 py-0.5 text-left hover:bg-muted/40"
    >
      <h3 className="truncate text-sm font-semibold text-foreground">
        {video.title}
      </h3>
      <Pencil className="size-3 shrink-0 text-muted-foreground/0 group-hover:text-muted-foreground" />
    </button>
  );
}

function toneForPhase(phase: VoiceRun["phase"]) {
  if (phase === "failed") return "failed" as const;
  if (phase === "ready") return "complete" as const;
  if (phase === "awaiting_review") return "queued" as const;
  return "in-progress" as const;
}

export function VideosView() {
  const videos = useVideos();
  const runs = useVoiceRuns();
  const { selectedVideoId, setSelectedVideoId } = useStudio();
  const videoList = useMemo(() => videos.data ?? [], [videos.data]);
  const runList = useMemo(() => runs.data ?? [], [runs.data]);

  const rows = useMemo<VideoRow[]>(
    () =>
      videoList.map((video) => ({
        video,
        run: runList.find((run) => run.videoId === video.videoId) ?? null,
      })),
    [videoList, runList],
  );

  /* A run pointing at a video the factory does not list. Its clips are gone,
     so it gets no review action - only enough to see it and delete it. */
  const orphanedRuns = useMemo(
    () =>
      runList.filter(
        (run) =>
          !run.videoId ||
          !videoList.some((video) => video.videoId === run.videoId),
      ),
    [runList, videoList],
  );

  const selectedRow =
    rows.find((row) => row.video.videoId === selectedVideoId) ??
    rows[0] ??
    null;

  if (videos.isLoading)
    return (
      <div className="p-10 text-center text-sm text-muted-foreground">
        Loading ingested videos…
      </div>
    );
  /* No fallback to the run list here on purpose. A stale copy of the videos is
     exactly what hid the outage this view is meant to make visible. */
  if (videos.isError)
    return (
      <div className="rounded-xl border border-destructive/30 p-10 text-center text-sm text-destructive">
        Unable to reach the voice factory, so no videos can be listed. Start it
        with <span className="font-mono">just serve-jeanlucrecord</span>.
      </div>
    );

  return (
    <div className="flex flex-col gap-5">
      <section>
        <div className="mb-3 flex items-center gap-2">
          <Film className="size-4 text-primary" />
          <h2 className="text-sm font-semibold text-foreground">
            Processing Queue
          </h2>
          <span className="font-mono text-xs text-muted-foreground">
            {videoList.length} videos
          </span>
        </div>
        {videoList.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border p-10 text-center">
            <p className="text-sm text-muted-foreground">
              No videos yet. Paste a YouTube URL above to start processing.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {rows.map(({ video, run }) => (
              <VideoCard
                key={video.videoId}
                video={video}
                phase={run?.phase ?? null}
                selected={
                  video.videoId === selectedRow?.video.videoId
                }
                onSelect={() => setSelectedVideoId(video.videoId)}
              />
            ))}
          </div>
        )}
      </section>

      {orphanedRuns.length > 0 && (
        <section className="rounded-xl border border-dashed border-border p-4">
          <h3 className="mb-2 text-sm font-semibold text-foreground">
            Runs without a video
          </h3>
          <p className="mb-3 text-xs text-muted-foreground">
            The voice factory no longer holds the video these runs point at, so
            there is nothing to review.
          </p>
          <ul className="flex flex-col gap-2">
            {orphanedRuns.map((run) => (
              <li
                key={run.id}
                className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground"
              >
                <span className="font-medium text-foreground">
                  {run.primaryCharacter}
                </span>
                <StatusPill
                  tone={toneForPhase(run.phase)}
                  pulse={false}
                  label={run.phase.replaceAll("_", " ")}
                />
                <span className="truncate font-mono text-[0.7rem]">
                  {run.videoId ?? "no video id"}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {selectedRow && (
        <section className="rounded-xl border border-border bg-card/50 p-4">
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <VideoTitle video={selectedRow.video} />
            {selectedRow.run && (
              <StatusPill
                tone={toneForPhase(selectedRow.run.phase)}
                pulse={
                  !["failed", "ready", "awaiting_review"].includes(
                    selectedRow.run.phase,
                  )
                }
                label={selectedRow.run.phase.replaceAll("_", " ")}
              />
            )}
            {selectedRow.video.url && (
              <a
                href={selectedRow.video.url}
                target="_blank"
                rel="noreferrer"
                className="ml-auto truncate font-mono text-[0.7rem] text-muted-foreground hover:text-primary"
              >
                {selectedRow.video.url}
              </a>
            )}
          </div>
          <ClipTable
            videoId={selectedRow.video.videoId}
            runId={selectedRow.run?.id ?? null}
          />
        </section>
      )}
    </div>
  );
}
