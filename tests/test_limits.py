from dataclasses import replace

import pytest

from src import limits
from src import render as render_module
from src.render import ScreenshotOptions, Text2ImgRender


def test_pixel_budget_includes_device_scale(monkeypatch):
    monkeypatch.setattr(limits, "LIMITS", replace(limits.LIMITS, max_screenshot_pixels=10000))
    assert limits.check_screenshot_size(100, 100, 1) == 10000
    with pytest.raises(limits.ResourceLimitError):
        limits.check_screenshot_size(100, 100, 1.3)


@pytest.mark.parametrize("dimension", [0, -1, float("inf"), float("nan")])
def test_non_positive_or_non_finite_dimensions_are_rejected(dimension):
    with pytest.raises(limits.ResourceLimitError):
        limits.check_screenshot_size(100, dimension, 1)


def test_single_dimension_is_limited_even_for_narrow_images(monkeypatch):
    monkeypatch.setattr(limits, "LIMITS", replace(limits.LIMITS, max_screenshot_dimension=100))
    with pytest.raises(limits.ResourceLimitError):
        limits.check_screenshot_size(1, 101, 1)


def test_html_limit_counts_utf8_bytes(monkeypatch):
    monkeypatch.setattr(limits, "LIMITS", replace(limits.LIMITS, max_html_bytes=3))
    limits.check_html_size("群")
    with pytest.raises(limits.ResourceLimitError):
        limits.check_html_size("群聊")


def test_scale_and_timeout_cannot_bypass_server_limits(monkeypatch):
    monkeypatch.setattr(render_module, "LIMITS", replace(limits.LIMITS, max_scale_level="normal"))
    renderer = Text2ImgRender()
    requested = ScreenshotOptions(device_scale_factor_level="ultra", timeout=0)
    effective = renderer._effective_options(requested)
    assert effective.device_scale_factor_level == "normal"
    assert effective.timeout == render_module.LIMITS.operation_timeout_ms
    assert effective.animations == "disabled"
    assert requested.timeout == 0
    assert requested.device_scale_factor_level == "ultra"
