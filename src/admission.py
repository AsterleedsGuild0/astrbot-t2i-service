import asyncio

from fastapi.responses import JSONResponse

from .diagnostics import RequestDiagnostics, current_diagnostics, log_rejection
from .limits import LIMITS, ResourceLimitError, check_runtime_resources


class RenderAdmissionMiddleware:
    """Bound pending requests before FastAPI parses or renders their bodies."""

    def __init__(self, app):
        self.app = app
        self._slot = asyncio.Semaphore(1)
        self._pending = 0

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path", "").rstrip("/") != "/text2img/generate"
        ):
            return await self.app(scope, receive, send)

        diagnostics = RequestDiagnostics(request_bytes=0)
        token = current_diagnostics.set(diagnostics)
        try:
            async def reject(status: int, message: str, stage: str, *, rejection=None):
                if rejection is not None:
                    log_rejection(**{"status": status, "stage": stage, **rejection})
                response = JSONResponse(
                    status_code=status,
                    content={"code": 1, "message": message, "data": {"stage": stage}},
                    headers={"Retry-After": "5"} if status == 503 else None,
                )
                await response(scope, receive, send)

            headers = dict(scope.get("headers", []))
            try:
                raw_length = headers.get(b"content-length")
                length = int(raw_length) if raw_length is not None else None
            except ValueError:
                return await reject(400, "Invalid Content-Length", "request_body")
            diagnostics.content_length = length
            if length is not None and (length < 0 or length > LIMITS.max_request_bytes):
                return await reject(
                    413, "Request exceeds T2I_MAX_REQUEST_BYTES", "request_body",
                    rejection={
                        "category": "request_body", "limit_name": "T2I_MAX_REQUEST_BYTES",
                        "limit_value": LIMITS.max_request_bytes, "observed": length,
                    },
                )
            if self._pending >= LIMITS.max_queue_size + 1:
                return await reject(
                    503, "Render queue is full", "queue",
                    rejection={
                        "category": "queue_full", "limit_name": "T2I_MAX_QUEUE_SIZE",
                        "limit_value": LIMITS.max_queue_size, "observed": self._pending,
                    },
                )

            self._pending += 1
            acquired = False
            try:
                try:
                    await asyncio.wait_for(self._slot.acquire(), LIMITS.queue_timeout)
                    acquired = True
                except TimeoutError:
                    return await reject(
                        503, "Timed out waiting for the render slot", "queue",
                        rejection={
                            "category": "queue_timeout",
                            "limit_name": "T2I_QUEUE_TIMEOUT_SECONDS",
                            "limit_value": LIMITS.queue_timeout,
                        },
                    )

                try:
                    check_runtime_resources()
                except ResourceLimitError as error:
                    return await reject(
                        error.status_code, str(error), error.stage,
                        rejection={**error.rejection, "stage": "admission"},
                    )

                # Buffer only the admitted body, with a byte limit even for chunked uploads.
                body = bytearray()
                size = 0
                try:
                    async with asyncio.timeout(LIMITS.body_timeout):
                        while True:
                            message = await receive()
                            if message["type"] == "http.disconnect":
                                return
                            if message["type"] != "http.request":
                                continue
                            size += len(message.get("body", b""))
                            diagnostics.request_bytes = size
                            if size > LIMITS.max_request_bytes:
                                return await reject(
                                    413, "Request exceeds T2I_MAX_REQUEST_BYTES", "request_body",
                                    rejection={
                                        "category": "request_body",
                                        "limit_name": "T2I_MAX_REQUEST_BYTES",
                                        "limit_value": LIMITS.max_request_bytes,
                                        "observed": size,
                                    },
                                )
                            body.extend(message.get("body", b""))
                            if not message.get("more_body", False):
                                break
                except TimeoutError:
                    return await reject(
                        408, "Timed out reading the request body", "request_body",
                        rejection={
                            "category": "request_body_timeout",
                            "limit_name": "T2I_BODY_TIMEOUT_SECONDS",
                            "limit_value": LIMITS.body_timeout,
                        },
                    )

                body = bytes(body)
                delivered = False

                async def replay_receive():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await receive()

                return await self.app(scope, replay_receive, send)
            finally:
                if acquired:
                    self._slot.release()
                self._pending -= 1
        finally:
            current_diagnostics.reset(token)
