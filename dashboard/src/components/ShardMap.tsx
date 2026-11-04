import { useState } from "react";
import { api, formatBytes, type FileInfo, type Placement, type Provider, type Shard } from "../api";
import { flag, providerColorVar, shortName } from "../providerColors";

const K = 4; // shards needed to rebuild a segment (RS(4,2))

interface Props {
  file: FileInfo | null;
  placement: Placement | null;
  providers: Provider[];
  onDeleted: (id: string) => void;
}

export function ShardMap({ file, placement, providers, onDeleted }: Props) {
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!file) {
    return (
      <section className="panel">
        <h2>Shard map</h2>
        <p className="empty">Select a file to see where its shards live.</p>
      </section>
    );
  }

  const order = providers.map((p) => p.name);
  const byName = new Map(providers.map((p) => [p.name, p]));
  const isUp = (s: Shard) => (s.provider ? byName.get(s.provider)?.up ?? false : false);
  const inIndia = (s: Shard) => (s.provider ? byName.get(s.provider)?.jurisdiction === "IN" : false);
  const readable = (s: Shard) => s.status === "STORED" && isUp(s);

  const segments = placement?.segments ?? [];
  const allRebuildable = segments.every((seg) => seg.shards.filter(readable).length >= K);

  const remove = async () => {
    if (!confirm(`Delete "${file.name}"? Its shards are removed from every cloud.`)) return;
    setDeleting(true);
    setError(null);
    try {
      await api.remove(file.id);
      onDeleted(file.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>
          Shard map: <span className="file-title">{file.name}</span>
        </h2>
        <div className="actions">
          <a className={`button ${allRebuildable ? "" : "disabled"}`} href={api.downloadUrl(file.id)} download>
            Download
          </a>
          <button type="button" className="danger" onClick={remove} disabled={deleting}>
            {deleting ? "Deleting…" : "Delete"}
          </button>
        </div>
      </div>
      {error && <p className="error">{error}</p>}

      <p className={`verdict ${allRebuildable ? "ok" : "bad"}`}>
        {allRebuildable
          ? `✓ Rebuildable right now: every segment has at least ${K} readable shards.`
          : `✗ Not rebuildable right now: some segment has fewer than ${K} readable shards.`}
      </p>

      <div className="table-wrap">
        <table className="shardmap">
          <thead>
            <tr>
              <th>Segment</th>
              {Array.from({ length: 6 }, (_, i) => (
                <th key={i}>{i < K ? `data ${i}` : `parity ${i - K}`}</th>
              ))}
              <th title="Readable shards on clouds that are up right now">Readable</th>
              <th title="Stored shards on Indian clouds">🇮🇳 India</th>
            </tr>
          </thead>
          <tbody>
            {segments.map((seg) => {
              const ok = seg.shards.filter(readable).length;
              const india = seg.shards.filter((s) => s.status === "STORED" && inIndia(s)).length;
              return (
                <tr key={seg.seg_index}>
                  <td className="seg">
                    #{seg.seg_index}
                    <small>{formatBytes(seg.plain_len)}</small>
                  </td>
                  {seg.shards.map((s) => (
                    <td key={s.shard_index}>
                      <ShardCell shard={s} color={providerColorVar(s.provider, order)} up={isUp(s)} />
                    </td>
                  ))}
                  <td className={ok >= K ? "count ok" : "count bad"}>{ok}/6</td>
                  <td className="count">{india}/6</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="legend">
        {providers.map((p) => (
          <span key={p.name}>
            <i style={{ background: providerColorVar(p.name, order) }} />
            {p.name} {flag(p.jurisdiction)}
          </span>
        ))}
        <span><i className="l-missing" />missing</span>
        <span><i className="l-corrupt" />corrupt</span>
        <span><i className="l-down" />cloud down</span>
      </div>
      <p className="hint">
        RS(4,2): any {K} of the 6 shards rebuild a segment, and no cloud holds more than 2 shards of one
        segment, so losing a whole cloud is survivable. Hover a shard for its random object key.
      </p>
    </section>
  );
}

function ShardCell({ shard, color, up }: { shard: Shard; color: string; up: boolean }) {
  const state = shard.status !== "STORED" ? shard.status.toLowerCase() : up ? "stored" : "down";
  const title = [
    `shard ${shard.shard_index} on ${shard.provider ?? "—"}`,
    `status: ${shard.status}${shard.status === "STORED" && !up ? " (cloud down)" : ""}`,
    `object key: ${shard.object_key}`,
    `size: ${formatBytes(shard.size)}`,
  ].join("\n");
  return (
    <span className={`shard ${state}`} style={{ ["--c" as string]: color }} title={title}>
      {shard.provider ? shortName(shard.provider) : "—"}
      {state === "corrupt" && " ✕"}
    </span>
  );
}
