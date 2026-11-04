import type { GatewayEvent } from "../api";

interface Props {
  events: GatewayEvent[];
  fileNames: Map<string, string>;
}

export function EventsFeed({ events, fileNames }: Props) {
  const name = (id: unknown) => {
    const s = String(id ?? "");
    return fileNames.get(s) ?? `file ${s.slice(0, 8)}`;
  };

  const describe = (e: GatewayEvent): string => {
    const d = e.details;
    switch (e.type) {
      case "UPLOAD":
        return `${name(d.file_id)} stored as ${d.status}`;
      case "REROUTE":
        return `${name(d.file_id)} seg ${d.seg_index} shard ${d.shard_index}: ${d.from} → ${d.to}`;
      case "REPAIR_QUEUED":
        return `${name(d.file_id)} is missing a shard; repair queued`;
      case "ROLLBACK":
        return `${name(d.file_id)} upload rolled back (${d.reason})`;
      case "CORRUPT": {
        const shards = (d.shards as [number, number][] | undefined) ?? [];
        return `${name(d.file_id)}: bad shard ${shards.map(([s, i]) => `seg ${s}/shard ${i}`).join(", ")} rejected`;
      }
      case "DELETE":
        return `${name(d.file_id)} deleted`;
      default:
        return JSON.stringify(d);
    }
  };

  return (
    <section className="panel">
      <h2>Events</h2>
      {events.length === 0 ? (
        <p className="empty">Nothing has happened yet.</p>
      ) : (
        <ol className="events">
          {events.map((e) => (
            <li key={e.id}>
              <time dateTime={e.ts}>{new Date(e.ts).toLocaleTimeString()}</time>
              <span className={`etype e-${e.type}`}>{e.type}</span>
              <span className="edesc">{describe(e)}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
