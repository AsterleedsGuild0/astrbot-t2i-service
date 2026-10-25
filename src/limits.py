import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from loguru import logger


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        if value >= minimum:
            return value
    except ValueError:
        pass
    logger.warning(f"Invalid {name}; falling back to {default}")
    return default


@dataclass(frozen=True)
class RenderLimits:
    max_request_bytes: int
    max_html_bytes: int
    max_queue_size: int
    queue_timeout: int
    body_timeout: int
    template_timeout: int
    template_memory_bytes: int
    render_timeout: int
    operation_timeout_ms: int
    cleanup_timeout: int
    max_viewport_dimension: int
    max_screenshot_dimension: int
    max_screenshot_pixels: int
    max_scale_level: str
    browser_max_renders: int
    min_available_memory_bytes: int
    min_free_disk_bytes: int
    max_page_event_logs: int

    @classmethod
    def from_env(cls):
        max_scale = os.getenv("T2I_MAX_SCALE_LEVEL", "ultra").strip().lower()
        if max_scale not in {"normal", "high", "ultra"}:
            logger.warning("Invalid T2I_MAX_SCALE_LEVEL; falling back to ultra")
            max_scale = "ultra"
        return cls(
            max_request_bytes=_env_int("T2I_MAX_REQUEST_BYTES", 8 * 1024 * 1024),
            max_html_bytes=_env_int("T2I_MAX_HTML_BYTES", 4 * 1024 * 1024),
            max_queue_size=_env_int("T2I_MAX_QUEUE_SIZE", 2, minimum=0),
            queue_timeout=_env_int("T2I_QUEUE_TIMEOUT_SECONDS", 10),
            body_timeout=_env_int("T2I_BODY_TIMEOUT_SECONDS", 15),
            template_timeout=_env_int("T2I_TEMPLATE_TIMEOUT_SECONDS", 10),
            template_memory_bytes=_env_int("T2I_TEMPLATE_MEMORY_MB", 256) * 1024 * 1024,
            render_timeout=_env_int("T2I_RENDER_TIMEOUT_SECONDS", 45),
            operation_timeout_ms=_env_int("T2I_OPERATION_TIMEOUT_MS", 30000),
            cleanup_timeout=_env_int("T2I_CLEANUP_TIMEOUT_SECONDS", 3),
            max_viewport_dimension=_env_int("T2I_MAX_VIEWPORT_DIMENSION", 4096),
            max_screenshot_dimension=_env_int("T2I_MAX_SCREENSHOT_DIMENSION", 16384),
            max_screenshot_pixels=_env_int("T2I_MAX_SCREENSHOT_PIXELS", 16000000),
            max_scale_level=max_scale,
            browser_max_renders=_env_int("T2I_BROWSER_MAX_RENDERS", 100),
            min_available_memory_bytes=_env_int("T2I_MIN_AVAILABLE_MEMORY_MB", 128) * 1024 * 1024,
            min_free_disk_bytes=_env_int("T2I_MIN_FREE_DISK_MB", 128) * 1024 * 1024,
            max_page_event_logs=_env_int("T2I_MAX_PAGE_EVENT_LOGS", 20),
        )


LIMITS = RenderLimits.from_env()


class ResourceLimitError(RuntimeError):
    def __init__(self, message: str, status_code: int = 413):
        super().__init__(message)
        self.status_code = status_code
        self.stage = "resource_limit"


def check_html_size(content: str) -> None:
    if len(content.encode("utf-8")) > LIMITS.max_html_bytes:
        raise ResourceLimitError("HTML or template exceeds T2I_MAX_HTML_BYTES")


def check_screenshot_size(width: float, height: float, scale: float) -> int:
    if not all(math.isfinite(value) and value > 0 for value in (width, height, scale)):
        raise ResourceLimitError("Screenshot dimensions must be finite and positive")
    pixel_width = math.ceil(width * scale)
    pixel_height = math.ceil(height * scale)
    pixels = pixel_width * pixel_height
    if max(pixel_width, pixel_height) > LIMITS.max_screenshot_dimension:
        raise ResourceLimitError("Screenshot exceeds T2I_MAX_SCREENSHOT_DIMENSION")
    if pixels > LIMITS.max_screenshot_pixels:
        raise ResourceLimitError("Screenshot exceeds T2I_MAX_SCREENSHOT_PIXELS")
    return pixels


def _read_cgroup_number(path: str) -> int | None:
    try:
        value = Path(path).read_text(encoding="ascii").strip()
        return None if value == "max" else int(value)
    except (OSError, ValueError):
        return None


def check_runtime_resources() -> None:
    current = _read_cgroup_number("/sys/fs/cgroup/memory.current")
    maximum = _read_cgroup_number("/sys/fs/cgroup/memory.max")
    if current is None or maximum is None:
        current = _read_cgroup_number("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        maximum = _read_cgroup_number("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    # Reserve headroom inside a configured container limit before opening another page.
    if current is not None and maximum and current >= maximum * 0.9:
        raise ResourceLimitError("Container memory is above 90% of its limit", 503)
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                available = int(line.split()[1]) * 1024
                if available < LIMITS.min_available_memory_bytes:
                    raise ResourceLimitError("Host available memory is below the safety reserve", 503)
                break
    except (OSError, ValueError, IndexError):
        pass
    data_dir = Path("data") if Path("data").exists() else Path(".")
    if shutil.disk_usage(data_dir).free < LIMITS.min_free_disk_bytes:
        raise ResourceLimitError("Insufficient free disk space for rendering", 507)
