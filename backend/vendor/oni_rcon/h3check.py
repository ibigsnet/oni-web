"""Is this file strictly Halo 3 content? (IBIGS 2026-10-17)

Every file that reaches a server from outside (ReclaimerForge downloads, Steam Workshop mods, customer uploads) is
checked here before it is written where the game reads it. Standard library only, so the same file is used by
oni-rcon, oni-web and the Upload-folder sorter.

  check(name, data, kind)  ->  what it is ("forge map", "mod sound bank", ...) or raises Rejected(reason)

kind:
  "forge"  ReclaimerForge assets: map / game type / playlist JSON in Reclaimer's known shapes, or Halo `_blf`
           binaries (.mvar, .bin). 2 MB each.
  "mod"    one file of a Workshop/MCC mod folder: Halo cache maps, FMOD banks, Halo tag files, `_blf` files,
           images, mod JSON and small text, each by its own signature (the bytes must match the extension).
  "game"   Halo 3 server files: data as for mods; Windows program files (.dll/.exe/...) only when they are
           byte-for-byte the official file already installed at that path (pass `official`).

Whatever the kind: anything that looks like a program, script, shortcut, installer or archive is refused, as is
any extension not listed here. A refusal says why, in plain words.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from pathlib import PurePosixPath


class Rejected(Exception):
    """Not Halo 3 content (the message says why)."""


MB = 1024 * 1024
FORGE_MAX = 2 * MB
H3_MAP_VARIANT = 57504

# Programs, scripts, shortcuts, installers, archives: never Halo content, whatever the file is called.
DANGEROUS_EXT = {
    ".exe", ".dll", ".sys", ".scr", ".com", ".cpl", ".ocx", ".drv", ".efi", ".msi", ".msp", ".msix", ".appx",
    ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe", ".js", ".jse", ".mjs", ".wsf", ".wsh", ".hta",
    ".lnk", ".url", ".pif", ".reg", ".inf", ".chm", ".jar", ".class", ".py", ".pyc", ".pyw", ".pl", ".rb",
    ".sh", ".bash", ".zsh", ".fish", ".command", ".so", ".dylib", ".appimage", ".deb", ".rpm", ".apk",
    ".desktop", ".service", ".zip", ".7z", ".rar", ".gz", ".tgz", ".xz", ".bz2", ".zst", ".tar", ".cab",
    ".iso", ".img", ".vhd", ".vhdx", ".html", ".htm", ".svg", ".php", ".asp", ".aspx", ".scpt", ".docm",
    ".xlsm", ".pptm",
}
# Programs that are part of the official game: only accepted for kind="game", and only if identical.
PROGRAM_EXT = {".exe", ".dll", ".sys", ".ocx", ".drv"}
MAGIC_PROGRAM = (
    (b"MZ", "a Windows program"), (b"\x7fELF", "a Linux program"), (b"\xcf\xfa\xed\xfe", "a macOS program"),
    (b"\xce\xfa\xed\xfe", "a macOS program"), (b"\xca\xfe\xba\xbe", "a macOS/Java program"),
    (b"#!", "a script"), (b"PK\x03\x04", "a zip archive"), (b"PK\x05\x06", "a zip archive"),
    (b"Rar!", "a RAR archive"), (b"7z\xbc\xaf\x27\x1c", "a 7-Zip archive"), (b"\x1f\x8b", "a gzip archive"),
    (b"\xfd7zXZ", "an xz archive"), (b"BZh", "a bzip2 archive"), (b"\x28\xb5\x2f\xfd", "a zstd archive"),
    (b"MSCF", "a cabinet installer"), (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "an Office/installer file"),
    (b"L\x00\x00\x00\x01\x14\x02\x00", "a Windows shortcut"), (b"\x00asm", "a WebAssembly program"),
    (b"%PDF", "a PDF"),
)
SCRIPT_TEXT = re.compile(rb"^\s*(<\?php|<script|<html|<!doctype|@echo|powershell|cmd(\.exe)?\s|#!|import\s|function\s"
                         rb"|var\s|const\s|set\s+\w+=|start\s|reg\s+add|wscript|cscript|rundll32|mshta)", re.I)


def _sniff_dangerous(data: bytes) -> str | None:
    for magic, what in MAGIC_PROGRAM:
        if data.startswith(magic):
            return what
    # a UTF-16/UTF-8 BOM in front of a script is still a script
    head = data[:256].lstrip(b"\xef\xbb\xbf\xff\xfe\xfe\xff")
    if SCRIPT_TEXT.match(head.replace(b"\x00", b"")):
        return "a script"
    return None


def _json(data: bytes, limit: int, what: str) -> dict:
    if len(data) > limit:
        raise Rejected(f"{what} is {len(data) // 1024} KB; the limit is {limit // 1024} KB")
    if b"\x00" in data:
        raise Rejected(f"{what} isn't text (it has NUL bytes)")
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise Rejected(f"{what} isn't valid JSON") from None
    if not isinstance(doc, dict):
        raise Rejected(f"{what} isn't a JSON object")
    depth, stack = 0, [(doc, 1)]
    while stack:
        node, d = stack.pop()
        depth = max(depth, d)
        if depth > 64:
            raise Rejected(f"{what} is nested too deeply to be game data")
        if isinstance(node, dict):
            stack.extend((v, d + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, d + 1) for v in node)
    return doc


FORGE_GAME_KEYS = {"version", "name", "author", "base_map", "hosting", "map", "mode", "rules", "options", "world",
                   "packed_game", "script"}
PLAYLIST_KEYS = {"version", "name", "vote_seconds", "games"}
PLAYLIST_GAME_KEYS = {"map", "game", "world", "options", "weight", "name", "rules"}


def _forge_json(doc: dict) -> str:
    keys = set(doc)
    if isinstance(doc.get("games"), list):
        if keys - PLAYLIST_KEYS or doc.get("version") != 2:
            raise Rejected("a playlist with fields Reclaimer doesn't use: " + ", ".join(sorted(keys - PLAYLIST_KEYS)))
        if not 1 <= len(doc["games"]) <= 256:
            raise Rejected("a playlist needs 1 to 256 games")
        for g in doc["games"]:
            if not isinstance(g, dict) or set(g) - PLAYLIST_GAME_KEYS:
                raise Rejected("a playlist game with fields Reclaimer doesn't use")
            for k in ("map", "game"):
                ref = g.get(k)
                if not isinstance(ref, str) or not ref or len(ref) > 300 or ".." in ref.replace("\\", "/").split("/") \
                        or ref.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", ref):
                    raise Rejected(f"a playlist game names an unsafe {k}: {str(ref)[:80]!r}")
        return "forge playlist"
    if keys - FORGE_GAME_KEYS:
        raise Rejected("a Forge file with fields Reclaimer doesn't use: " + ", ".join(sorted(keys - FORGE_GAME_KEYS)))
    if not isinstance(doc.get("version"), int) or not isinstance(doc.get("base_map"), dict):
        raise Rejected("not a Reclaimer map or game type export (no version / base map)")
    base = doc["base_map"]
    if not re.fullmatch(r"[A-Za-z0-9_ .'-]{1,100}", str(base.get("stem", ""))):
        raise Rejected("its base map name isn't a Halo 3 map name")
    m = doc.get("map")
    if isinstance(m, dict) and "data" in m:
        if not isinstance(m["data"], str):
            raise Rejected("its map data isn't encoded map data")
        try:
            raw = base64.b64decode(m["data"], validate=True)
        except (binascii.Error, ValueError):
            raise Rejected("its map data isn't valid base64") from None
        if not 64 <= len(raw) <= FORGE_MAX:
            raise Rejected("its map data has an impossible size")
        # Halo 3's native sandbox map variant is a fixed 57,504-byte block (every ReclaimerForge map); a _blf
        # container is the other form the game reads.
        if len(raw) != H3_MAP_VARIANT and not raw.startswith(b"_blf"):
            what = _sniff_dangerous(raw)
            raise Rejected(f"its embedded map data is {what}" if what else
                           f"its map data isn't a Halo 3 map variant ({len(raw)} bytes, not {H3_MAP_VARIANT})")
    pg = doc.get("packed_game")
    if pg is not None:
        if isinstance(pg, dict):
            if set(pg) - {"data", "format", "version", "size"}:
                raise Rejected("its packed game has fields Reclaimer doesn't use")
            pg = pg.get("data")
        if not isinstance(pg, str):
            raise Rejected("its packed game isn't encoded game data")
        try:
            raw = base64.b64decode(pg, validate=True)
        except (binascii.Error, ValueError):
            raise Rejected("its packed game isn't valid base64") from None
        if not raw.startswith(b"_blf") or len(raw) > FORGE_MAX:
            raise Rejected("its packed game isn't a Halo 3 game variant (_blf)")
    if "script" in doc and not isinstance(doc["script"], dict):
        raise Rejected("its game script isn't Reclaimer's script format")
    return "forge game type" if ("packed_game" in doc or "script" in doc or "options" in doc) else "forge map"


IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8", b"BM", b"DDS ")
TEXT_EXT = {".txt", ".csv", ".md", ".ini", ".cfg", ".log"}
# Opaque Halo/MCC data seen in real Workshop mods; accepted when nothing in them looks like a program.
MOD_DATA_EXT = {".bin", ".info", ".probestore", ".compression_data", ".tmp", ".dat", ".mapinfo", ".strings"}
MOD_LIMITS = {".map": 4096 * MB, ".fsb": 2048 * MB, ".psd": 512 * MB}


def check(name: str, data: bytes, kind: str = "mod", official: dict[str, str] | None = None) -> str:
    """See the module docstring. `data` may be only the file's first megabytes for very large files plus its
    size via `size=`; use check_path() for files on disk."""
    return _check(name, data, len(data), kind, official)


def check_path(path, rel: str | None = None, kind: str = "mod", official: dict[str, str] | None = None) -> str:
    """check() for a file on disk, reading only what is needed (head for big binaries, all of small text/JSON)."""
    from pathlib import Path
    p = Path(path)
    size = p.stat().st_size
    ext = p.suffix.lower()
    want_all = ext in {".json"} | TEXT_EXT or (kind == "forge") or ext in PROGRAM_EXT or size <= 4 * MB
    with open(p, "rb") as fh:
        data = fh.read(size if want_all else 4 * MB)
    if kind == "game" and ext in PROGRAM_EXT:
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(4 * MB), b""):
                h.update(chunk)
        return _program(rel or p.name, h.hexdigest(), official)
    return _check(rel or p.name, data, size, kind, official)


def _program(rel: str, sha: str, official: dict[str, str] | None) -> str:
    want = (official or {}).get(rel.replace("\\", "/").lower())
    if not want:
        raise Rejected(f"{rel} is a program file that isn't part of the installed Halo 3 server files")
    if want != sha:
        raise Rejected(f"{rel} is a program file that differs from the official one already installed")
    return "official game program"


def _check(name: str, data: bytes, size: int, kind: str, official) -> str:
    base = PurePosixPath(str(name).replace("\\", "/")).name
    ext = PurePosixPath(base.lower()).suffix
    if not base or base.startswith(".") and kind != "game":
        raise Rejected(f"{name!r} is a hidden file")
    if kind == "game" and ext in PROGRAM_EXT:
        return _program(name, hashlib.sha256(data).hexdigest(), official)
    if ext in DANGEROUS_EXT:
        raise Rejected(f"{base} is a {ext} file: programs, scripts, shortcuts and archives are never Halo content")
    what = _sniff_dangerous(data)
    if what and not (ext in TEXT_EXT and what == "a script" and kind != "forge"):
        raise Rejected(f"{base} is really {what}, not Halo 3 content")

    if kind == "forge":
        if size > FORGE_MAX:
            raise Rejected(f"{base} is {size // 1024} KB; Forge files are at most {FORGE_MAX // MB} MB")
        if ext == ".json":
            return _forge_json(_json(data, FORGE_MAX, base))
        if ext in (".mvar", ".bin"):
            if not data.startswith(b"_blf"):
                raise Rejected(f"{base} isn't a Halo 3 variant file (no _blf header)")
            return "forge variant"
        raise Rejected(f"{base}: Forge content is only .json, .mvar or .bin")

    # mods, uploads and game data
    if ext == ".map":
        if not data.startswith((b"daeh", b"head")):
            raise Rejected(f"{base} isn't a Halo cache map (no map header)")
        return "halo map"
    if ext == ".fsb":
        if not data.startswith((b"FSB5", b"FSB4")):
            raise Rejected(f"{base} isn't an FMOD sound bank")
        return "sound bank"
    if ext in (".mvar", ".halo3_mapinfo", ".halo3_campaign", ".blf", ".campaign", ".mapinfo") and data[:4] == b"_blf" \
            or ext in (".mvar", ".halo3_mapinfo", ".halo3_campaign", ".blf"):
        if not data.startswith(b"_blf"):
            raise Rejected(f"{base} isn't a Halo _blf file")
        return "halo blf"
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".dds", ".webp"):
        if not (data.startswith(IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")):
            raise Rejected(f"{base} isn't an image")
        if size > 64 * MB:
            raise Rejected(f"{base} is too big for an image")
        return "image"
    if ext == ".psd":
        if not data.startswith(b"8BPS"):
            raise Rejected(f"{base} isn't a Photoshop image")
        return "image"
    if ext == ".json":
        _json(data, 16 * MB, base)
        return "mod json"
    if ext in TEXT_EXT:
        if size > 4 * MB or b"\x00" in data:
            raise Rejected(f"{base} isn't a small text file")
        return "text"
    if len(data) >= 64 and data[60:64] in (b"MALB", b"BLAM") and re.fullmatch(r"\.[a-z0-9_]{2,60}", ext or ""):
        return "halo tag"
    if ext in MOD_DATA_EXT or (kind == "game" and ext in {".dat", ".bk2", ".bik", ".wem", ".pck", ".ini", ".cfg", ".xps", ".campaign"}):
        if size > MOD_LIMITS.get(ext, 512 * MB):
            raise Rejected(f"{base} is too big")
        return "halo data"
    raise Rejected(f"{base}: {ext or 'files without an extension'} isn't a Halo 3 file type this server accepts")


def official_hashes(game_dir) -> dict[str, str]:
    """{relative path (lower case): sha256} of the program files already installed in a Halo 3 server folder."""
    from pathlib import Path
    out = {}
    root = Path(game_dir)
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink() and p.suffix.lower() in PROGRAM_EXT:
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(4 * MB), b""):
                    h.update(chunk)
            out[p.relative_to(root).as_posix().lower()] = h.hexdigest()
    return out
