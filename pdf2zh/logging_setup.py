"""Server runtime log file setup — shared by the CLI and the API service.

The service currently logs to the console (or nowhere when the process has no
root handler); this helper optionally mirrors every record into a rotating
file so a GUI/web deployment can keep a persistent server log on disk.
Configurable via the ``PDF2ZH_LOG_FILE`` env var or an explicit ``--log-file``
argument (``resolve_log_file`` merges both, CLI wins).

Idempotent: only one file handler is ever attached per process.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional

#: one process-level file handler (avoid double attach across entry points).
_FILE_HANDLER: Optional[RotatingFileHandler] = None

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_MAX_BYTES = 20 * 1024 * 1024
_BACKUP_COUNT = 3


def resolve_log_file(cli_value: str = "") -> Optional[str]:
    """``--log-file`` wins over the ``PDF2ZH_LOG_FILE`` env var."""
    value = (cli_value or "").strip()
    if value:
        return os.path.abspath(value)
    env = (os.environ.get("PDF2ZH_LOG_FILE") or "").strip()
    return os.path.abspath(env) if env else None


def install_log_file(path: str, level: int = logging.INFO) -> bool:
    """Mirror root-logger records into a rotating file.  No-op when already
    installed or the path cannot be opened.  Returns True when active."""
    global _FILE_HANDLER
    if _FILE_HANDLER is not None:
        return True
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(_FORMAT))
        root = logging.getLogger()
        if root.level == logging.NOTSET or root.level > level:
            root.setLevel(level)
        root.addHandler(handler)
        _FILE_HANDLER = handler
        logging.getLogger(__name__).info("server log file: %s", os.path.abspath(path))
        return True
    except (OSError, ValueError):  # noqa: BLE001 -- logging must never crash boot
        return False
