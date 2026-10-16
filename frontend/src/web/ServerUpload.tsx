import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { api, clientId } from "./bridge";

// IBIGS 2026-10-16: send files to this server. Everything lands in the server's Upload folder and sorts itself
// (mods, Halo 3 server files, lobby settings, Forge maps / game types / playlists). One file or one folder = one item.

type Result = { state: "done" | "failed"; name: string; at: number; text: string };
type Status = { results: Result[]; waiting: string[]; free_bytes: number };
type Item = { name: string; files: { file: File; path: string }[]; sent: number; total: number;
  state: "sending" | "sorting" | "done" | "failed" | "error"; detail: string; queued: string[] };

const gb = (n: number) => n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(0)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`;

function send(batch: string, path: string, file: File, progress: (n: number) => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/server-upload");
    xhr.withCredentials = true;
    xhr.setRequestHeader("X-ONI-Request", "1");
    xhr.setRequestHeader("X-ONI-Client", clientId);
    xhr.setRequestHeader("X-ONI-Batch", batch);
    xhr.setRequestHeader("X-ONI-Path", encodeURIComponent(path).replace(/%2F/g, "/"));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => progress(e.loaded);
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) { progress(file.size); resolve(); return; }
      if (xhr.status === 401) window.dispatchEvent(new Event("oni-signout"));
      let detail = `upload failed (${xhr.status})`;
      try { const d = JSON.parse(xhr.responseText).detail; if (typeof d === "string") detail = d; } catch { /* keep */ }
      reject(new Error(detail));
    };
    xhr.onerror = () => reject(new Error("connection lost while sending"));
    xhr.send(file);
  });
}

async function walk(entry: FileSystemEntry, prefix: string, out: { file: File; path: string }[]): Promise<void> {
  if (entry.isFile) {
    const file = await new Promise<File>((ok, bad) => (entry as FileSystemFileEntry).file(ok, bad));
    out.push({ file, path: prefix + entry.name });
  } else if (entry.isDirectory) {
    const reader = (entry as FileSystemDirectoryEntry).createReader();
    for (;;) {
      const batch = await new Promise<FileSystemEntry[]>((ok, bad) => reader.readEntries(ok, bad));
      if (!batch.length) break;
      for (const child of batch) await walk(child, `${prefix}${entry.name}/`, out);
    }
  }
}

export function ServerUploadButton() {
  const [available, setAvailable] = useState(false);
  const [open, setOpen] = useState(false);
  useEffect(() => { api<Status>("/api/server-upload").then(() => setAvailable(true)).catch(() => setAvailable(false)); }, []);
  if (!available) return null;
  return <>
    <button onClick={() => setOpen(true)} title="Send mods, server files, settings or Forge content to this server">UPLOAD FILES</button>
    {open && <ServerUpload onClose={() => setOpen(false)} />}
  </>;
}

function ServerUpload({ onClose }: { onClose: () => void }) {
  const [items, setItems] = useState<Item[]>([]);
  const [status, setStatus] = useState<Status | null>(null);
  const [over, setOver] = useState(false);
  const [error, setError] = useState("");
  const filesInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const refresh = useCallback(() => api<Status>("/api/server-upload").then(setStatus).catch((e) => setError(String(e.message || e))), []);
  useEffect(() => { refresh(); const t = window.setInterval(refresh, 3000); return () => window.clearInterval(t); }, [refresh]);
  useEffect(() => {
    if (!status) return;
    setItems((list) => list.map((it) => {
      if (it.state !== "sorting") return it;
      const hit = status.results.find((r) => it.queued.includes(r.name) || it.queued.some((q) => r.name.startsWith(q)));
      if (!hit) return it;
      const lines = hit.text.split("\n").filter((l) => l.trim());
      return { ...it, state: hit.state, detail: (hit.state === "done" ? lines.slice(1) : lines.slice(1)).join("\n") || lines.join("\n") };
    }));
  }, [status]);

  const update = (name: string, change: Partial<Item>) => setItems((list) => list.map((it) => it.name === name ? { ...it, ...change } : it));
  const start = async (groups: Item[]) => {
    setError("");
    setItems((list) => [...groups, ...list]);
    for (const g of groups) {
      const batch = crypto.randomUUID();
      let base = 0;
      try {
        for (const f of g.files) {
          await send(batch, f.path, f.file, (n) => update(g.name, { sent: base + n }));
          base += f.file.size;
        }
        const done = await api<{ queued: string[] }>("/api/server-upload/done", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ batch }) });
        update(g.name, { state: "sorting", sent: g.total, queued: done.queued, detail: "Sorting on the server…" });
      } catch (e) {
        update(g.name, { state: "error", detail: String((e as Error).message || e) });
        api("/api/server-upload/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ batch }) }).catch(() => undefined);
      }
    }
    refresh();
  };
  const group = (files: { file: File; path: string }[]): Item[] => {
    const by = new Map<string, { file: File; path: string }[]>();
    for (const f of files) {
      const top = f.path.includes("/") ? f.path.split("/")[0] : f.path;
      by.set(top, [...(by.get(top) || []), f]);
    }
    return [...by].map(([name, fs]) => ({ name, files: fs, sent: 0, total: fs.reduce((n, f) => n + f.file.size, 0),
      state: "sending", detail: "", queued: [] }));
  };
  const pickFiles = (list: FileList | null, folder: boolean) => {
    if (!list?.length) return;
    const files = [...list].map((file) => ({ file, path: folder ? (file.webkitRelativePath || file.name) : file.name }));
    start(group(files));
  };
  const drop = async (e: DragEvent) => {
    e.preventDefault(); setOver(false);
    const out: { file: File; path: string }[] = [];
    const entries = [...e.dataTransfer.items].map((i) => i.webkitGetAsEntry?.()).filter(Boolean) as FileSystemEntry[];
    if (entries.length) for (const entry of entries) await walk(entry, "", out);
    else for (const file of [...e.dataTransfer.files]) out.push({ file, path: file.name });
    if (out.length) start(group(out));
  };

  return <div className="web-modal" role="dialog" aria-label="Upload files to this server" onClick={(e) => e.target === e.currentTarget && onClose()}>
    <div className="web-modal-card upload-card">
      <div className="upload-head"><h2>UPLOAD TO THIS SERVER</h2><button onClick={onClose} aria-label="Close">CLOSE</button></div>
      <p>Drop anything here. It is checked and put where it belongs:</p>
      <ul className="upload-kinds">
        <li><b>Workshop mods</b>: the zip, or the whole folder with <code>ModInfo.json</code></li>
        <li><b>Halo 3 server files</b>: a zip or folder with <code>halo3/halo3.dll</code></li>
        <li><b>Lobby settings</b>: a <code>dedicated.toml</code> (matched to its lobby; the old one is kept)</li>
        <li><b>Forge</b>: ReclaimerForge zips, maps, game types and playlists</li>
      </ul>
      <div className={`upload-drop${over ? " over" : ""}`} onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)} onDrop={drop}>
        <span>Drag files or folders here</span>
        <div>
          <button className="btn" onClick={() => filesInput.current?.click()}>CHOOSE FILES</button>
          <button className="btn" onClick={() => folderInput.current?.click()}>CHOOSE A FOLDER</button>
        </div>
        <input ref={filesInput} type="file" multiple hidden onChange={(e) => { pickFiles(e.target.files, false); e.target.value = ""; }} />
        <input ref={folderInput} type="file" hidden {...{ webkitdirectory: "", directory: "" }}
          onChange={(e) => { pickFiles(e.target.files, true); e.target.value = ""; }} />
      </div>
      {status && <small className="web-muted">Free space on the server: {gb(status.free_bytes)}. Big mods can take a few minutes to send.</small>}
      {error && <p className="web-error" role="alert">{error}</p>}
      {items.length > 0 && <ul className="upload-items">{items.map((it) => <li key={it.name} className={`upload-${it.state}`}>
        <div><b>{it.name}</b> <span>{it.state === "sending" ? `sending ${gb(it.sent)} of ${gb(it.total)}`
          : it.state === "sorting" ? "sorting…" : it.state === "done" ? "installed" : it.state === "failed" ? "not installed" : "not sent"}</span></div>
        {it.state === "sending" && <progress max={it.total || 1} value={it.sent} />}
        {it.detail && <pre>{it.detail}</pre>}
      </li>)}</ul>}
      {status && status.results.length > 0 && <details className="upload-history"><summary>Earlier uploads</summary>
        <ul>{status.results.slice(0, 15).map((r) => <li key={r.name + r.at} className={`upload-${r.state}`}>
          <b>{r.name}</b> <span>{r.state === "done" ? "installed" : "not installed"}</span>
          <pre>{r.text.split("\n").slice(1).join("\n")}</pre></li>)}</ul></details>}
    </div>
  </div>;
}
