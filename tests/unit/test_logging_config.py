"""Tests for `app.core.logging.configure_logging`.

The function is a guarded `logging.basicConfig`: it must give an unconfigured process
(the ARQ worker case) a working root handler at the configured level, and must leave a
pre-configured process (uvicorn with its own log config) untouched. Both branches are
exercised against the real root logger, with its state saved and restored so the tests
don't leak into pytest's own log capture.
"""

import logging
from collections.abc import Iterator

import pytest

from app.core.logging import configure_logging


@pytest.fixture
def restore_root_logger() -> Iterator[logging.Logger]:
    """Yield the root logger, restoring its handlers and level afterwards.

    pytest's capture plugin installs its own root handlers; the tests below clear or
    extend them to control their preconditions, so everything is put back on exit.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        yield root
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_sets_root_level_and_handler_when_unconfigured(
    restore_root_logger: logging.Logger,
) -> None:
    """A bare process (no root handlers) gets a handler at settings.log_level (INFO)."""
    root = restore_root_logger
    root.handlers.clear()  # simulate a process where nothing configured logging
    configure_logging()
    assert root.handlers, "basicConfig should have attached a root handler"
    assert root.level == logging.INFO


def test_noop_when_root_already_has_handlers(restore_root_logger: logging.Logger) -> None:
    """An existing config (e.g. uvicorn's) is not stomped: level and handlers stay put."""
    root = restore_root_logger
    root.handlers.clear()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    root.setLevel(logging.CRITICAL)
    configure_logging()
    assert root.handlers == [sentinel]
    assert root.level == logging.CRITICAL
