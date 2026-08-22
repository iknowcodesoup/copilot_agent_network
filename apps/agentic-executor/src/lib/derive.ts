import type { VoiceRun, VoiceRunPhase } from "./types"

// ---------------------------------------------------------------------------
// Client-derived values.
//
// The live wire types intentionally omit a few things the UI needs. Per the
// migration decisions, we synthesize them here instead of storing them on the
// backend contract:
//   - voice color: auto-assigned from a stable hash of the voice id
//   - clip audio URL: composed from the video + clip ids
//   - phase → status-pill tone / progress semantics
// ---------------------------------------------------------------------------

/** Base URL of the voice API. Empty = same-origin (the simulated backend). */
export const VOICE_API_BASE = process.env.NEXT_PUBLIC_VOICE_API_BASE ?? ""

/** Audio for a clip is composable from its video + clip id (no stored URL). */
export function clipAudioUrl(videoId: string, clipId: string): string {
  return `${VOICE_API_BASE}/videos/${videoId}/clips/${clipId}/audio`
}

const VOICE_PALETTE = [
  "oklch(0.66 0.19 293)",
  "oklch(0.7 0.15 155)",
  "oklch(0.78 0.14 78)",
  "oklch(0.7 0.12 235)",
  "oklch(0.68 0.19 12)",
  "oklch(0.72 0.16 330)",
]

/** Deterministic per-voice accent color derived from the voice id. */
export function voiceColor(id: string): string {
  let h = 0
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0
  return VOICE_PALETTE[h % VOICE_PALETTE.length]
}

export type PillTone = "in-progress" | "complete" | "failed" | "queued" | "running" | "neutral"

/** Map a run phase to a StatusPill tone. */
export function phaseTone(phase: VoiceRunPhase): PillTone {
  switch (phase) {
    case "downloading":
    case "diarizing":
    case "committing":
      return "in-progress"
    case "awaiting_review":
      return "queued"
    case "training":
    case "exporting":
      return "running"
    case "ready":
      return "complete"
    case "failed":
      return "failed"
    default:
      return "neutral"
  }
}

/** Phases where work is actively advancing (drives the indeterminate bar). */
export function isPhaseActive(phase: VoiceRunPhase): boolean {
  return (
    phase === "downloading" ||
    phase === "diarizing" ||
    phase === "committing" ||
    phase === "training" ||
    phase === "exporting"
  )
}

/** A run's display title.

    The video's real name belongs to the video, which the factory owns, so a
    run names itself by the character it is training. Where the video's title
    matters, read it from the videos list and join on videoId. */
export function runTitle(run: VoiceRun): string {
  return run.primaryCharacter || run.sourceUrl
}
