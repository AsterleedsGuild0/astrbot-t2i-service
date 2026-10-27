import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger
from playwright._impl._errors import TargetClosedError

from src import api, limits, render as render_module
from src.diagnostics import current_diagnostics
from src.render import ScreenshotOptions, Text2ImgRender

SENTINEL = "PRIVATE_BODY_SENTINEL_群聊"


@pytest.fixture
def warnings():
    records = []
    sink = logger.add(lambda message: records.append(message.record), level="WARNING")
    try:
        yield records
    finally:
        logger.remove(sink)


def rejection(records, category, status):
    entries = [r for r in records if "resource_rejection" in r["extra"]]
    assert len(entries) == 1
    assert entries[0]["level"].name == "WARNING"
    assert "\n" not in entries[0]["message"]
    assert "PRIVATE_BODY_SENTINEL" not in " ".join(r["message"] for r in records)
    fields = entries[0]["extra"]["resource_rejection"]
    assert fields["category"] == category
    assert fields["status"] == status
    return fields


def test_html_bytes_and_safe_options_without_content(monkeypatch, warnings):
    monkeypatch.setattr(limits, "LIMITS", replace(limits.LIMITS, max_html_bytes=8))
    request = api.GenerateRequest(
        html=SENTINEL,
        options={"type": "png", "device_scale_factor_level": "ultra", "unknown": SENTINEL},
    )
    response = asyncio.run(api.text2img(request))
    fields = rejection(warnings, "html_size", 413)
    assert json.loads(response.body) == {
        "code": 1, "message": "HTML or template exceeds T2I_MAX_HTML_BYTES",
        "data": {"stage": "resource_limit"},
    }
    assert fields["limit_name"] == "T2I_MAX_HTML_BYTES"
    assert fields["limit_value"] == 8
    assert fields["observed"] == fields["html_bytes"] == len(SENTINEL.encode())
    assert fields["options"]["type"] == "png"
    assert fields["options"]["device_scale_factor_level"] == "ultra"
    assert "unknown" not in fields["options"]
    assert current_diagnostics.get() is None


def test_named_template_limit_does_not_log_template_name(tmp_path, monkeypatch, warnings):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tmpl").mkdir()
    (tmp_path / "tmpl" / f"{SENTINEL}.html").write_text(SENTINEL)
    monkeypatch.setattr(api, "LIMITS", replace(limits.LIMITS, max_html_bytes=8))
    response = asyncio.run(api.text2img(api.GenerateRequest(tmplname=SENTINEL)))
    assert response.status_code == 413
    fields = rejection(warnings, "template_input", 413)
    assert fields["template_source_bytes"] == 9  # Bounded read, not total file size.
    assert fields["limit_value"] == 8


@pytest.mark.parametrize(
    "cause,category,status,limit_name,limit_value,observed",
    [
        ("cgroup", "container_memory", 503, "cgroup_memory_ratio", 0.9, 0.95),
        ("host", "host_memory", 503, "T2I_MIN_AVAILABLE_MEMORY_MB", 128, 1),
        ("disk", "disk_reserve", 507, "T2I_MIN_FREE_DISK_MB", 128, 1),
    ],
)
def test_runtime_reserves_log_actual_measurements(
    cause, category, status, limit_name, limit_value, observed, monkeypatch, warnings
):
    def cgroup(path):
        return (95 if path.endswith("current") else 100) if cause == "cgroup" else None

    monkeypatch.setattr(limits, "_read_cgroup_number", cgroup)
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/meminfo":
            return "MemAvailable: 1024 kB" if cause == "host" else "MemAvailable: 1048576 kB"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(limits.shutil, "disk_usage", lambda _: SimpleNamespace(free=1024 * 1024))
    monkeypatch.setattr(limits, "LIMITS", replace(
        limits.LIMITS, min_available_memory_bytes=128 * 1024 * 1024,
        min_free_disk_bytes=128 * 1024 * 1024,
    ))
    response = asyncio.run(api.text2img(api.GenerateRequest(html=SENTINEL)))
    assert response.status_code == status
    fields = rejection(warnings, category, status)
    assert (fields["limit_name"], fields["limit_value"], fields["observed"]) == (
        limit_name, limit_value, observed,
    )
    if status == 503:
        assert response.headers["retry-after"] == "5"


class Page:
    def on(self, *_):
        pass

    async def set_viewport_size(self, viewport):
        self.viewport = viewport

    async def goto(self, *_args, **_kwargs):
        pass

    async def evaluate(self, _):
        return self.viewport

    async def screenshot(self, **_):
        raise AssertionError("Rejected input must not reach screenshot")

    async def close(self):
        pass


@pytest.mark.parametrize(
    "case,category,status,limit_name",
    [
        ("viewport", "viewport_dimension", 413, "T2I_MAX_VIEWPORT_DIMENSION"),
        ("pixels", "screenshot_pixels", 413, "T2I_MAX_SCREENSHOT_PIXELS"),
        ("dimension", "screenshot_dimension", 413, "T2I_MAX_SCREENSHOT_DIMENSION"),
        ("document_pixels", "screenshot_pixels", 413, "T2I_MAX_SCREENSHOT_PIXELS"),
        ("document_dimension", "screenshot_dimension", 413, "T2I_MAX_SCREENSHOT_DIMENSION"),
        ("invalid_dimension", "screenshot_dimensions", 413, "finite_positive_dimensions"),
        ("clip", "screenshot_clip", 413, "document_bounds"),
        ("deadline", "render_timeout", 504, "T2I_RENDER_TIMEOUT_SECONDS"),
        ("browser", "browser_unavailable", 503, "browser_connected"),
        ("cleanup", "renderer_cleanup", 503, "T2I_CLEANUP_TIMEOUT_SECONDS"),
        ("unhealthy", "renderer_unhealthy", 503, "renderer_healthy"),
    ],
)
def test_render_rejections_log_once_and_keep_response_contract(
    case, category, status, limit_name, tmp_path, monkeypatch, warnings
):
    monkeypatch.chdir(tmp_path)
    renderer = Text2ImgRender()
    page = Page()

    async def document_size(_):
        if case == "document_pixels":
            return {"width": 4001, "height": 4000}
        if case == "document_dimension":
            return {"width": 16385, "height": 1}
        return {"width": float("nan"), "height": 1}

    if case in {"document_pixels", "document_dimension", "invalid_dimension"}:
        monkeypatch.setattr(page, "evaluate", document_size)

    class Context:
        async def new_page(self):
            if case == "browser":
                raise TargetClosedError()
            return page

    async def ensure(_):
        return Context()

    async def stuck_render(*_):
        await asyncio.Event().wait()

    async def fail_close():
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(renderer, "_ensure_context", ensure)
    monkeypatch.setattr(api, "render", renderer)
    changed = replace(limits.LIMITS,
        max_viewport_dimension=99 if case == "viewport" else 4096,
        max_screenshot_pixels=9999 if case == "pixels" else 16000000,
        max_screenshot_dimension=99 if case == "dimension" else 16384,
        render_timeout=0.01 if case == "deadline" else 45,
    )
    monkeypatch.setattr(limits, "LIMITS", changed)
    monkeypatch.setattr(render_module, "LIMITS", changed)
    if case == "deadline":
        monkeypatch.setattr(renderer, "_html2pic", stuck_render)
    elif case == "cleanup":
        monkeypatch.setattr(page, "close", fail_close)
        monkeypatch.setattr(page, "screenshot", stuck_render)
        # Trigger a viewport limit first; cleanup failure supersedes that rejection.
        monkeypatch.setattr(render_module, "LIMITS", replace(changed, max_viewport_dimension=99))
    elif case == "unhealthy":
        renderer._unhealthy = True
    options = ScreenshotOptions(
        type="png", device_scale_factor_level="normal", viewport_width=100, viewport_height=100,
        clip={"x": 90, "y": 0, "width": 20, "height": 10} if case == "clip" else None,
    )
    response = asyncio.run(api.text2img(api.GenerateRequest(html=SENTINEL, options=options)))
    assert response.status_code == status
    fields = rejection(warnings, category, status)
    assert fields["limit_name"] == limit_name
    assert fields["options"]["type"] == "png"
    assert current_diagnostics.get() is None
    payload = json.loads(response.body)
    assert payload["code"] == 1
    assert payload["data"]["stage"] in {"resource_limit", "deadline", "browser", "cleanup"}
    if status == 503:
        assert response.headers["retry-after"] == "5"
