"""Piping/batch equivalent: stdin lines or --command, JSON replies on stdout; no automatic retries."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def parse_line(line):
    # Preserve the upstream console's rest-of-line arguments for these four commands.
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    command = next(lexer, None)
    if command is None:
        return []
    lead = {"say": 0, "tell": 1, "kick": 1, "servername": 0}.get(command.lower())
    if lead is None:
        return [command, *lexer]
    args = [command]
    for _ in range(lead):
        value = next(lexer, None)
        if value is None:
            return args
        args.append(value)
    # shlex consumed the space after the last token; its stream points at the remaining free text.
    rest = lexer.instream.read().strip()
    if len(rest) > 1 and rest[0] == rest[-1] and rest[0] in "'\"" and rest[0] not in rest[1:-1]:
        rest = rest[1:-1]
    if rest:
        args.append(rest)
    return args


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--profile", help="Saved profile ID to connect; omitted: reuse the active fleet")
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--command", "-c")
    parser.add_argument("--delay", type=float, default=0, help="Seconds between commands (0–300)")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    if not 0 <= args.delay <= 300 or not .1 <= args.timeout <= 120:
        parser.error("delay must be 0–300, timeout 0.1–120")
    parsed = urlsplit(args.url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        parser.error("Use an http(s) gateway URL without credentials")
    token = os.getenv("ONI_WEB_TOKEN", "")
    if token and parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        parser.error("Use HTTPS for a remote gateway")

    def rpc(name, payload):
        headers = {"Content-Type": "application/json", "X-ONI-Request": "1"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(args.url.rstrip("/") + "/api/rpc/" + name, json.dumps(payload).encode(), headers, method="POST")
        with urlopen(request, timeout=max(20, args.timeout + 5)) as response:
            return json.load(response)
    try:
        info = rpc("start_console", {"ids": [args.profile]} if args.profile else {})
        # Wait only on read-only gateway state. Never probe with or retry an RCON command.
        deadline = time.monotonic() + 12
        while True:
            states = rpc("connection_states", {})
            if not 0 <= args.index < len(states["links"]):
                raise ValueError("No such station")
            state = states["links"][args.index]["state"]
            if state == "online":
                break
            if state == "denied" or time.monotonic() > deadline:
                raise ValueError("Station is not online")
            time.sleep(.2)
        lines = [args.command] if args.command else sys.stdin
        failed = False
        for line in lines:
            parts = parse_line(line.strip())
            if not parts:
                continue
            result = rpc("call", {"index": args.index, "_generation": info["generation"],
                                  "command": parts[0], "args": parts[1:], "timeout": args.timeout})
            print(json.dumps(result, ensure_ascii=False), flush=True)
            failed |= result.get("ok") is not True
            if args.delay:
                time.sleep(args.delay)
        return 1 if failed else 0
    except (HTTPError, URLError, TimeoutError, ValueError):
        print("Gateway request failed. The command may have run and was not resent.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
