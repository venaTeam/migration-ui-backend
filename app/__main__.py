"""`python -m app`: serve on UI_BACKEND_PORT."""

from __future__ import annotations

from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from app.config import Settings


def main() -> None:
    load_dotenv(Path.cwd() / ".env")
    settings = Settings.from_env()
    # One worker: sessions live in this process, and only one process may use
    # the registry file.
    uvicorn.run("app.main:create_app", factory=True, host="0.0.0.0", port=settings.port, workers=1)


if __name__ == "__main__":
    main()
