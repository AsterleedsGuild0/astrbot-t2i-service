import asyncio

from src import render as render_module
from src.render import ScreenshotOptions, Text2ImgRender


class FakePage:
    """记录 page.screenshot 实际收到的参数，用于断言服务端的参数归一化行为。"""

    def __init__(self, recorder: dict):
        self.recorder = recorder
        self.viewport: dict | None = None

    def on(self, event, handler):  # noqa: D401 - 事件监听在测试中无副作用
        return None

    async def set_viewport_size(self, viewport):
        self.viewport = viewport

    async def goto(self, url, timeout=None, wait_until=None):
        return None

    async def screenshot(self, path, **kwargs):
        self.recorder.update(kwargs)
        with open(path, "wb") as f:
            f.write(b"fake-image")

    async def close(self):
        return None


class FakeContext:
    def __init__(self, recorder: dict):
        self.recorder = recorder

    async def new_page(self):
        return FakePage(self.recorder)


def run_html2pic(tmp_path, monkeypatch, options: ScreenshotOptions) -> tuple[dict, list]:
    """在假浏览器上下文中执行一次 html2pic，返回截图参数与请求过的等级。"""
    monkeypatch.chdir(tmp_path)
    html_file = tmp_path / "page.html"
    html_file.write_text("<html><body>test</body></html>", encoding="utf-8")

    recorder: dict = {}
    levels: list[str] = []
    renderer = Text2ImgRender()

    async def fake_ensure_context(level):
        levels.append(level)
        return FakeContext(recorder)

    monkeypatch.setattr(renderer, "_ensure_context", fake_ensure_context)
    asyncio.run(renderer.html2pic(str(html_file), options))
    return recorder, levels


def test_low_jpeg_quality_is_raised_to_floor(tmp_path, monkeypatch):
    """AstrBot 默认传入 quality=40，应被抬升到下限以避免压缩失真。"""
    recorder, _ = run_html2pic(
        tmp_path,
        monkeypatch,
        ScreenshotOptions(type="jpeg", quality=40),
    )

    assert recorder["quality"] == render_module.MIN_JPEG_QUALITY


def test_missing_jpeg_quality_falls_back_to_floor(tmp_path, monkeypatch):
    recorder, _ = run_html2pic(tmp_path, monkeypatch, ScreenshotOptions(type="jpeg"))

    assert recorder["quality"] == render_module.MIN_JPEG_QUALITY


def test_high_jpeg_quality_is_preserved(tmp_path, monkeypatch):
    """高于下限的显式质量不应被改动。"""
    recorder, _ = run_html2pic(
        tmp_path,
        monkeypatch,
        ScreenshotOptions(type="jpeg", quality=95),
    )

    assert recorder["quality"] == 95


def test_png_never_receives_quality(tmp_path, monkeypatch):
    """PNG 传 quality 会让 Playwright 报错，必须剔除。"""
    recorder, _ = run_html2pic(
        tmp_path,
        monkeypatch,
        ScreenshotOptions(type="png", quality=40),
    )

    assert "quality" not in recorder


def test_default_scale_level_is_high(tmp_path, monkeypatch):
    """未指定 device_scale_factor_level 时使用 high(1.3x) 而非 normal(1.0x)。"""
    _, levels = run_html2pic(tmp_path, monkeypatch, ScreenshotOptions(type="jpeg"))

    assert levels == [render_module.DEFAULT_SCALE_LEVEL]
    assert render_module.DEFAULT_SCALE_LEVEL == "high"
    assert Text2ImgRender.SCALE_FACTOR_MAP[render_module.DEFAULT_SCALE_LEVEL] == 1.3


def test_explicit_scale_level_is_respected(tmp_path, monkeypatch):
    _, levels = run_html2pic(
        tmp_path,
        monkeypatch,
        ScreenshotOptions(type="jpeg", device_scale_factor_level="normal"),
    )

    assert levels == ["normal"]
