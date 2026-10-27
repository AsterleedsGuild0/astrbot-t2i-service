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
    def __init__(
        self, message: str, status_code: int = 413, *,
        category: str, limit_name: str, limit_value,
        observed=None, rejection_stage: str = "resource_limit",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.stage = "resource_limit"
        self.rejection = {
            "status": status_code, "stage": rejection_stage, "category": category,
            "limit_name": limit_name, "limit_value": limit_value, "observed": observed,
        }


def check_html_size(content: str, *, template: bool = False) -> None:
    from .diagnostics import record_sizes

    size = len(content.encode("utf-8"))
    record_sizes(**({"template_source_bytes": size} if template else {"html_bytes": size}))
    if size > LIMITS.max_html_bytes:
        raise ResourceLimitError(
            "HTML or template exceeds T2I_MAX_HTML_BYTES",
            category="template_input" if template else "html_size",
            limit_name="T2I_MAX_HTML_BYTES", limit_value=LIMITS.max_html_bytes,
            observed=size, rejection_stage="template_input" if template else "html",
        )


def check_screenshot_size(width: float, height: float, scale: float) -> int:
    if not all(math.isfinite(value) and value > 0 for value in (width, height, scale)):
        raise ResourceLimitError(
            "Screenshot dimensions must be finite and positive",
            category="screenshot_dimensions", limit_name="finite_positive_dimensions",
            limit_value=True, rejection_stage="screenshot",
        )
    pixel_width = math.ceil(width * scale)
    pixel_height = math.ceil(height * scale)
    pixels = pixel_width * pixel_height
    if max(pixel_width, pixel_height) > LIMITS.max_screenshot_dimension:
        raise ResourceLimitError(
            "Screenshot exceeds T2I_MAX_SCREENSHOT_DIMENSION",
            category="screenshot_dimension", limit_name="T2I_MAX_SCREENSHOT_DIMENSION",
            limit_value=LIMITS.max_screenshot_dimension,
            observed=max(pixel_width, pixel_height), rejection_stage="screenshot",
        )
    if pixels > LIMITS.max_screenshot_pixels:
        raise ResourceLimitError(
            "Screenshot exceeds T2I_MAX_SCREENSHOT_PIXELS",
            category="screenshot_pixels", limit_name="T2I_MAX_SCREENSHOT_PIXELS",
            limit_value=LIMITS.max_screenshot_pixels, observed=pixels,
            rejection_stage="screenshot",
        )
    return pixels


def _read_cgroup_number(path: str) -> int | None:
    try:
        value = Path(path).read_text(encoding="ascii").strip()
        return None if value == "max" else int(value)
    except (OSError, ValueError):
        return None


def check_runtime_resources(*, stage: str = "admission") -> None:
    current = _read_cgroup_number("/sys/fs/cgroup/memory.current")
    maximum = _read_cgroup_number("/sys/fs/cgroup/memory.max")
    if current is None or maximum is None:
        current = _read_cgroup_number("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        maximum = _read_cgroup_number("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    # Reserve headroom inside a configured container limit before opening another page.
    if current is not None and maximum and current >= maximum * 0.9:
        raise ResourceLimitError(
            "Container memory is above 90% of its limit", 503,
            category="container_memory", limit_name="cgroup_memory_ratio",
            limit_value=0.9, observed=current / maximum, rejection_stage=stage,
        )
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                available = int(line.split()[1]) * 1024
                if available < LIMITS.min_available_memory_bytes:
                    raise ResourceLimitError(
                        "Host available memory is below the safety reserve", 503,
                        category="host_memory", limit_name="T2I_MIN_AVAILABLE_MEMORY_MB",
                        limit_value=LIMITS.min_available_memory_bytes // (1024 * 1024),
                        observed=available / (1024 * 1024), rejection_stage=stage,
                    )
                break
    except (OSError, ValueError, IndexError):
        pass
    data_dir = Path("data") if Path("data").exists() else Path(".")
    free = shutil.disk_usage(data_dir).free
    if free < LIMITS.min_free_disk_bytes:
        raise ResourceLimitError(
            "Insufficient free disk space for rendering", 507,
            category="disk_reserve", limit_name="T2I_MIN_FREE_DISK_MB",
            limit_value=LIMITS.min_free_disk_bytes // (1024 * 1024),
            observed=free / (1024 * 1024), rejection_stage=stage,
        )
