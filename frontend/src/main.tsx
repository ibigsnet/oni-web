import { Component, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { invoke } from "@web/bridge";
import "@fontsource/jetbrains-mono/400.css";
import "./theme.css";
import { Session } from "./web/Session";

/** What went wrong, on screen and in oni-rcon.log: the window's background is the console's own near-black, so a
 *  fault that drew nothing would otherwise look like a black screen and say nothing. */
export function fatal(what: string, err: unknown) {
  const text = `${what}: ${err instanceof Error ? `${err.message}\n${err.stack ?? ""}` : String(err)}`;
  invoke("log_error", { text }).catch(() => {});
  const root = document.getElementById("fatal") ?? document.body.appendChild(Object.assign(document.createElement("div"), { id: "fatal" }));
  root.className = "fatal";
  root.textContent = "";
  const head = document.createElement("div");
  head.style.color = "var(--red)";
  head.textContent = "ONI RCON HIT A FAULT · reload the page; the backend records the error type";
  const body = document.createElement("pre");
  body.textContent = text;
  root.append(head, body);
}

window.addEventListener("error", (e) => fatal("Error", e.error ?? e.message));
window.addEventListener("unhandledrejection", (e) => fatal("Unhandled", e.reason));

class Guard extends Component<{ children: ReactNode }, { err: unknown }> {
  state = { err: null as unknown };
  static getDerivedStateFromError(err: unknown) {
    return { err };
  }
  componentDidCatch(err: unknown) {
    fatal("Render", err);
  }
  render() {
    return this.state.err ? null : this.props.children;
  }
}

createRoot(document.getElementById("root")!).render(<Guard><Session /></Guard>);
