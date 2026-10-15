"""The setup screen: add servers in a form instead of a config file, each one tested before it's saved."""
from __future__ import annotations

import asyncio
import contextlib
import itertools
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Collapsible, Footer, Input, Label, Static

from .app import AMBER, CYAN, DIM, GREEN, ONI, RED, SPIN, WHITE, emblem, explain
from .config import Server, add_servers, parse_target
from .rcon import Rcon, Tunnel


def target(address: str, port: str) -> str:
    """What parse_target reads, from the two boxes: an address may carry its own port, or be a URL."""
    address, port = address.strip(), port.strip()
    if "://" in address or not port:
        return address
    if address.count(":") > 1 and not address.startswith("["):  # bare IPv6
        return f"[{address}]:{port}"
    if ":" in address.rsplit("]", 1)[-1]:  # it has a port already
        return address
    return f"{address or '127.0.0.1'}:{port}"


async def probe(s: Server, timeout: float = 12) -> tuple[bool, str]:
    """Sign in once and hang up: (worked, what to tell the operator)."""
    done: asyncio.Future = asyncio.get_running_loop().create_future()
    tasks, ready, url = [], None, s.url

    def settle(ok: bool, text: str) -> None:
        if not done.done():
            done.set_result((ok, text))

    def on_state(rc: Rcon, state: str, detail: str) -> None:
        if state == "online":
            settle(True, f"Connected to {rc.info.get('server') or s.where}.")
        elif state in ("denied", "offline"):
            settle(False, explain(detail, state))

    if s.ssh and not s.url:
        tun = Tunnel(s.ssh, [(s.host, s.port)], lambda t, state, d: state == "down" and settle(False, explain(d)))
        tasks.append(asyncio.create_task(tun.run()))
        url, ready = f"ws://127.0.0.1:{tun.local[(s.host, s.port)]}", tun.ready
    elif not url:
        url = f"ws://[{s.host}]:{s.port}" if ":" in s.host else f"ws://{s.host}:{s.port}"
    tasks.append(asyncio.create_task(Rcon(url, s.password, "setup", lambda *_: None, on_state, ready).run()))
    try:
        return await asyncio.wait_for(done, timeout)
    except TimeoutError:
        return False, "No answer in time. Check the address and port, and any firewall in between."
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)


class SetupApp(App):
    """Returns the servers added, "demo" to look around first, or None when closed."""
    CSS_PATH = "oni.tcss"
    TITLE = "ONI RCON · SETUP"
    BINDINGS = [Binding("ctrl+q", "quit", "Quit")]

    def __init__(self, path: Path, existing: list[Server], by: str, ask_by: bool):
        super().__init__()
        self.path, self.existing, self.by, self.ask_by = path, existing, by, ask_by
        self.added: list[Server] = []
        self.spin = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="setup"):
            with Center():
                yield Static(id="setup-emblem")
            yield Static(Text.assemble(("O N I   R C O N\n", AMBER),
                                       ("Let's connect to your Halo 3 server. You need its address and RCON password "
                                        "(dedicated.toml, under [rcon]).", DIM)), id="setup-title")
            with Center(), Horizontal(id="setup-main"):
                yield Static(id="setup-side")
                with Vertical(id="setup-form", classes="dialog"):
                    with Horizontal(classes="setup-row"):
                        with Vertical(classes="setup-wide"):
                            yield Label("Address")
                            yield Input("127.0.0.1", id="address", placeholder="IP, host name or wss:// link")
                        with Vertical(classes="setup-narrow"):
                            yield Label("Port")
                            yield Input("11774", id="port", placeholder="game port", type="integer")
                    yield Static("Where the server runs: 127.0.0.1 is this computer. RCON shares the game port.",
                                 classes="hint")
                    with Horizontal(classes="setup-row"):
                        with Vertical(classes="setup-wide"):
                            yield Label("RCON password")
                            yield Input(id="password", password=True, placeholder="from dedicated.toml")
                        with Vertical(classes="setup-wide setup-col2"):
                            yield Label("Name (optional)")
                            yield Input(id="name", placeholder="else the server's own")
                        if self.ask_by:
                            with Vertical(classes="setup-wide setup-col2"):
                                yield Label("Your name")
                                yield Input(self.by, id="by", tooltip="Kicks and bans you make are logged under it.")
                    with Collapsible(title="Advanced: reach it through SSH", id="setup-ssh"):
                        yield Input(id="ssh", placeholder="user@game-box   (leave empty to connect directly)")
                        yield Static("Best when the server is on another machine: RCON isn't encrypted, so keep it on "
                                     "127.0.0.1 there and tunnel in. Needs an SSH key, not a password.",
                                     classes="hint")
                    with Horizontal(classes="setup-row"):
                        yield Checkbox("Remember the password", True, id="remember")
                        yield Static(classes="hint", id="remember-hint")
                    yield Static(id="setup-status")
                    yield Static(id="setup-list")
                    with Horizontal(classes="dialog-buttons"):
                        yield Button("TRY THE DEMO", id="demo")
                        yield Static(classes="setup-gap")
                        yield Button("ADD ANYWAY", id="force", classes="-hidden")
                        yield Button("TEST & ADD", id="add", variant="primary")
                        yield Button("START  ▸", id="start", variant="success")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(ONI)
        self.theme = "oni"
        self.query_one("#setup-form").border_title = "ADD A SERVER"
        self.on_checkbox_changed()
        self.query_one("#password").focus()
        self.paint_list()
        self.on_resize()

    def on_resize(self) -> None:
        self.call_after_refresh(self.fit_emblem)

    def on_checkbox_changed(self, _=None) -> None:
        home, path = str(Path.home()), str(self.path)
        shown = "~" + path[len(home):] if path.startswith(home) else path
        self.query_one("#remember-hint", Static).update(
            f"saved in {shown}" if self.query_one("#remember", Checkbox).value else "you'll be asked for it each start")

    def fit_emblem(self) -> None:
        """The emblem above the form in the rows it leaves over, so the buttons never drop below the fold; failing
        that, beside the form in the columns it leaves over (a 120 x 30 terminal has room for the smallest)."""
        form = self.query_one("#setup-form")
        used = self.query_one("#setup-title").outer_size.height + form.outer_size.height + 4
        used += 5  # room for the status and the server list that appear as servers go in
        top = emblem(self.size.height - used, 40)
        side = Text() if top.plain else emblem(form.outer_size.height, self.size.width - form.outer_size.width - 6)
        for w, art in (("#setup-emblem", top), ("#setup-side", side)):
            self.query_one(w, Static).update(art)
            self.query_one(w).display = bool(art.plain)

    def server(self) -> Server | None:
        v = {k: self.query_one(f"#{k}", Input).value.strip() for k in ("address", "port", "password", "name", "ssh")}
        try:
            s = parse_target(target(v["address"], v["port"]), ssh=v["ssh"], name=v["name"])
        except ValueError:
            self.status(False, "That address doesn't look right. Use an IP or host name, and the port number.")
            return None
        if not v["password"]:
            self.status(False, "Enter the RCON password.")
            self.query_one("#password").focus()
            return None
        s.password = v["password"]
        if any(x.where == s.where for x in self.existing + self.added):
            self.status(False, f"{s.where} is already in your list.")
            return None
        return s

    def status(self, ok: bool | None, text: str) -> None:
        if self.spin:
            self.spin.stop()
            self.spin = None
        glyph, color = {True: ("✓", GREEN), False: ("✗", RED), None: ("", AMBER)}[ok]
        self.query_one("#setup-status", Static).update(Text(f"{glyph} {text}".strip(), color))

    def paint_list(self) -> None:
        t = Text()  # one flowing line: it wraps only once there are many
        for s in self.existing:
            t.append("◉ ", DIM)
            t.append(f"{s.name or s.where}   ", WHITE)
        for s in self.added:
            t.append("◉ ", GREEN)
            t.append(f"{s.name or s.where}   ", WHITE)
        box = self.query_one("#setup-list", Static)
        box.update(Text("YOUR SERVERS   ", CYAN) + t)
        box.display = bool(t)
        self.query_one("#start", Button).disabled = not (self.existing or self.added)

    @on(Input.Submitted)
    def _enter(self, e: Input.Submitted) -> None:
        e.stop()
        self.test_and_add()

    @on(Button.Pressed)
    def _button(self, e: Button.Pressed) -> None:
        e.stop()
        if e.button.id == "add":
            self.test_and_add()
        elif e.button.id == "force" and (s := self.server()):
            self.add(s, "Added without a test. It'll keep trying to connect once you start.")
        elif e.button.id == "demo":
            self.exit("demo")
        elif e.button.id == "start":
            self.start()

    @work(exclusive=True, group="test")
    async def test_and_add(self) -> None:
        s = self.server()
        if not s:
            return
        self.query_one("#force").add_class("-hidden")
        self.query_one("#add", Button).disabled = True
        frame = itertools.count()
        self.spin = self.set_interval(0.1, lambda: self.query_one("#setup-status", Static).update(
            Text(f"{SPIN[next(frame) % 4]} Connecting to {s.where}…", AMBER)))
        try:
            ok, text = await probe(s)
        finally:
            self.query_one("#add", Button).disabled = False
        if ok:
            self.add(s, text)
        else:
            self.status(False, text)
            self.query_one("#force").remove_class("-hidden")

    def add(self, s: Server, text: str) -> None:
        self.added.append(s)
        self.status(True, f"{text} Add another (next port filled in), or press START.")
        self.query_one("#force").add_class("-hidden")
        if s.port:
            self.query_one("#port", Input).value = str(s.port + 1)
        self.query_one("#name", Input).value = ""
        self.paint_list()
        self.query_one("#start").focus()

    def start(self) -> None:
        if self.added:
            by = self.query_one("#by", Input).value.strip() if self.ask_by else ""
            try:
                add_servers(self.path, self.added, by=by, remember=self.query_one("#remember", Checkbox).value)
            except OSError as err:
                self.status(False, f"Couldn't save {self.path}: {err}")
                return
            self.by = by or self.by
        self.exit(self.added)


def setup(path: Path, existing: list[Server], by: str, ask_by: bool) -> tuple[list[Server] | str | None, str]:
    """Run the setup screen; returns (its result, the operator's name)."""
    app = SetupApp(path, existing, by, ask_by)
    return app.run(), app.by
