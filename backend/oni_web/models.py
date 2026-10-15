"""Only connection information crosses the browser boundary; executable host settings never do."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    name: str = Field(default="", max_length=100)
    host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    port: int = Field(default=49176, ge=1, le=65535, strict=True)
    url: str = Field(default="", max_length=2048)
    password: str | None = Field(default=None, max_length=4096, repr=False)
    ssh: str = Field(default="", max_length=253)
    remember: bool = True

    @field_validator("password")
    @classmethod
    def password_ok(cls, value):
        if value and len(value) < 8:
            raise ValueError("Project Reclaimer requires an RCON password of at least 8 characters")
        return value

    @field_validator("host")
    @classmethod
    def host_ok(cls, value):
        value = value.strip().strip("[]")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9._-]{0,251}[a-zA-Z0-9])?", value):
                raise ValueError("Use a host name or IP address without a scheme or path")
        return value

    @field_validator("ssh")
    @classmethod
    def ssh_ok(cls, value):
        value = value.strip()
        if value and not re.fullmatch(r"(?:[a-zA-Z0-9_][a-zA-Z0-9_.-]*@)?[a-zA-Z0-9_][a-zA-Z0-9_.-]*", value):
            raise ValueError("Use an SSH host alias or user@host; configure ports and keys in ssh/config")
        return value

    @field_validator("url")
    @classmethod
    def url_ok(cls, value):
        if value:
            u = urlsplit(value)
            if (u.scheme not in ("ws", "wss") or not u.hostname or u.username or u.password
                    or u.fragment or u.query or any(ord(x) < 33 for x in value)):
                raise ValueError("Use a ws:// or wss:// URL without credentials, query or fragment")
            _ = u.port  # validate range
        return value

    @model_validator(mode="after")
    def no_url_tunnel(self):
        if self.url and self.ssh:
            raise ValueError("Choose a WebSocket URL or SSH forwarding")
        return self


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0, le=9999, strict=True)
    command: str = Field(min_length=1, max_length=128, pattern=r"^[^\s\x00-\x1f\x7f]+$")
    args: list[str] = Field(default_factory=list, max_length=128)
    timeout: float | None = Field(default=None, ge=0.1, le=120)

    @field_validator("args")
    @classmethod
    def args_ok(cls, value):
        if sum(len(x.encode()) for x in value) > 65536 or any("\0" in x for x in value):
            raise ValueError("Command arguments exceed 64 KiB or contain NUL")
        return value
