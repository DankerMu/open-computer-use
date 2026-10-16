# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Lifespan-owned Office transactions independent of DNS and callback workers."""
from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, ParamSpec, TypeVar

import anyio

_Parameters = ParamSpec("_Parameters")
_Result = TypeVar("_Result")


async def run_transaction_worker(
    app: Any,
    operation: Callable[_Parameters, _Result],
    *args: _Parameters.args,
    **kwargs: _Parameters.kwargs,
) -> _Result:
    executor = getattr(app.state, "office_transaction_executor", None)
    if executor is None:
        raise RuntimeError("Office transaction worker ownership is unavailable")
    context = contextvars.copy_context()
    submitted = executor.submit(context.run, operation, *args, **kwargs)
    # Unshielded cancellation can stop queued work, but cannot abandon a running transaction.
    return await asyncio.wrap_future(submitted)


@asynccontextmanager
async def transaction_worker_lifespan(app: Any) -> AsyncIterator[None]:
    loop = asyncio.get_running_loop()
    completed: asyncio.Future[None] = loop.create_future()
    stop = threading.Event()
    executor = ThreadPoolExecutor()

    def finish(failure: BaseException | None) -> None:
        if failure is None:
            completed.set_result(None)
        else:
            completed.set_exception(failure)

    def shutdown() -> None:
        stop.wait()
        failure = None
        try:
            executor.shutdown(wait=True, cancel_futures=False)
        except BaseException as extra:
            failure = extra
        # Notification is last: joining this supervisor never waits on loop-owned work.
        loop.call_soon_threadsafe(finish, failure)

    cancellation = None
    supervisor = None
    try:
        supervisor = threading.Thread(target=shutdown, name="office-transaction-shutdown")
        supervisor.start()
        app.state.office_transaction_executor = executor
        try:
            yield
        except asyncio.CancelledError as extra:
            cancellation = extra
    finally:
        try:
            app.state.office_transaction_executor = None
        finally:
            if supervisor is None or supervisor.ident is None:
                # A genuinely unstarted supervisor owns no jobs and must not be joined.
                executor.shutdown(wait=True, cancel_futures=False)
            else:
                stop.set()
                # AnyIO shielding handles enclosing scopes; retained future shielding also
                # handles repeated direct Task.cancel() without cancelling shutdown completion.
                with anyio.CancelScope(shield=True):
                    while not completed.done():
                        try:
                            await asyncio.shield(completed)
                        except asyncio.CancelledError as extra:
                            if cancellation is None:
                                cancellation = extra
                        except BaseException:
                            break
                    supervisor.join()
                    shutdown_failure = completed.exception()
                if cancellation is not None:
                    raise cancellation
                # Observe scope cancellation requested while cleanup was shielded.
                await anyio.lowlevel.checkpoint()
                if shutdown_failure is not None:
                    raise shutdown_failure
