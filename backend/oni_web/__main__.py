from __future__ import annotations

import sys
from pathlib import Path

# The vendored package stays importable without installing or accessing the sibling repository.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))

from .settings import Settings
from .app import create_app


def main():
    import uvicorn
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1, access_log=False,
                proxy_headers=False, ws_max_size=2_000_000)


if __name__ == "__main__":
    main()
