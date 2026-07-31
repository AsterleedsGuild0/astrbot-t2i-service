import os
import re
import time

from jinja2.sandbox import SandboxedEnvironment
from loguru import logger
from playwright._impl._errors import TargetClosedError
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from pydantic import BaseModel
from typing import Literal, cast
from typing_extensions import TypedDict

from .metrics import (
    BROWSER_CONNECTED,
    BROWSER_CONTEXTS,
    BROWSER_RESTARTS,
    BROWSER_STARTS,
    RENDER_ACTIVE_PAGES,
    RENDER_DURATION,
    RENDER_IN_PROGRESS,
    RENDER_OUTPUT_BYTES,
    RENDER_REQUESTS,
    RENDER_VIEWPORT_PIXELS,
)
from .util import generate_data_path

WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"", "0", "false", "no", "off"}:
        return False

    logger.warning(f"Invalid {name} value {value!r}; falling back to {default}")
    return default


def _env_wait_until() -> WaitUntil:
    value = os.getenv("T2I_RENDER_WAIT_UNTIL", "domcontentloaded").strip().lower()
    valid_wait_until = {"commit", "domcontentloaded", "load", "networkidle"}
    if value in valid_wait_until:
        return cast(WaitUntil, value)

    logger.warning(
        "Invalid T2I_RENDER_WAIT_UNTIL value "
        f"{value!r}; falling back to domcontentloaded"
    )
    return "domcontentloaded"


DEFAULT_RENDER_WAIT_UNTIL = _env_wait_until()
SKIP_FONT_READY = _env_flag("T2I_SKIP_FONT_READY", True)

# 调用方未指定 device_scale_factor_level 时使用的默认等级。
# 取 high(1.3x) 而非 normal(1.0x)：清晰度上限由真实像素数决定，1.0x 无超采样时
# 文字抗锯齿细节最少，是观感模糊的主要来源之一。
DEFAULT_SCALE_LEVEL = "high"

# JPEG 质量下限。部分调用方（如 AstrBot 默认的 quality=40）为省流量传入过低的质量，
# 会让文字边缘产生明显的块状压缩失真。低于此值时统一抬升到下限。
MIN_JPEG_QUALITY = 70

# Playwright 默认会在截图前等待 document.fonts.ready。服务端场景中若远程字体
# 加载缓慢或不可达，可能导致截图阶段超时。该开关允许在需要时跳过这一步等待。
if SKIP_FONT_READY:
    os.environ.setdefault("PW_TEST_SCREENSHOT_NO_FONTS_READY", "1")


class RenderError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


class FloatRect(TypedDict):
    x: float
    y: float
    width: float
    height: float


class ScreenshotOptions(BaseModel):
    """Playwright 截图参数

    详见：https://playwright.dev/python/docs/api/class-page#page-screenshot

    Args:
        timeout (float, optional): 截图超时时间.
        type (Literal["jpeg", "png"], optional): 截图图片类型.
        path (Union[str, Path]], optional): 截图保存路径，如不需要则留空.
        quality (int, optional): 截图质量，仅适用于 JPEG 格式图片.
            低于 MIN_JPEG_QUALITY(70) 或未指定时会被抬升到该下限，避免文字出现压缩失真.
        omit_background (bool, optional): 是否允许隐藏默认的白色背景，这样就可以截透明图了，仅适用于 PNG 格式.
        full_page (bool, optional): 是否截整个页面而不是仅设置的视口大小，默认为 True.
        clip (FloatRect, optional): 截图后裁切的区域，xy为起点.
        animations: (Literal["allow", "disabled"], optional): 是否允许播放 CSS 动画.
        caret: (Literal["hide", "initial"], optional): 当设置为 `hide` 时，截图时将隐藏文本插入符号，默认为 `hide`.
        scale: (Literal["css", "device"], optional): 页面缩放设置.
            当设置为 `css` 时，则将设备分辨率与 CSS 中的像素一一对应，在高分屏上会使得截图变小.
            当设置为 `device` 时，则根据设备的屏幕缩放设置或当前 Playwright 的 Page/Context 中的
            device_scale_factor 参数来缩放.
        viewport_width: (int, optional): 自定义视口宽度，用于控制截图宽度.
            优先级：
            1. 显式指定此参数；
            2. 从 HTML 的 <meta name="viewport" content="width=..."> 自动解析；
            3. 未指定时默认为 800px.
        viewport_height: (int, optional): 自定义视口高度，用于控制截图高度.
            优先级：
            1. 显式指定此参数；
            2. 从 HTML 的 <meta name="viewport" content="height=..."> 自动解析；
            3. 未指定时默认为 720px.
        device_scale_factor_level: (Literal["normal", "high", "ultra"], optional): 设备像素比等级.
            未指定时默认为 DEFAULT_SCALE_LEVEL("high").
            - normal: 1.0
            - high: 1.3
            - ultra: 1.8

    @author: Redlnn(https://github.com/GraiaCommunity/graiax-text2img-playwright)
    """

    timeout: float | None = None
    wait_until: WaitUntil | None = DEFAULT_RENDER_WAIT_UNTIL
    type: Literal["jpeg", "png", None] = None
    quality: int | None = None
    omit_background: bool | None = None
    full_page: bool | None = True
    clip: FloatRect | None = None
    animations: Literal["allow", "disabled", None] = None
    caret: Literal["hide", "initial", None] = None
    scale: Literal["css", "device", None] = None
    viewport_width: int | None = None
    viewport_height: int | None = None
    device_scale_factor_level: Literal["normal", "high", "ultra", None] = None


class Text2ImgRender:
    # Mapping from device_scale_factor_level to actual device_scale_factor
    SCALE_FACTOR_MAP = {
        "normal": 1.0,
        "high": 1.3,
        "ultra": 1.8,
    }

    def __init__(self):
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        # Context pool: {"normal": context, "high": context, "ultra": context}
        self.contexts: dict[str, BrowserContext] = {}
        logger.info(
            "Text2ImgRender config: "
            f"default_wait_until={DEFAULT_RENDER_WAIT_UNTIL}, "
            f"skip_font_ready={SKIP_FONT_READY}, "
            f"default_scale_level={DEFAULT_SCALE_LEVEL}, "
            f"min_jpeg_quality={MIN_JPEG_QUALITY}"
        )

    def _resolve_wait_until(self, wait_until: WaitUntil | None) -> WaitUntil:
        return wait_until or DEFAULT_RENDER_WAIT_UNTIL

    async def _ensure_context(self, level: str = DEFAULT_SCALE_LEVEL) -> BrowserContext:
        """Ensure that Playwright, Browser and BrowserContext are initialized.

        Args:
            level: Device scale factor level ("normal", "high", or "ultra").
                   Defaults to DEFAULT_SCALE_LEVEL if not specified.

        Returns:
            The BrowserContext for the specified level.
        """
        if self.playwright is None:
            self.playwright = await async_playwright().start()

        # ensure browser launched
        if self.browser is None or not self.browser.is_connected():
            if self.browser is not None:
                try:
                    await self.browser.close()
                except Exception as e:
                    logger.debug(f"Close old browser failed: {e}")
            self.browser = await self.playwright.chromium.launch(headless=True)
            BROWSER_STARTS.inc()
            BROWSER_CONNECTED.set(1)

        # ensure context available for the specified level
        if level not in self.contexts:
            scale_factor = self.SCALE_FACTOR_MAP.get(level, 1.0)
            self.contexts[level] = await self.browser.new_context(
                device_scale_factor=scale_factor,
            )
            logger.info(
                f"Created context for level '{level}' with device_scale_factor={scale_factor}"
            )
            BROWSER_CONTEXTS.set(len(self.contexts))

        return self.contexts[level]

    async def from_jinja_template(self, template: str, data: dict) -> tuple[str, str]:
        env = SandboxedEnvironment()
        html = env.from_string(template).render(data)
        return await self.from_html(html)

    async def from_html(self, html: str) -> tuple[str, str]:
        html_file_path, abs_path = generate_data_path(
            suffix="html", namespace="rendered"
        )
        with open(html_file_path, "w", encoding="utf-8") as f:
            f.write(html)
        return html_file_path, abs_path

    def _resolve_viewport_size(
        self, html_file_path: str, screenshot_options: ScreenshotOptions
    ) -> tuple[int | None, int | None]:
        """根据截图参数与 HTML 内容推断 viewport 大小（宽, 高）。

        优先级：
        1. 调用方在 ScreenshotOptions 中显式指定 `viewport_width` / `viewport_height`；
        2. 从 HTML 中的 `<meta name="viewport" content="width=...; height=...">` 自动解析；
        3. 未能解析到时返回对应的 None（调用方可选择使用 Playwright 默认值）。

        将逻辑集中到独立方法，便于后续扩展：
        - 支持更多 meta 语法 / 自定义 data-* 属性；
        - 支持从额外配置源中读取默认宽度等。
        """

        viewport_width: int | None = screenshot_options.viewport_width
        viewport_height: int | None = screenshot_options.viewport_height

        # 如果两者都有显式值，直接返回
        if viewport_width is not None and viewport_height is not None:
            return viewport_width, viewport_height

        # 未指定时，尝试从 HTML meta 中解析（只读前几 KB 即可命中 <head> 区域）
        try:
            with open(html_file_path, "r", encoding="utf-8") as f:
                head_snippet = f.read(4096)

            # 尝试解析宽度和高度（允许任意顺序出现在 content 中）
            if viewport_width is None:
                pattern = (
                    r'<meta\s+[^>]*name=["\']viewport["\'][^>]*'
                    r'content=["\'][^"\']*width\s*=\s*(\d+)[^"\']*["\'][^>]*>'
                )
                if m := re.search(pattern, head_snippet, re.IGNORECASE):
                    viewport_width = int(m[1])

            if viewport_height is None:
                pattern = (
                    r'<meta\s+[^>]*name=["\']viewport["\'][^>]*'
                    r'content=["\'][^"\']*height\s*=\s*(\d+)[^"\']*["\'][^>]*>'
                )
                if m := re.search(pattern, head_snippet, re.IGNORECASE):
                    viewport_height = int(m[1])
        except (OSError, UnicodeDecodeError, re.error, ValueError) as e:
            logger.debug(f"Adjust viewport from meta tag failed: {e}")

        return viewport_width, viewport_height

    async def terminate(self) -> None:
        """Terminate Playwright and close browser."""
        # Close all contexts in the pool
        for level, context in list(self.contexts.items()):
            try:
                await context.close()
                logger.debug(f"Closed context for level '{level}'")
            except Exception as e:
                logger.debug(f"Close context for level '{level}' failed: {e}")
        self.contexts.clear()
        BROWSER_CONTEXTS.set(0)

        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception as e:
                logger.debug(f"Close browser failed: {e}")
            self.browser = None
        BROWSER_CONNECTED.set(0)

        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except Exception as e:
                logger.debug(f"Stop Playwright failed: {e}")
            self.playwright = None

    async def html2pic(
        self, html_file_path: str, screenshot_options: ScreenshotOptions
    ) -> str:
        # Determine which context to use based on device_scale_factor_level
        level = screenshot_options.device_scale_factor_level or DEFAULT_SCALE_LEVEL
        suffix = screenshot_options.type if screenshot_options.type else "png"
        started = time.perf_counter()
        result = "error"
        page = None
        RENDER_IN_PROGRESS.inc()
        try:
            context = await self._ensure_context(level)
            result_path, _ = generate_data_path(suffix=suffix, namespace="rendered")

            try:
                page = await context.new_page()
            except TargetClosedError as e:
                BROWSER_RESTARTS.labels(reason="target_closed").inc()
                logger.warning(
                    f"html2pic: Failed to create new page, restarting browser context: {e}"
                )
                # Close and remove the specific context, then recreate it
                if level in self.contexts:
                    try:
                        await self.contexts[level].close()
                    except Exception:
                        pass
                    del self.contexts[level]
                    BROWSER_CONTEXTS.set(len(self.contexts))
                context = await self._ensure_context(level)
                page = await context.new_page()
            RENDER_ACTIVE_PAGES.inc()

            wait_until = self._resolve_wait_until(screenshot_options.wait_until)

            def _truncate(value: str | None, limit: int = 300) -> str:
                if value is None:
                    return ""
                return value if len(value) <= limit else f"{value[:limit]}..."

            def _sanitize_url(url: str | None) -> str:
                if not url:
                    return ""
                safe_url = url.split("?", 1)[0].split("#", 1)[0]
                return _truncate(safe_url)

            def _request_failure_reason(request) -> str:
                failure = getattr(request, "failure", None)
                if callable(failure):
                    failure = failure()
                if failure is None:
                    return "no_detail"
                return _truncate(str(failure))

            def _log_render_event(event: str, level_name: str = "warning", **details) -> None:
                log_func = getattr(logger, level_name, logger.warning)
                log_func(
                    "html2pic page event: "
                    f"event={event}, html_file_path={html_file_path}, "
                    f"timeout={screenshot_options.timeout}, wait_until={wait_until}, "
                    f"full_page={screenshot_options.full_page}, type={screenshot_options.type}, "
                    f"device_scale_factor_level={level}, details={details}"
                )

            page.on(
                "requestfailed",
                lambda request: _log_render_event(
                    "requestfailed",
                    url=_sanitize_url(getattr(request, "url", None)),
                    failure=_request_failure_reason(request),
                ),
            )
            page.on(
                "response",
                lambda response: (
                    _log_render_event(
                        "response_error",
                        level_name="info",
                        status=getattr(response, "status", None),
                        url=_sanitize_url(getattr(response, "url", None)),
                    )
                    if getattr(response, "status", 0) >= 400
                    else None
                ),
            )
            page.on(
                "pageerror",
                lambda error: _log_render_event(
                    "pageerror",
                    message=_truncate(str(error)),
                ),
            )
            page.on(
                "console",
                lambda message: (
                    _log_render_event(
                        "console",
                        level_name=(
                            "warning"
                            if getattr(message, "type", None) == "error"
                            else "info"
                        ),
                        type=getattr(message, "type", None),
                        message=_truncate(getattr(message, "text", None)),
                    )
                    if getattr(message, "type", None) in {"warning", "error"}
                    else None
                ),
            )

            viewport_width, viewport_height = self._resolve_viewport_size(
                html_file_path, screenshot_options
            )

            width = viewport_width if viewport_width is not None else 800
            height = viewport_height if viewport_height is not None else 720
            scale_factor = self.SCALE_FACTOR_MAP.get(level, 1.0)
            RENDER_VIEWPORT_PIXELS.labels(scale=level).observe(
                width * height * scale_factor * scale_factor
            )
            # Always set viewport size to ensure defaults are applied
            await page.set_viewport_size({"width": width, "height": height})
            logger.info(f"html2pic: set viewport size to {width}x{height}")

            try:
                logger.info(
                    "html2pic goto start: "
                    f"html_file_path={html_file_path}, timeout={screenshot_options.timeout}, "
                    f"wait_until={wait_until}, full_page={screenshot_options.full_page}, "
                    f"type={screenshot_options.type}, device_scale_factor_level={level}"
                )
                try:
                    await page.goto(
                        f"file://{html_file_path}",
                        timeout=screenshot_options.timeout,
                        wait_until=wait_until,
                    )
                except Exception as e:
                    logger.exception(
                        "html2pic goto failed: "
                        f"html_file_path={html_file_path}, timeout={screenshot_options.timeout}, "
                        f"wait_until={wait_until}, full_page={screenshot_options.full_page}, "
                        f"type={screenshot_options.type}, device_scale_factor_level={level}"
                    )
                    raise RenderError("goto", f"page.goto failed: {e}") from e

                screenshot_kwargs = screenshot_options.model_dump(exclude_none=True)
                screenshot_kwargs.pop("wait_until", None)
                screenshot_kwargs.pop("viewport_width", None)
                screenshot_kwargs.pop("viewport_height", None)
                screenshot_kwargs.pop("device_scale_factor_level", None)

                # Robustness: Remove quality if type is png, as Playwright errors out
                if screenshot_options.type == "png":
                    screenshot_kwargs.pop("quality", None)
                elif screenshot_options.type == "jpeg":
                    # 抬升过低的 JPEG 质量，避免文字边缘出现块状压缩失真
                    quality = screenshot_kwargs.get("quality")
                    if quality is None or quality < MIN_JPEG_QUALITY:
                        logger.info(
                            "html2pic: raise jpeg quality from "
                            f"{quality} to {MIN_JPEG_QUALITY}"
                        )
                        screenshot_kwargs["quality"] = MIN_JPEG_QUALITY

                logger.info(
                    "html2pic screenshot start: "
                    f"html_file_path={html_file_path}, timeout={screenshot_options.timeout}, "
                    f"wait_until={wait_until}, full_page={screenshot_options.full_page}, "
                    f"type={screenshot_options.type}, device_scale_factor_level={level}"
                )
                try:
                    await page.screenshot(path=result_path, **screenshot_kwargs)
                except Exception as e:
                    logger.exception(
                        "html2pic screenshot failed: "
                        f"html_file_path={html_file_path}, timeout={screenshot_options.timeout}, "
                        f"wait_until={wait_until}, full_page={screenshot_options.full_page}, "
                        f"type={screenshot_options.type}, device_scale_factor_level={level}"
                    )
                    raise RenderError("screenshot", f"page.screenshot failed: {e}") from e
            finally:
                # Ensure the page is closed to free resources
                try:
                    await page.close()
                except Exception as e:
                    logger.debug(f"html2pic: close page failed: {e}")
                page = None
                RENDER_ACTIVE_PAGES.dec()

            RENDER_OUTPUT_BYTES.labels(format=suffix).observe(
                os.path.getsize(result_path)
            )
            result = "success"
            logger.info(f"Rendered {html_file_path} to {result_path}")
            return result_path
        finally:
            if page is not None:
                try:
                    await page.close()
                finally:
                    RENDER_ACTIVE_PAGES.dec()
            if self.browser is None or not self.browser.is_connected():
                BROWSER_CONNECTED.set(0)
            RENDER_REQUESTS.labels(
                result=result,
                format=suffix,
                scale=level,
            ).inc()
            RENDER_DURATION.labels(result=result, scale=level).observe(
                time.perf_counter() - started
            )
            RENDER_IN_PROGRESS.dec()
