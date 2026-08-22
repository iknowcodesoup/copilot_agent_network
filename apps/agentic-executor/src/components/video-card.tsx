"use client";

import { Film, Scissors } from "lucide-react";
import { StatusPill } from "./status-pill";
import { cn } from "@/lib/utils";
import type { VideoSummary, VoiceRunPhase } from "@/lib/types";

function toneForPhase(
  phase: VoiceRunPhase,
): "in-progress" | "complete" | "failed" | "queued" {
  if (phase === "failed") return "failed";
  if (phase === "ready") return "complete";
  if (phase === "awaiting_review") return "queued";
  return "in-progress";
}

/* The video describes itself, counts included: they come from the factory,
   which recomputes them from review.csv. phase is null for a video no run has
   claimed - ingested for one character, and offered to the next. */
export function VideoCard({
  video,
  phase,
  selected,
  onSelect,
}: {
  video: VideoSummary;
  phase: VoiceRunPhase | null;
  selected: boolean;
  onSelect: () => void;
}) {
  const tone = phase ? toneForPhase(phase) : null;
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "group flex flex-col overflow-hidden rounded-xl border bg-card text-left transition-all",
        selected
          ? "border-primary/60 ring-1 ring-primary/40"
          : "border-border hover:border-primary/30",
      )}
    >
      <div className="relative flex h-20 items-end overflow-hidden bg-muted/30 px-3 pb-3 pt-6">
        <Film className="absolute left-3 top-2 size-3.5 text-muted-foreground/60" />
        <div className="absolute inset-0 bg-background/35" />
      </div>
      <div className="flex flex-1 flex-col gap-2 p-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h3 className="truncate text-sm font-medium text-foreground">
              {video.title}
            </h3>
            <p className="truncate font-mono text-[0.7rem] text-muted-foreground">
              {video.channel ?? "Unknown channel"}
            </p>
          </div>
          {tone ? (
            <StatusPill tone={tone} pulse={tone === "in-progress"} />
          ) : (
            <StatusPill tone="queued" label="not started" />
          )}
        </div>
        <div className="flex items-center justify-between font-mono text-[0.7rem] text-muted-foreground">
          <span className="inline-flex items-center gap-1">
            <Scissors className="size-3" />
            {video.clipCount} clips
          </span>
          {video.durationSec != null && (
            <span>{Math.round(video.durationSec / 60)} min</span>
          )}
        </div>
      </div>
    </button>
  );
}
