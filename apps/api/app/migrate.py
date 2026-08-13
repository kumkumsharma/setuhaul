"""Production-safe schema migration entrypoint (Alembic upgrade only)."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config


def main() -> int:
    api_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(api_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_root / "alembic"))
    command.upgrade(cfg, "head")
    print("Schema migrations applied (Alembic upgrade head).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
