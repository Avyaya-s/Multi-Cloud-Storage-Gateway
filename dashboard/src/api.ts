// Typed client for the gateway API (same shapes as gateway/api/routes.py).

export type FileStatus = "PENDING" | "ACTIVE" | "DEGRADED" | "FAILED" | "DELETED";
export type ShardStatus = "PENDING" | "STORED" | "MISSING" | "CORRUPT";

export interface FileInfo {
  id: string;
  name: string;
  size: number;
  policy: string;
  status: FileStatus;
  segment_count: number | null;
  created_at: string;
}

export interface Shard {
  shard_index: number;
  provider: string | null;
  status: ShardStatus;
  object_key: string;
  size: number;
}

export interface Segment {
  seg_index: number;
  plain_len: number;
  shards: Shard[];
}

export interface Placement {
  file_id: string;
  segments: Segment[];
}

export interface Provider {
  name: string;
  type: string;
  jurisdiction: "IN" | "NON_IN" | string;
  endpoint: string;
  up: boolean;
  latency_ms: number | null;
  shards: Partial<Record<ShardStatus, number>>;
}

export interface GatewayEvent {
  id: number;
  ts: string;
  type: string;
  details: Record<string, unknown>;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function detail(r: Response): Promise<string> {
  try {
    const body = await r.json();
    return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
  } catch {
    return r.statusText;
  }
}

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new ApiError(r.status, await detail(r));
  return r.json() as Promise<T>;
}

export const api = {
  files: () => getJson<FileInfo[]>("/files"),
  placement: (id: string) => getJson<Placement>(`/files/${id}/placement`),
  providers: () => getJson<Provider[]>("/providers"),
  events: (limit = 40) => getJson<GatewayEvent[]>(`/events?limit=${limit}`),
  downloadUrl: (id: string) => `/files/${id}/download`,

  async remove(id: string): Promise<void> {
    const r = await fetch(`/files/${id}`, { method: "DELETE" });
    if (!r.ok) throw new ApiError(r.status, await detail(r));
  },

  /** XHR instead of fetch so we can report upload progress. */
  upload(file: File, policy: string, onProgress: (fraction: number) => void): Promise<FileInfo> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const form = new FormData();
      form.append("file", file);
      form.append("policy", policy);
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
      xhr.onload = () => {
        let body: { detail?: unknown } = {};
        try {
          body = JSON.parse(xhr.responseText);
        } catch {
          /* non-JSON error page */
        }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body as FileInfo);
        else reject(new ApiError(xhr.status, String(body.detail ?? xhr.statusText)));
      };
      xhr.onerror = () => reject(new ApiError(0, "gateway not reachable"));
      xhr.open("POST", "/files");
      xhr.send(form);
    });
  },
};

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}
