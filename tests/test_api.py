import asyncio
import json
from contextlib import suppress
from pathlib import Path

from starlette.requests import Request

from src import api
from src.render import RenderError
from src.storage import StoredImage


class FakeRenderer:
    def __init__(self, output_path: Path):
        self.output_path = output_path

    async def from_html(self, html: str):
        return "data/rendered_test.html", str(self.output_path.with_suffix(".html"))

    async def html2pic(self, html_file_path, screenshot_options):
        self.output_path.write_bytes(b"png")
        return str(self.output_path)


class FakeStorage:
    def __init__(self):
        self.saved_path: str | None = None

    async def save(self, image_path: str) -> str:
        self.saved_path = image_path
        return "rendered_test.png"

    async def get(self, image_id: str):
        if image_id == "rendered_test.png":
            return StoredImage(content=b"png", media_type="image/png")
        return None


class FailingRenderer(FakeRenderer):
    async def html2pic(self, html_file_path, screenshot_options):
        raise RenderError("goto", "page.goto failed: Timeout")


def test_generate_json_response_remains_compatible(tmp_path, monkeypatch):
    output_path = tmp_path / "rendered_test.png"
    storage = FakeStorage()
    monkeypatch.setattr(api, "render", FakeRenderer(output_path))
    monkeypatch.setattr(api, "image_storage", storage)

    response = asyncio.run(
        api.text2img(
            api.GenerateRequest(
                html="<html><body>test</body></html>",
                json=True,
            )
        )
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "code": 0,
        "message": "success",
        "data": {"id": "data/rendered_test.png"},
    }
    assert storage.saved_path == str(output_path)


def test_get_image_response_remains_compatible(monkeypatch):
    monkeypatch.setattr(api, "image_storage", FakeStorage())

    response = asyncio.run(api.text2img_image("rendered_test.png"))

    assert response.status_code == 200
    assert response.body == b"png"
    assert response.media_type == "image/png"


def test_get_missing_image_response_remains_compatible(monkeypatch):
    monkeypatch.setattr(api, "image_storage", FakeStorage())

    response = asyncio.run(api.text2img_image("missing.png"))

    assert response.status_code == 404
    assert json.loads(response.body) == {
        "code": 1,
        "message": "file not found",
        "data": {},
    }


def test_generate_render_error_returns_structured_payload(tmp_path, monkeypatch):
    output_path = tmp_path / "rendered_test.png"
    monkeypatch.setattr(api, "render", FailingRenderer(output_path))

    response = asyncio.run(
        api.text2img(
            api.GenerateRequest(
                html="<html><body>test</body></html>",
                json=True,
            )
        )
    )

    assert response.status_code == 500
    assert json.loads(response.body) == {
        "code": 1,
        "message": "render error: page.goto failed: Timeout",
        "data": {
            "error_type": "RenderError",
            "html_file": "rendered_test.html",
            "options": {
                "wait_until": "domcontentloaded",
                "type": "png",
                "full_page": True,
                "scale": "device",
            },
            "stage": "goto",
        },
    }


class WatchedRenderer:
    """Renderer stub whose result is released by the test; tracks cancellation."""

    def __init__(self, release: asyncio.Event):
        self.release = release
        self.cancelled = False
        self._unhealthy = False

    async def html2pic(self, html_file_path, screenshot_options):
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return "data/rendered_test.png"


def starlette_request(receive):
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive)


def test_watcher_that_swallows_cancellation_does_not_latch_unhealthy(monkeypatch):
    """Regression: Starlette's is_disconnected() can swallow task.cancel() (anyio CancelScope).

    A successful render must still return promptly and leave the service healthy.
    """

    async def scenario():
        release, finish = asyncio.Event(), asyncio.Event()

        class StuckRequest:
            async def is_disconnected(self):
                # Ignores cancellation until the test lets it go, like the swallowed case.
                release.set()
                while not finish.is_set():
                    with suppress(asyncio.CancelledError):
                        await asyncio.sleep(0.05)
                return False

        renderer = WatchedRenderer(release)
        monkeypatch.setattr(api, "render", renderer)
        try:
            result = await asyncio.wait_for(
                asyncio.shield(api.render_with_disconnect(StuckRequest(), "page.html", None)), 3
            )
        finally:
            finish.set()
            await asyncio.sleep(0.3)
        return result, renderer

    result, renderer = asyncio.run(scenario())
    assert result == "data/rendered_test.png"
    assert renderer._unhealthy is False


def test_client_disconnect_cancels_render_and_keeps_service_healthy(monkeypatch):
    async def scenario():
        async def receive():
            return {"type": "http.disconnect"}

        renderer = WatchedRenderer(asyncio.Event())
        monkeypatch.setattr(api, "render", renderer)
        try:
            await asyncio.wait_for(
                api.render_with_disconnect(starlette_request(receive), "page.html", None), 2
            )
        except RenderError as error:
            return error, renderer
        raise AssertionError("disconnect was not reported")

    error, renderer = asyncio.run(scenario())
    assert (error.stage, error.status_code) == ("client_disconnect", 499)
    assert renderer.cancelled is True
    assert renderer._unhealthy is False
