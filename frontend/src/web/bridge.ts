/** Browser replacement for Tauri IPC. The only socket opened here is to our own gateway. */
type EventCallback<T> = (event: { payload: T }) => void;
type Callback = EventCallback<unknown>;
const listeners = new Map<string, Set<Callback>>();
export const clientId = crypto.randomUUID();
let socket: WebSocket | null = null;
let retry: ReturnType<typeof setTimeout> | undefined;
let cursor = 0;
let generation = -1;
let fleetGeneration = -1;
let instance = "";
let stopped = false;

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options, credentials: "same-origin", redirect: "error",
    headers: { "X-ONI-Request": "1", "X-ONI-Client": clientId, ...options.headers },
  });
  if (response.status === 401) window.dispatchEvent(new Event("oni-signout"));
  const result = await response.json().catch(() => ({ detail: "The gateway returned an unreadable response" }));
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Request failed");
  return result as T;
}

export async function invoke<T = unknown>(name: string, args: Record<string, unknown> = {}): Promise<T> {
  if (name === "forge_pick") return chooseFiles() as Promise<T>;
  const body = "index" in args || "indices" in args ? { ...args, _generation: fleetGeneration } : args;
  return api<T>(`/api/rpc/${encodeURIComponent(name)}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }).then((result) => {
    if (name === "start_console") fleetGeneration = (result as { generation: number }).generation;
    return result;
  }).catch((error) => {
    if (error instanceof TypeError) throw new Error("Gateway connection lost. The operation may have run; it was not resent.");
    throw error;
  });
}

function webStatus(connected: boolean, detail = "") {
  window.dispatchEvent(new CustomEvent("oni-web-status", { detail: { connected, detail } }));
}

function pump() {
  if (stopped || socket || ![...listeners.values()].some((s) => s.size)) return;
  const url = new URL("/api/events", location.href);
  url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("after", String(cursor));
  const ws = socket = new WebSocket(url);
  ws.onopen = () => { if (socket === ws) webStatus(true); };
  ws.onmessage = (event) => {
    if (socket !== ws) return;
    let messages: Record<string, any>[];
    try { messages = JSON.parse(event.data); } catch { ws.close(); return; }
    if (!Array.isArray(messages)) return;
    const batch = messages.filter((m) => {
      if (m.kind === "stream") {
        if (listeners.get("oni")?.size && ((instance && instance !== m.instance) || (generation !== -1 && generation !== m.generation))) {
          cursor = 0;
          window.dispatchEvent(new Event("oni-reload"));
        }
        instance = m.instance;
        generation = m.generation;
        fleetGeneration = m.generation;
        return false;
      }
      if (m.kind === "reload") { if (listeners.get("oni")?.size) window.dispatchEvent(new Event("oni-reload")); return false; }
      if (m.kind === "setup-line") {
        for (const callback of listeners.get("setup-line") ?? []) callback({ payload: m.text });
        return false;
      }
      if (m.kind === "heartbeat") return false;
      if (typeof m.seq === "number") {
        if (m.seq <= cursor) return false;
        cursor = m.seq;
      }
      return true;
    });
    if (batch.length) for (const callback of listeners.get("oni") ?? []) callback({ payload: batch });
  };
  ws.onclose = (event) => {
    if (socket !== ws) return;
    socket = null;
    webStatus(false, event.code === 1008 ? "Session expired; sign in again" : "Gateway interrupted; reconnecting…");
    if (event.code === 1008) {
      api<{ authenticated: boolean }>("/api/session").then((s) => {
        if (!s.authenticated) window.dispatchEvent(new Event("oni-signout"));
      }).catch(() => {});
    }
    if (!stopped) retry = setTimeout(pump, 2000);
  };
  ws.onerror = () => ws.close();
}

export async function listen<T>(name: string, callback: EventCallback<T>): Promise<() => void> {
  stopped = false;
  const set = listeners.get(name) ?? new Set<Callback>();
  set.add(callback as Callback);
  listeners.set(name, set);
  if (name === "oni") {
    // Setup may already have a progress stream. A newly mounted console needs its own current-state snapshot.
    const old = socket;
    socket = null;
    old?.close();
    cursor = 0;
    generation = -1;
  }
  pump();
  return () => {
    set.delete(callback as Callback);
    if (![...listeners.values()].some((x) => x.size)) closeEvents();
  };
}

export function closeEvents() {
  stopped = true;
  clearTimeout(retry);
  const old = socket;
  socket = null;
  old?.close();
  webStatus(false);
  cursor = 0;
  generation = -1;
}

export async function writeText(value: string) {
  if (!navigator.clipboard) throw new Error("Clipboard access requires HTTPS or localhost");
  await navigator.clipboard.writeText(value);
}

export async function signOut() {
  await api("/api/logout", { method: "POST", body: "{}" });
  closeEvents();
  window.dispatchEvent(new Event("oni-signout"));
}

export function getCurrentWindow() {
  return { close: signOut };
}

async function chooseFiles(): Promise<{ files: { name: string; size: number }[] }> {
  const files = await new Promise<File[]>((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.accept = ".json,.mvar,.bin";
    input.addEventListener("change", () => resolve(Array.from(input.files ?? [])), { once: true });
    input.addEventListener("cancel", () => resolve([]), { once: true });
    input.click();
  });
  if (files.length > 10 || files.some((f) => f.size === 0) || files.reduce((n, f) => n + f.size, 0) > 100 * 1024 * 1024) {
    throw new Error("Choose 1–10 nonempty files totaling at most 100 MiB");
  }
  await invoke("upload_clear");
  const result: { name: string; size: number }[] = [];
  for (const file of files) {
    result.push(await api("/api/uploads", { method: "POST", body: file,
      headers: { "Content-Type": "application/octet-stream", "X-ONI-Filename": encodeURIComponent(file.name) } }));
  }
  return { files: result };
}
