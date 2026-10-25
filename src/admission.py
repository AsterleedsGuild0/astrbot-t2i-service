import asyncio

from fastapi.responses import JSONResponse

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

        async def reject(status: int, message: str, stage: str):
            response = JSONResponse(
                status_code=status,
                content={"code": 1, "message": message, "data": {"stage": stage}},
                headers={"Retry-After": "5"} if status == 503 else None,
            )
            await response(scope, receive, send)

        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await reject(400, "Invalid Content-Length", "request_body")
        if length < 0 or length > LIMITS.max_request_bytes:
            return await reject(413, "Request exceeds T2I_MAX_REQUEST_BYTES", "request_body")
        if self._pending >= LIMITS.max_queue_size + 1:
            return await reject(503, "Render queue is full", "queue")

        self._pending += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._slot.acquire(), LIMITS.queue_timeout)
                acquired = True
            except TimeoutError:
                return await reject(503, "Timed out waiting for the render slot", "queue")

            try:
                check_runtime_resources()
            except ResourceLimitError as error:
                return await reject(error.status_code, str(error), error.stage)

            # Buffer only the admitted body, with a byte limit even for chunked uploads.
            messages = []
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
                        if size > LIMITS.max_request_bytes:
                            return await reject(413, "Request exceeds T2I_MAX_REQUEST_BYTES", "request_body")
                        messages.append(message)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                return await reject(408, "Timed out reading the request body", "request_body")

            index = 0

            async def replay_receive():
                nonlocal index
                if index < len(messages):
                    message = messages[index]
                    index += 1
                    return message
                return await receive()

            return await self.app(scope, replay_receive, send)
        finally:
            if acquired:
                self._slot.release()
            self._pending -= 1
