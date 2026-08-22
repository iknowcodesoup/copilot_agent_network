"use client";

import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StudioProvider, useStudio } from "@/components/studio-provider";
import { VoiceLiveState } from "@/lib/voice_event_stream";
import { VideosView } from "@/components/videos-view";
import { VoicesView } from "@/components/voices-view";
import { LogMonitor } from "@/components/log-monitor";
import { ChatPanel } from "@/components/chat-panel";
import { AddVideoBar } from "@/components/add-video-bar";

function ViewTabs() {
  const { view, setView, snapshot } = useStudio();
  const tabs: { id: "videos" | "voices"; label: string; count: number }[] = [
    { id: "videos", label: "Videos", count: snapshot.videos.length },
    { id: "voices", label: "Voices", count: snapshot.voices.length },
  ];
  return (
    <div className="flex items-center gap-1 rounded-lg border border-border bg-card p-1">
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          onClick={() => setView(t.id)}
          className={
            view === t.id
              ? "flex items-center gap-2 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-accent-foreground"
              : "flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
          }
        >
          {t.label}
          <span
            className={
              view === t.id
                ? "rounded-full bg-accent-foreground/20 px-1.5 text-[11px]"
                : "rounded-full bg-muted px-1.5 text-[11px]"
            }
          >
            {t.count}
          </span>
        </button>
      ))}
    </div>
  );
}

function ConnectionBadge() {
  const { connected } = useStudio();
  return (
    <div className="flex items-center gap-2 rounded-md border border-border bg-card px-2.5 py-1.5">
      <span className="relative flex h-2 w-2">
        {connected && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-[var(--color-status-complete)] opacity-60" />
        )}
        <span
          className="relative inline-flex h-2 w-2 rounded-full"
          style={{
            background: connected
              ? "var(--color-status-complete)"
              : "var(--color-status-failed)",
          }}
        />
      </span>
      <span className="font-mono text-[11px] text-muted-foreground">
        {connected ? "query connected" : "query unavailable"}
      </span>
    </div>
  );
}

function StudioShell() {
  const { view } = useStudio();
  const [logOpen, setLogOpen] = useState(true);

  return (
    <div className="flex h-screen w-full overflow-hidden bg-background text-foreground">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-col gap-3 border-b border-border px-6 py-4">
          <div className="flex items-center justify-between gap-4">
            <div className="flex items-center gap-3">
              <div className="flex h-9 w-9 items-center justify-center rounded-md bg-accent font-mono text-sm font-bold text-accent-foreground">
                VS
              </div>
              <div>
                <h1 className="font-mono text-base font-semibold leading-tight text-foreground">
                  Voice Studio
                </h1>
                <p className="text-[11px] text-muted-foreground">
                  YouTube to diarized clips to trained voice models
                </p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <ConnectionBadge />
              <button
                type="button"
                onClick={() => setLogOpen((o) => !o)}
                className="rounded-md border border-border bg-card px-2.5 py-1.5 font-mono text-[11px] text-muted-foreground transition-colors hover:text-foreground"
              >
                {logOpen ? "Hide logs" : "Show logs"}
              </button>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <ViewTabs />
            <div className="min-w-0 flex-1">
              <AddVideoBar />
            </div>
          </div>
        </header>

        <div className="flex min-h-0 flex-1 flex-col">
          <main className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
            {view === "videos" ? <VideosView /> : <VoicesView />}
          </main>
          {logOpen && (
            <div className="h-56 shrink-0 border-t border-border">
              <LogMonitor />
            </div>
          )}
        </div>
      </div>

      <div className="hidden w-80 shrink-0 lg:block xl:w-96">
        <ChatPanel />
      </div>
    </div>
  );
}

export default function Page() {
  const [queryClient] = useState(() => new QueryClient());
  return (
    <QueryClientProvider client={queryClient}>
      {/* One connection for the dashboard. Renders nothing; it only writes
          pushed state into the query cache every hook below already reads. */}
      <VoiceLiveState />
      <StudioProvider>
        <StudioShell />
      </StudioProvider>
    </QueryClientProvider>
  );
}
