import { useRef, useState, type DragEvent } from "react";
import { api, formatBytes, type FileInfo } from "../api";

interface Props {
  files: FileInfo[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onUploaded: (f: FileInfo) => void;
}

export function FilesPanel({ files, selectedId, onSelect, onUploaded }: Props) {
  return (
    <section className="panel">
      <h2>Files</h2>
      <UploadBox onUploaded={onUploaded} />
      {files.length === 0 ? (
        <p className="empty">No files yet. Upload one above.</p>
      ) : (
        <div className="table-wrap">
          <table className="files">
            <thead>
              <tr>
                <th>Name</th>
                <th>Size</th>
                <th>Policy</th>
                <th>Status</th>
                <th>Segments</th>
                <th>Uploaded</th>
              </tr>
            </thead>
            <tbody>
              {files.map((f) => (
                <tr
                  key={f.id}
                  className={f.id === selectedId ? "selected" : ""}
                  onClick={() => onSelect(f.id)}
                  tabIndex={0}
                  onKeyDown={(e) => e.key === "Enter" && onSelect(f.id)}
                >
                  <td className="name" title={f.name}>{f.name}</td>
                  <td>{formatBytes(f.size)}</td>
                  <td><span className="tag">{f.policy}</span></td>
                  <td><span className={`status s-${f.status}`}>{f.status}</span></td>
                  <td>{f.segment_count ?? "—"}</td>
                  <td>{new Date(f.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function UploadBox({ onUploaded }: { onUploaded: (f: FileInfo) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [policy, setPolicy] = useState("standard");
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const pick = (f: File | undefined) => {
    if (f) {
      setFile(f);
      setError(null);
    }
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    pick(e.dataTransfer.files[0]);
  };

  const upload = async () => {
    if (!file) return;
    setError(null);
    setProgress(0);
    try {
      const stored = await api.upload(file, policy, setProgress);
      onUploaded(stored);
      setFile(null);
      if (input.current) input.current.value = "";
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setProgress(null);
    }
  };

  const busy = progress !== null;
  return (
    <div
      className={`upload ${dragging ? "dragging" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
    >
      <div className="upload-row">
        <button type="button" className="ghost" onClick={() => input.current?.click()} disabled={busy}>
          Choose file
        </button>
        <span className="upload-name">
          {file ? `${file.name} (${formatBytes(file.size)})` : "…or drop a file here"}
        </span>
        <label className="policy">
          Policy
          <select value={policy} onChange={(e) => setPolicy(e.target.value)} disabled={busy}>
            <option value="standard">standard</option>
            <option value="sensitive">sensitive</option>
          </select>
        </label>
        <button type="button" onClick={upload} disabled={!file || busy}>
          {busy ? (progress! < 1 ? `Sending ${Math.round(progress! * 100)}%` : "Encrypting & striping…") : "Upload"}
        </button>
      </div>
      <input ref={input} type="file" hidden onChange={(e) => pick(e.target.files?.[0])} />
      {busy && (
        <div className="bar">
          <div style={{ width: `${Math.round(progress! * 100)}%` }} />
        </div>
      )}
      {error && <p className="error">Upload failed: {error}</p>}
    </div>
  );
}
