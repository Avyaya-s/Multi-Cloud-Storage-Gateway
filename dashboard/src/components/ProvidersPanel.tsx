import { Line, LineChart, ResponsiveContainer, YAxis } from "recharts";
import type { Provider } from "../api";
import { flag, providerColorVar } from "../providerColors";

export interface LatencySample {
  t: number;
  ms: number | null;
}

interface Props {
  providers: Provider[];
  history: Record<string, LatencySample[]>;
}

export function ProvidersPanel({ providers, history }: Props) {
  const order = providers.map((p) => p.name);
  return (
    <section className="panel">
      <h2>Clouds</h2>
      {providers.length === 0 && <p className="empty">No providers reported yet.</p>}
      <ul className="clouds">
        {providers.map((p) => {
          const stored = p.shards.STORED ?? 0;
          const bad = (p.shards.CORRUPT ?? 0) + (p.shards.MISSING ?? 0);
          return (
            <li key={p.name} className={`cloud ${p.up ? "up" : "down"}`}>
              <span className="swatch" style={{ background: providerColorVar(p.name, order) }} />
              <div className="cloud-main">
                <div className="cloud-head">
                  <strong>{p.name}</strong>
                  <span title={p.jurisdiction === "IN" ? "India" : "Outside India"}>{flag(p.jurisdiction)}</span>
                  <span className={`pill ${p.up ? "ok" : "bad"}`}>{p.up ? "UP" : "DOWN"}</span>
                </div>
                <div className="cloud-meta">
                  <span>{stored} shards</span>
                  {bad > 0 && <span className="warn">{bad} need repair</span>}
                  <span>{p.latency_ms == null ? "—" : `${p.latency_ms.toFixed(0)} ms`}</span>
                </div>
              </div>
              <div className="spark" aria-label={`${p.name} latency history`}>
                <ResponsiveContainer width="100%" height={32}>
                  <LineChart data={history[p.name] ?? []}>
                    <YAxis hide domain={[0, "auto"]} />
                    <Line
                      type="monotone"
                      dataKey="ms"
                      stroke={providerColorVar(p.name, order)}
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                      connectNulls={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </li>
          );
        })}
      </ul>
      <p className="hint">
        Each cloud is probed every few seconds. Gaps in the line mean it did not answer.
      </p>
    </section>
  );
}
