import asyncio
import os
import secrets
import time
from collections import deque
from contextlib import suppress
from pathlib import Path

import fastapi
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from jinja2.exceptions import SecurityError
from loguru import logger
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .admission import RenderAdmissionMiddleware
from .limits import LIMITS, ResourceLimitError, check_html_size, check_runtime_resources
from .metrics import (
    CLEANUP_DURATION,
    CLEANUP_FILES,
    CLEANUP_RUNS,
    HTTP_REQUEST_DURATION,
    HTTP_REQUESTS,
    HTTP_REQUESTS_IN_PROGRESS,
    IMAGE_STORAGE_DURATION,
    IMAGE_STORAGE_OPERATIONS,
    METRICS_ENABLED,
    RATE_LIMIT_REJECTIONS,
    RENDER_INPUT_BYTES,
)
from .render import RenderError, ScreenshotOptions, Text2ImgRender
from .storage import create_image_storage
from .util import cleanup_expired_files

app = fastapi.FastAPI()
app.add_middleware(RenderAdmissionMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
render = Text2ImgRender()
image_storage = create_image_storage()
rate_limit_lock = asyncio.Lock()
rate_limit_timestamps: deque[float] = deque()
rate_limit_max_requests = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "0"))
rate_limit_window_seconds = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "0"))


def metric_route(path: str) -> str:
    if path.startswith("/text2img/data/"):
        return "/text2img/data/{id}"
    if path in {
        "/text2img/generate",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
    }:
        return path
    return "unmatched"


@app.middleware("http")
async def observe_http_requests(request: fastapi.Request, call_next):
    if not METRICS_ENABLED:
        return await call_next(request)

    method = request.method.upper()
    route_path = metric_route(request.url.path)
    started = time.perf_counter()
    status_code = 500
    HTTP_REQUESTS_IN_PROGRESS.labels(method=method).inc()
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        HTTP_REQUESTS_IN_PROGRESS.labels(method=method).dec()
        if route_path != "/metrics":
            HTTP_REQUESTS.labels(
                method=method,
                route=route_path,
                status_code=str(status_code),
            ).inc()
            HTTP_REQUEST_DURATION.labels(method=method, route=route_path).observe(
                time.perf_counter() - started
            )


class GenerateRequest(BaseModel):
    html: str | None = None
    tmpl: str | None = None
    tmplname: str | None = None
    tmpldata: dict | None = None
    options: ScreenshotOptions | None = None
    json: bool = False


# 启动时创建清理任务
@app.on_event("startup")
async def startup_event():
    """应用启动时的事件处理"""
    # 启动定期清理任务
    app.state.cleanup_task = asyncio.create_task(periodic_cleanup())
    logger.info("Started periodic cleanup task")


@app.on_event("shutdown")
async def shutdown_event():
    cleanup_task = getattr(app.state, "cleanup_task", None)
    if cleanup_task is not None:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
    await render.terminate()


@app.get("/healthz", include_in_schema=False)
async def health_check():
    return JSONResponse(
        status_code=503 if render._unhealthy else 200,
        content={"status": "unhealthy" if render._unhealthy else "ok"},
    )


async def periodic_cleanup():
    """定期清理过期文件的后台任务"""
    while True:
        started = time.perf_counter()
        try:
            cleaned = await asyncio.to_thread(cleanup_expired_files)
            CLEANUP_FILES.inc(cleaned)
            CLEANUP_RUNS.labels(result="success").inc()
        except Exception:
            CLEANUP_RUNS.labels(result="error").inc()
            logger.exception("Error during periodic cleanup")
        finally:
            CLEANUP_DURATION.observe(time.perf_counter() - started)
        # 每小时执行一次清理
        await asyncio.sleep(3600)


async def enforce_rate_limit() -> int | None:
    if rate_limit_max_requests <= 0 or rate_limit_window_seconds <= 0:
        return None
    now = time.monotonic()
    async with rate_limit_lock:
        while (
            rate_limit_timestamps
            and now - rate_limit_timestamps[0] >= rate_limit_window_seconds
        ):
            rate_limit_timestamps.popleft()
        if len(rate_limit_timestamps) >= rate_limit_max_requests:
            retry_after = rate_limit_window_seconds - (now - rate_limit_timestamps[0])
            return max(0, int(retry_after) + 1)
        rate_limit_timestamps.append(now)
    return None


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: fastapi.Request):
    if not METRICS_ENABLED:
        return Response(status_code=404)

    metrics_token = os.getenv("METRICS_TOKEN", "")
    authorization = request.headers.get("Authorization", "")
    if metrics_token and not secrets.compare_digest(
        authorization,
        f"Bearer {metrics_token}",
    ):
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    content = await asyncio.to_thread(generate_latest)
    return Response(
        content=content,
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )


@app.get("/text2img/data/{id}")
async def text2img_image(id: str):
    started = time.perf_counter()
    try:
        image = await image_storage.get(id)
    except Exception:
        IMAGE_STORAGE_OPERATIONS.labels(operation="get", result="error").inc()
        logger.exception("Failed to load image from storage")
        return JSONResponse(
            status_code=500,
            content={"code": 1, "message": "image storage error", "data": {}},
        )
    finally:
        IMAGE_STORAGE_DURATION.labels(operation="get").observe(
            time.perf_counter() - started
        )

    if image is None:
        IMAGE_STORAGE_OPERATIONS.labels(operation="get", result="not_found").inc()
        return JSONResponse(
            status_code=404,
            content={"code": 1, "message": "file not found", "data": {}},
        )
    IMAGE_STORAGE_OPERATIONS.labels(operation="get", result="success").inc()
    if image.path is not None:
        return FileResponse(image.path, media_type=image.media_type)
    return Response(content=image.content, media_type=image.media_type)


def remove_temporary_file(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as error:
        logger.warning(f"Failed to remove temporary file {path}: {error}")


DISCONNECT_POLL_SECONDS = 0.25
DISCONNECT_WATCHER_STOP_SECONDS = 1.0


async def render_with_disconnect(http_request, abs_path, options):
    if http_request is None:
        return await render.html2pic(abs_path, options)

    # The watcher is stopped with a flag, never with task.cancel(): Starlette's
    # is_disconnected() awaits receive() inside an already-cancelled anyio
    # CancelScope, and an external cancellation landing there is swallowed
    # together with the scope's own, leaving the watcher polling forever.
    stopped = asyncio.Event()

    async def wait_for_disconnect():
        while not stopped.is_set():
            if await http_request.is_disconnected():
                raise RenderError("client_disconnect", "Client disconnected during rendering", 499)
            with suppress(TimeoutError):
                await asyncio.wait_for(stopped.wait(), DISCONNECT_POLL_SECONDS)

    render_task = asyncio.create_task(render.html2pic(abs_path, options))
    disconnect_task = asyncio.create_task(wait_for_disconnect())
    try:
        done, _ = await asyncio.wait(
            (render_task, disconnect_task), return_when=asyncio.FIRST_COMPLETED
        )
        if render_task in done:
            return await render_task
        return await disconnect_task
    finally:
        stopped.set()
        if not render_task.done():
            render_task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(render_task, return_exceptions=True),
                LIMITS.cleanup_timeout * 4,
            )
        except TimeoutError:
            # Only an unfinished render can leave the browser in an unknown state.
            render._unhealthy = True
            logger.error("Render cancellation did not finish; refusing new renders")
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(disconnect_task, return_exceptions=True)),
                DISCONNECT_WATCHER_STOP_SECONDS,
            )
        except TimeoutError:
            logger.warning("Disconnect watcher did not stop in time; render result is unaffected")


@app.post("/text2img/generate")
async def text2img(request: GenerateRequest, http_request: fastapi.Request = None):
    temporary_files = []
    try:
        if getattr(render, "_unhealthy", False):
            raise ResourceLimitError("Renderer is unhealthy; restart the container", 503)
        check_runtime_resources()
        return await generate_image(request, http_request, temporary_files)
    except ResourceLimitError as error:
        return JSONResponse(
            status_code=error.status_code,
            content={"code": 1, "message": str(error), "data": {"stage": error.stage}},
            headers={"Retry-After": "5"} if error.status_code == 503 else None,
        )
    finally:
        for path in temporary_files:
            remove_temporary_file(path)


async def generate_image(request: GenerateRequest, http_request, temporary_files):
    """
    html: str
    options: ScreenshotOptions
    """

    retry_after = await enforce_rate_limit()
    if retry_after is not None:
        RATE_LIMIT_REJECTIONS.inc()
        return JSONResponse(
            status_code=429,
            content={"code": 1, "message": "rate limit exceeded", "data": {}},
            headers={"Retry-After": str(retry_after)},
        )

    is_json_return = request.json or False
    if request.tmpl or request.tmplname:
        if request.tmpl:
            tmpl = request.tmpl
            source = "template"
        else:
            template_root = Path("tmpl").resolve()
            template_path = (template_root / f"{request.tmplname}.html").resolve()
            if not template_path.is_relative_to(template_root) or not template_path.is_file():
                return JSONResponse(
                    status_code=400,
                    content={"code": 1, "message": "invalid template name", "data": {}},
                )
            with template_path.open("rb") as template_file:
                raw_template = template_file.read(LIMITS.max_html_bytes + 1)
            if len(raw_template) > LIMITS.max_html_bytes:
                raise ResourceLimitError("Template file exceeds T2I_MAX_HTML_BYTES")
            tmpl = raw_template.decode("utf-8")
            source = "template_name"
        check_html_size(tmpl)
        RENDER_INPUT_BYTES.labels(source=source).observe(len(tmpl.encode("utf-8")))
        try:
            _, abs_path = await render.from_jinja_template(tmpl, request.tmpldata or {})
        except ResourceLimitError:
            raise
        except SecurityError as e:
            return JSONResponse(
                status_code=400,
                content={"code": 1, "message": f"security error: {str(e)}", "data": {}},
            )
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={
                    "code": 1,
                    "message": f"template render error: {str(e)}",
                    "data": {},
                },
            )
    elif request.html:
        RENDER_INPUT_BYTES.labels(source="html").observe(
            len(request.html.encode("utf-8"))
        )
        html = request.html
        _, abs_path = await render.from_html(html)
    else:
        return JSONResponse(
            status_code=400,
            content={"code": 1, "message": "html or tmpl not found", "data": {}},
        )
    temporary_files.append(abs_path)
    options = (
        request.options
        if request.options
        else ScreenshotOptions(
            timeout=None,
            type="png",
            quality=None,
            omit_background=None,
            full_page=True,
            clip=None,
            animations=None,
            caret=None,
            scale="device",
            viewport_width=None,
            viewport_height=None,
            device_scale_factor_level=None,
        )
    )

    try:
        pic = await render_with_disconnect(http_request, abs_path, options)
    except Exception as e:
        try:
            options_dump = options.model_dump(exclude_none=True)
        except Exception as dump_error:
            logger.warning(f"failed to dump screenshot options: {dump_error}")
            options_dump = {}

        return_mode = "json" if is_json_return else "file"
        logger.warning(
            f"render failed: {type(e).__name__}: {e}; "
            f"abs_path={abs_path}, options={options_dump}, return_mode={return_mode}"
        )

        error_data = {
            "error_type": type(e).__name__,
            "html_file": os.path.basename(abs_path),
            "options": options_dump,
        }
        if isinstance(e, (RenderError, ResourceLimitError)):
            error_data["stage"] = e.stage
        status_code = e.status_code if isinstance(e, (RenderError, ResourceLimitError)) else 500

        return JSONResponse(
            status_code=status_code,
            content={
                "code": 1,
                "message": f"render error: {str(e)}",
                "data": error_data,
            },
            headers={"Retry-After": "5"} if status_code == 503 else None,
        )

    media_type = "image/png" if pic.endswith(".png") else "image/jpeg"

    if is_json_return:
        started = time.perf_counter()
        try:
            image_id = await image_storage.save(pic)
        except Exception:
            IMAGE_STORAGE_OPERATIONS.labels(operation="save", result="error").inc()
            logger.exception("Failed to persist rendered image")
            return JSONResponse(
                status_code=500,
                content={
                    "code": 1,
                    "message": "image storage error",
                    "data": {},
                },
            )
        finally:
            IMAGE_STORAGE_DURATION.labels(operation="save").observe(
                time.perf_counter() - started
            )
        IMAGE_STORAGE_OPERATIONS.labels(operation="save", result="success").inc()
        return JSONResponse(
            content={
                "code": 0,
                "message": "success",
                "data": {"id": f"data/{image_id}"},
            },
        )
    else:
        return FileResponse(
            pic,
            media_type=media_type,
            background=BackgroundTask(remove_temporary_file, pic),
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8999)))
