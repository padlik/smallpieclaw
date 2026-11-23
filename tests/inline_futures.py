"""Test-only synchronous pool double that behaves like a real executor Future."""

from __future__ import annotations

from concurrent.futures import Future
from typing import Callable, TypeVar

T = TypeVar("T")


def run_inline_future(
    fn: Callable[..., T], *args: object, **kwargs: object
) -> "Future[T]":
    """Run *fn* synchronously and return a completed real ``Future``.

    Mirrors ``ThreadPoolExecutor``: any ``BaseException`` raised by the worker
    is captured in the Future instead of escaping ``submit``. It is synchronous,
    so it cannot prove callback-outside-lock behaviour; use a real executor for
    handoff/race tests.
    """
    future: "Future[T]" = Future()
    if not future.set_running_or_notify_cancel():  # pragma: no cover - fresh Future
        return future
    try:
        result = fn(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - faithful worker capture
        future.set_exception(exc)
    else:
        future.set_result(result)
    return future
