"""Keep cancellation from abandoning disk work or newly acquired session locks."""

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dynaval.services.sessions import SessionStore


async def _finish[T](task: asyncio.Task[T]) -> tuple[T, bool]:
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise


async def durable_call[**P, T](function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Complete already-started I/O before allowing its caller's lock to release."""
    result, cancelled = await _finish(
        asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    )
    if cancelled:
        raise asyncio.CancelledError
    return result


async def acquire_session(operation: Callable[[], "SessionStore"]) -> "SessionStore":
    """Release a store if its opening task is cancelled before receiving it."""
    store, cancelled = await _finish(asyncio.create_task(asyncio.to_thread(operation)))
    if cancelled:
        await _finish(asyncio.create_task(asyncio.to_thread(store.close)))
        raise asyncio.CancelledError
    return store
