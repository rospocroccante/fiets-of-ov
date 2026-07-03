"""Process-wide logging setup.

`Settings.log_level` is only honoured if something actually configures the logging
tree; without this, a bare process (most importantly the ARQ worker under
`make worker`) has no root handler, so app log lines — including the LogNotifier's
rain alerts, the MVP delivery channel — are silently dropped. Both entrypoints
(FastAPI lifespan, ARQ on_startup) call `configure_logging()` exactly once at boot.
"""

import logging

from app.core.config import get_settings


def configure_logging() -> None:
    """Attach a root handler at `settings.log_level`, unless one is already configured.

    Uses `logging.basicConfig`, which is deliberately a no-op when the root logger
    already has handlers. That guard is the point: under uvicorn with `--log-config`
    (or any deployment that pre-configures root logging) we must not stomp that setup,
    while a bare process (the ARQ worker, plain `python -m`) gets a sensible default.
    """
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
