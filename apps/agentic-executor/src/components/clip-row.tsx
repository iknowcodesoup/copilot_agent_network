"use client";

import { useEffect, useState } from "react";
import { Check, X, Pencil, AudioLines } from "lucide-react";
import { VoiceSpeakerCombobox } from "./voice-speaker-combobox";
import { AudioPlayerBar } from "./audio-player-bar";
import { cn } from "@/lib/utils";
import type { StudioClip } from "@/lib/types";
import { clipAudioUrl, useUpdateClips } from "@/lib/voice_api";

/*
 * Clip writes target clip.videoId directly, never useStudio's activeVideoId.
 * That "active" id is StudioProvider's own fallback guess (first run's video)
 * and can name a different video than the one this row is actually showing,
 * which was silently sending edits to the wrong video's clips.
 */
export function ClipRow({ clip }: { clip: StudioClip }) {
  const updateClips = useUpdateClips(clip.videoId);
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(clip.text);

  useEffect(() => {
    if (!editing) setText(clip.text);
  }, [clip.text, editing]);
  const saveText = () => {
    setEditing(false);
    if (text.trim() && text !== clip.text)
      updateClips.mutate([{ clipId: clip.clipId, text: text.trim() }]);
  };
  /* One clip, one assignment. It is written on the clip itself, so picking a
     voice here moves this clip and no other. */
  const assignVoice = (_voiceId: string, voiceName: string) =>
    updateClips.mutate([{ clipId: clip.clipId, assignedVoice: voiceName }]);

  return (
    <div
      className={cn(
        "rounded-lg border bg-background/40 p-3",
        !clip.keep && "opacity-75",
        clip.keep ? "border-success/30" : "border-border",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[0.7rem] text-muted-foreground/60">
          #{String(clip.index).padStart(2, "0")}
        </span>
        <VoiceSpeakerCombobox
          speakerLabel={clip.speakerLabel ?? clip.clipId}
          assignedVoiceName={clip.assignedVoice}
          onSelect={assignVoice}
        />
        {/* A rejected write must say so. Swallowing it is what made a failed
            assignment look like a dead control. */}
        {updateClips.isError && (
          <span
            role="alert"
            className="max-w-xs truncate text-[0.65rem] text-destructive"
            title={updateClips.error.message}
          >
            {updateClips.error.message}
          </span>
        )}
        {clip.flagged && (
          <span className="inline-flex items-center gap-1 rounded-md border border-warn/30 bg-warn/10 px-1.5 py-0.5 font-mono text-[0.65rem] uppercase text-warn">
            <AudioLines className="size-3" /> flagged
          </span>
        )}
        <div className="ml-auto flex items-center gap-1.5">
          <span
            className={cn(
              "font-mono text-[0.65rem] uppercase",
              clip.keep ? "text-success" : "text-muted-foreground",
            )}
          >
            {clip.keep ? "kept" : "excluded"}
          </span>
          <button
            type="button"
            onClick={() => updateClips.mutate([{ clipId: clip.clipId, keep: true }])}
            aria-label="Keep clip"
            className={cn(
              "flex size-7 items-center justify-center rounded-md border",
              clip.keep
                ? "border-success/40 bg-success/15 text-success"
                : "border-border text-muted-foreground hover:text-success",
            )}
          >
            <Check className="size-3.5" />
          </button>
          <button
            type="button"
            onClick={() => updateClips.mutate([{ clipId: clip.clipId, keep: false }])}
            aria-label="Exclude clip"
            className={cn(
              "flex size-7 items-center justify-center rounded-md border",
              !clip.keep
                ? "border-destructive/40 bg-destructive/15 text-destructive"
                : "border-border text-muted-foreground hover:text-destructive",
            )}
          >
            <X className="size-3.5" />
          </button>
        </div>
      </div>
      <div className="mt-2">
        {editing ? (
          <textarea
            autoFocus
            value={text}
            onChange={(e) => setText(e.target.value)}
            onBlur={saveText}
            onKeyDown={(e) => {
              if (
                e.key === "Enter" &&
                !e.shiftKey &&
                !e.nativeEvent.isComposing
              ) {
                e.preventDefault();
                saveText();
              }
              if (e.key === "Escape") {
                setText(clip.text);
                setEditing(false);
              }
            }}
            rows={2}
            className="w-full resize-none rounded-md border border-input bg-background px-2 py-1.5 text-sm leading-relaxed outline-none"
          />
        ) : (
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="group flex w-full items-start gap-1.5 rounded-md px-1 py-0.5 text-left text-sm leading-relaxed text-foreground/90 hover:bg-muted/40"
          >
            <span className="flex-1">{clip.text}</span>
            <Pencil className="mt-1 size-3 shrink-0 text-muted-foreground/0 group-hover:text-muted-foreground" />
          </button>
        )}
      </div>
      <div className="mt-2">
        <AudioPlayerBar
          src={clipAudioUrl(clip.videoId, clip.clipId)}
          peaks={[]}
          durationSec={clip.durationSec ?? 0}
          accent="var(--primary)"
          disabled={!clip.keep}
        />
      </div>
    </div>
  );
}
