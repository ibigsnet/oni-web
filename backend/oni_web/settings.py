"""Deployment settings, deliberately separate from browser-supplied connection data."""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


def loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = Path("data")
    token: str = field(default="", repr=False)
    encryption_key: str = field(default="", repr=False)
    origins: tuple[str, ...] = ("http://127.0.0.1:8000", "http://localhost:8000",
                                "http://127.0.0.1:5173", "http://localhost:5173")
    secure_cookie: bool = False
    commands_per_minute: int = 180
    max_inflight: int = 16
    allow_demo: bool = True
    config_path: str = ""
    native_helper: str = ""

    def __post_init__(self):
        if not loopback(self.host) and len(self.token) < 24:
            raise ValueError("Set ONI_WEB_TOKEN to at least 24 characters before binding outside loopback")
        if self.token and len(self.token) < 24:
            raise ValueError("ONI_WEB_TOKEN must contain at least 24 characters")
        if not 1 <= self.port <= 65535 or not 1 <= self.commands_per_minute <= 10000:
            raise ValueError("Invalid port or command rate")
        if not 1 <= self.max_inflight <= 128:
            raise ValueError("ONI_WEB_MAX_INFLIGHT must be between 1 and 128")
        if not self.origins or any(urlsplit(x).scheme not in ("http", "https")
                                   or not urlsplit(x).hostname or urlsplit(x).path
                                   or urlsplit(x).username for x in self.origins):
            raise ValueError("ONI_WEB_ORIGINS must be exact http(s) origins without paths or credentials")
        if not self.token and any(not loopback(urlsplit(x).hostname) for x in self.origins):
            raise ValueError("Set ONI_WEB_TOKEN for non-loopback browser origins, including reverse proxies")

    @classmethod
    def from_env(cls):
        load_dotenv()
        defaults = cls()
        return cls(
            host=os.getenv("ONI_WEB_HOST", defaults.host),
            port=int(os.getenv("ONI_WEB_PORT", "8000")),
            data_dir=Path(os.getenv("ONI_WEB_DATA_DIR", "data")).resolve(),
            token=os.getenv("ONI_WEB_TOKEN", ""),
            encryption_key=os.getenv("ONI_WEB_ENCRYPTION_KEY", ""),
            origins=tuple(x.strip() for x in os.getenv("ONI_WEB_ORIGINS", ",".join(defaults.origins)).split(",") if x.strip()),
            secure_cookie=os.getenv("ONI_WEB_SECURE_COOKIE", "false").lower() == "true",
            commands_per_minute=int(os.getenv("ONI_WEB_COMMANDS_PER_MINUTE", "180")),
            max_inflight=int(os.getenv("ONI_WEB_MAX_INFLIGHT", "16")),
            allow_demo=os.getenv("ONI_WEB_ALLOW_DEMO", "true").lower() == "true",
            config_path=os.getenv("ONI_RCON_CONFIG", ""),
            native_helper=os.getenv("ONI_WEB_NATIVE_HELPER", ""),
        )
