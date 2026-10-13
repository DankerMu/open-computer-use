# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Single-file responses whose metadata and bytes share one owned descriptor."""
from __future__ import annotations

import os
from collections.abc import Mapping

import anyio
from fastapi import HTTPException
from starlette._utils import collapse_excgroups
from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send

from uploads import close_fd, open_chat_upload


class WorkspaceFileResponse(FileResponse):
    def __init__(
        self,
        chat_id: str,
        rel_path: str,
        *,
        requested_filename: str,
        filename: str | None = None,
        media_type: str | None = None,
        headers: Mapping[str, str] | None = None,
        content_disposition_type: str = "attachment",
    ) -> None:
        super().__init__(
            path=rel_path,
            filename=filename,
            media_type=media_type,
            headers=headers,
            content_disposition_type=content_disposition_type,
        )
        self.chat_id = chat_id
        self.rel_path = rel_path
        self.requested_filename = requested_filename

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Native caller cancellation becomes scope cancellation of this child;
        # group exit joins its non-abandoned setup workers before cleanup.
        with collapse_excgroups():
            async with anyio.create_task_group() as owner:
                owner.start_soon(self._run_response, scope, receive, send)

    async def _run_response(self, scope: Scope, receive: Receive, send: Send) -> None:
        file_fd = None
        try:
            try:
                # The owned child retains the worker's result through scope
                # cancellation; the finally also owns setup failures.
                file_fd = await anyio.to_thread.run_sync(
                    open_chat_upload, self.chat_id, self.rel_path
                )
            except FileNotFoundError as exc:
                raise HTTPException(
                    status_code=404,
                    detail=f"File not found: {self.requested_filename}",
                ) from exc
            file_stat = await anyio.to_thread.run_sync(os.fstat, file_fd)
            self.set_stat_headers(file_stat)

            async with anyio.create_task_group() as task_group:
                async def stream() -> None:
                    try:
                        await anyio.lowlevel.checkpoint()
                        await send({
                            "type": "http.response.start",
                            "status": self.status_code,
                            "headers": self.raw_headers,
                        })
                        if scope["method"] == "HEAD":
                            await send({
                                "type": "http.response.body",
                                "body": b"",
                                "more_body": False,
                            })
                            return
                        while True:
                            chunk = await anyio.to_thread.run_sync(os.read, file_fd, self.chunk_size)
                            # A disconnect/cancellation during a shielded read must
                            # take effect before its completed chunk is published.
                            await anyio.lowlevel.checkpoint()
                            more_body = len(chunk) == self.chunk_size
                            await send({
                                "type": "http.response.body",
                                "body": chunk,
                                "more_body": more_body,
                            })
                            if not more_body:
                                break
                    finally:
                        task_group.cancel_scope.cancel()

                task_group.start_soon(stream)
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        task_group.cancel_scope.cancel()
                        break
                    await anyio.lowlevel.checkpoint()
        finally:
            # Non-abandoned setup workers finish before this child reaches cleanup;
            # the inner group also joins reads before releasing the sole FD owner.
            close_fd(file_fd)
