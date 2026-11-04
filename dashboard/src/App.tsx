import { useCallback, useMemo, useRef, useState } from "react";
import { api, type FileInfo, type GatewayEvent, type Placement, type Provider } from "./api";
import { EventsFeed } from "./components/EventsFeed";
import { FilesPanel } from "./components/FilesPanel";
import { ProvidersPanel, type LatencySample } from "./components/ProvidersPanel";
import { ShardMap } from "./components/ShardMap";
import { usePoll } from "./hooks";

const FAST_MS = 2500; // files, events, shard map
const PROVIDERS_MS = 4000; // a down cloud takes ~2 s to time out, so poll it less often
const HISTORY = 30;

export default function App() {
  const [online, setOnline] = useState<boolean | null>(null);
  const [files, setFiles] = useState<FileInfo[]>([]);
  const [events, setEvents] = useState<GatewayEvent[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [history, setHistory] = useState<Record<string, LatencySample[]>>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [placement, setPlacement] = useState<Placement | null>(null);
  const names = useRef(new Map<string, string>()); // remembers names of deleted files for the feed
  const selectedRef = useRef<string | null>(null);

  const choose = (id: string | null) => {
    selectedRef.current = id;
    setSelectedId(id);
  };

  const refresh = useCallback(async () => {
    try {
      const [f, e] = await Promise.all([api.files(), api.events()]);
      f.forEach((x) => names.current.set(x.id, x.name));
      setFiles(f);
      setEvents(e);
      setOnline(true);
      const sel = selectedRef.current;
      if (sel && !f.some((x) => x.id === sel)) {
        choose(null);
        setPlacement(null);
      } else if (sel) {
        const p = await api.placement(sel);
        if (selectedRef.current === sel) setPlacement(p);
      }
    } catch {
      setOnline(false);
    }
  }, []);

  const refreshProviders = useCallback(async () => {
    try {
      const p = await api.providers();
      const t = Date.now();
      setProviders(p);
      setHistory((h) => {
        const next: Record<string, LatencySample[]> = {};
        for (const x of p) next[x.name] = [...(h[x.name] ?? []), { t, ms: x.latency_ms }].slice(-HISTORY);
        return next;
      });
    } catch {
      /* `online` is tracked by the fast loop */
    }
  }, []);

  usePoll(refresh, FAST_MS);
  usePoll(refreshProviders, PROVIDERS_MS);

  const select = async (id: string) => {
    choose(id);
    setPlacement(null);
    const p = await api.placement(id).catch(() => null);
    if (selectedRef.current === id) setPlacement(p);
  };

  const selected = useMemo(() => files.find((f) => f.id === selectedId) ?? null, [files, selectedId]);
  const up = providers.filter((p) => p.up).length;

  return (
    <div className="app">
      <header>
        <div>
          <h1>SovereignStripe</h1>
          <p className="sub">Encrypted, erasure-coded storage across {providers.length || "several"} clouds</p>
        </div>
        <div className="header-stats">
          {providers.length > 0 && (
            <span className={`pill ${up === providers.length ? "ok" : "bad"}`}>
              {up}/{providers.length} clouds up
            </span>
          )}
          <span className={`pill ${online ? "ok" : online === false ? "bad" : ""}`}>
            {online === null ? "Connecting…" : online ? "Gateway online" : "Gateway offline"}
          </span>
          <a className="docs-link" href="/docs" target="_blank" rel="noreferrer">API docs ↗</a>
        </div>
      </header>

      {online === false && (
        <p className="banner">
          Can't reach the gateway at 127.0.0.1:8000. Start it with <code>uvicorn gateway.api.routes:app</code>.
        </p>
      )}

      <main className="grid">
        <ProvidersPanel providers={providers} history={history} />
        <FilesPanel
          files={files}
          selectedId={selectedId}
          onSelect={select}
          onUploaded={(f) => {
            names.current.set(f.id, f.name);
            select(f.id);
            refresh();
          }}
        />
        <div className="wide">
          <ShardMap
            file={selected}
            placement={placement}
            providers={providers}
            onDeleted={() => {
              choose(null);
              setPlacement(null);
              refresh();
            }}
          />
        </div>
        <div className="wide">
          <EventsFeed events={events} fileNames={names.current} />
        </div>
      </main>
    </div>
  );
}
