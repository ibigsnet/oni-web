"""Authenticated encryption and atomic persistence. A corrupt vault fails closed; it is never reset."""
from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def private_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class Vault:
    def __init__(self, directory: Path, key: str = ""):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "vault.enc"
        key_path = directory / "vault.key"
        if not key:
            if key_path.exists():
                key = key_path.read_text().strip()
            elif self.path.exists():
                raise RuntimeError("Vault key is missing. Restore vault.key or set ONI_WEB_ENCRYPTION_KEY")
            else:
                # Exclusive creation prevents concurrent first starts from generating different keys.
                try:
                    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as f:
                        f.write(Fernet.generate_key())
                except FileExistsError:
                    pass
                key = key_path.read_text().strip()
        self.cipher = Fernet(key.encode())
        self.data = {"schema": 1, "profiles": [], "by": "operator", "rules": [], "roles": {},
                     "webhooks": {}, "forge_key": "", "prefs": {}, "trusted": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.cipher.decrypt(self.path.read_bytes()))
                if not isinstance(loaded, dict) or loaded.get("schema") != 1 or not isinstance(loaded.get("profiles"), list):
                    raise ValueError("Unknown vault schema")
                self.data.update(loaded)
            except (InvalidToken, ValueError) as e:
                raise RuntimeError("Cannot decrypt/read vault. Check the encryption key; existing data was preserved") from e

    def commit(self, data: dict):
        # Commit disk first: failures leave both the previous memory state and file intact.
        private_write(self.path, self.cipher.encrypt(json.dumps(data, ensure_ascii=False).encode()))
        self.data = copy.deepcopy(data)

    def update(self, **changes):
        self.commit({**self.data, **changes})
