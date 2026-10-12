"""Run the server: ``uv run python -m app`` (add ``--reload`` for development).

Always a single uvicorn worker process: the research worker inside it is the
only executor, and the advisory lock keeps any extra process in standby.
Forwarded/proxy headers are not trusted by uvicorn; configure TRUSTED_PROXIES
for the application if you deploy behind a reverse proxy.
"""

from __future__ import annotations

import sys

import uvicorn

from app.core.config import get_settings


def main() -> None:
    settings = get_settings()
    reload = "--reload" in sys.argv[1:]
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,
        reload=reload,
        reload_dirs=["app"] if reload else None,
        log_config=None,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    main()
