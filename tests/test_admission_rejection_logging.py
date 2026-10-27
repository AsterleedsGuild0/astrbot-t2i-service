import asyncio
import json
from contextlib import suppress
from dataclasses import replace
from types import SimpleNamespace

import pytest
from loguru import logger

from src import admission, limits
from src.diagnostics import RequestDiagnostics, current_diagnostics


BODY_SENTINEL = b"PRIVATE-ADMISSION-BODY-NOT-FOR-LOGS"


@pytest.fixture
def rejection_records():
    records = []
    sink = logger.add(lambda message: records.append(message.record), level="WARNING")
    try:
        yield records
    finally:
        logger.remove(sink)


@pytest.fixture(autouse=True)
def configured_limits(monkeypatch):
    configured = replace(
        limits.LIMITS,
        max_request_bytes=256,
        max_queue_size=1,
        queue_timeout=0.02,
        body_timeout=0.02,
        min_available_memory_bytes=2 * 1024 * 1024,
        min_free_disk_bytes=2 * 1024 * 1024,
    )
    monkeypatch.setattr(admission, "LIMITS", configured)
    monkeypatch.setattr(limits, "LIMITS", configured)
    monkeypatch.setattr(admission, "check_runtime_resources", lambda: None)
    return configured


def request_scope(headers=()):
    return {
        "type": "http",
        "method": "POST",
        "path": "/text2img/generate",
        "headers": list(headers),
    }


async def invoke(middleware, *, headers=(), chunks=(b"",), receive=None, scope=None):
    sent = []
    if receive is None:
        messages = iter(
            {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(chunks) - 1,
            }
            for index, chunk in enumerate(chunks)
        )

        async def receive():
            return next(messages, {"type": "http.disconnect"})

    async def send(message):
        sent.append(message)

    await middleware(scope if scope is not None else request_scope(headers), receive, send)
    return sent


async def unexpected_app(scope, receive, send):
    raise AssertionError("rejected request reached the application")


async def unread_body():
    raise AssertionError("request body was read before admission")


async def send_success(send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"accepted"})


def assert_response(sent, status, message, stage):
    start, body = sent
    assert start["type"] == "http.response.start"
    assert start["status"] == status
    headers = dict(start["headers"])
    if status == 503:
        assert headers[b"retry-after"] == b"5"
    else:
        assert b"retry-after" not in headers
    assert body["type"] == "http.response.body"
    assert json.loads(body["body"]) == {
        "code": 1,
        "message": message,
        "data": {"stage": stage},
    }


def assert_warning(
    records, *, status, stage, category, limit_name, limit_value,
    content_length=None, request_bytes=0, observed=None,
):
    assert len(records) == 1
    record = records[0]
    assert record["level"].name == "WARNING"
    fields = record["extra"]["resource_rejection"]
    assert fields == {
        "status": status,
        "stage": stage,
        "category": category,
        "limit_name": limit_name,
        "limit_value": limit_value,
        "observed": observed,
        "content_length": content_length,
        "request_bytes": request_bytes,
        "template_source_bytes": None,
        "template_input_bytes": None,
        "template_output_bytes": None,
        "html_bytes": None,
        "options": {},
    }
    message = record["message"]
    assert message.startswith("resource_rejection ")
    assert json.loads(message.removeprefix("resource_rejection ")) == fields
    assert "\n" not in message
    assert "\r" not in message
    assert BODY_SENTINEL.decode() not in message
    return fields


@pytest.mark.parametrize("length", [-1, 257])
def test_content_length_rejection_does_not_read_body(length, rejection_records):
    middleware = admission.RenderAdmissionMiddleware(unexpected_app)
    sent = asyncio.run(invoke(
        middleware,
        headers=[(b"content-length", str(length).encode())],
        receive=unread_body,
    ))

    assert_response(sent, 413, "Request exceeds T2I_MAX_REQUEST_BYTES", "request_body")
    assert_warning(
        rejection_records,
        status=413,
        stage="request_body",
        category="request_body",
        limit_name="T2I_MAX_REQUEST_BYTES",
        limit_value=256,
        observed=length,
        content_length=length,
    )
    assert middleware._pending == 0


@pytest.mark.parametrize("declared_length", [None, 1])
def test_chunk_rejection_counts_the_exceeding_chunk(
    declared_length, monkeypatch, rejection_records,
):
    monkeypatch.setattr(admission, "LIMITS", replace(admission.LIMITS, max_request_bytes=8))
    middleware = admission.RenderAdmissionMiddleware(unexpected_app)
    headers = [] if declared_length is None else [(b"content-length", b"1")]
    sent = asyncio.run(invoke(middleware, headers=headers, chunks=(b"ab", BODY_SENTINEL)))
    size = 2 + len(BODY_SENTINEL)

    assert_response(sent, 413, "Request exceeds T2I_MAX_REQUEST_BYTES", "request_body")
    assert_warning(
        rejection_records,
        status=413,
        stage="request_body",
        category="request_body",
        limit_name="T2I_MAX_REQUEST_BYTES",
        limit_value=8,
        observed=size,
        content_length=declared_length,
        request_bytes=size,
    )
    assert middleware._pending == 0
    assert not middleware._slot.locked()


@pytest.mark.parametrize("initial_chunk", [b"", BODY_SENTINEL])
def test_body_timeout_reports_only_bytes_received(initial_chunk, rejection_records):
    async def scenario():
        delivered = False
        never = asyncio.Event()

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": initial_chunk, "more_body": True}
            await never.wait()
            raise AssertionError("body timeout did not cancel the read")

        middleware = admission.RenderAdmissionMiddleware(unexpected_app)
        sent = await invoke(middleware, headers=[(b"content-length", b"200")], receive=receive)
        assert middleware._pending == 0
        assert not middleware._slot.locked()
        return sent

    sent = asyncio.run(scenario())
    assert_response(sent, 408, "Timed out reading the request body", "request_body")
    assert_warning(
        rejection_records,
        status=408,
        stage="request_body",
        category="request_body_timeout",
        limit_name="T2I_BODY_TIMEOUT_SECONDS",
        limit_value=0.02,
        content_length=200,
        request_bytes=len(initial_chunk),
    )


@pytest.mark.parametrize(
    "capacity,category,message,limit_name,limit_value,observed",
    [
        (0, "queue_full", "Render queue is full", "T2I_MAX_QUEUE_SIZE", 0, 1),
        (
            1, "queue_timeout", "Timed out waiting for the render slot",
            "T2I_QUEUE_TIMEOUT_SECONDS", 0.02, None,
        ),
    ],
)
def test_queue_rejections_do_not_read_or_parse_body(
    capacity, category, message, limit_name, limit_value, observed,
    monkeypatch, rejection_records,
):
    monkeypatch.setattr(admission, "LIMITS", replace(admission.LIMITS, max_queue_size=capacity))

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def app(scope, receive, send):
            entered.set()
            await release.wait()
            await send_success(send)

        middleware = admission.RenderAdmissionMiddleware(app)
        first = asyncio.create_task(invoke(middleware, chunks=(b"first",)))
        try:
            await entered.wait()
            sent = await invoke(
                middleware, headers=[(b"content-length", b"200")], receive=unread_body,
            )
        finally:
            release.set()
            await first
        assert middleware._pending == 0
        assert not middleware._slot.locked()
        return sent

    sent = asyncio.run(scenario())
    assert_response(sent, 503, message, "queue")
    assert_warning(
        rejection_records,
        status=503,
        stage="queue",
        category=category,
        limit_name=limit_name,
        limit_value=limit_value,
        observed=observed,
        content_length=200,
    )


@pytest.mark.parametrize(
    "resource,status,category,message,limit_name,limit_value,observed",
    [
        (
            "container", 503, "container_memory", "Container memory is above 90% of its limit",
            "cgroup_memory_ratio", 0.9, 0.95,
        ),
        (
            "host", 503, "host_memory", "Host available memory is below the safety reserve",
            "T2I_MIN_AVAILABLE_MEMORY_MB", 2, 1.0,
        ),
        (
            "disk", 507, "disk_reserve", "Insufficient free disk space for rendering",
            "T2I_MIN_FREE_DISK_MB", 2, 1.0,
        ),
    ],
)
def test_runtime_resource_rejections_log_once_at_admission(
    resource, status, category, message, limit_name, limit_value, observed,
    monkeypatch, rejection_records,
):
    original_read_text = limits.Path.read_text

    def cgroup_number(path):
        if resource == "container":
            return 950 if path.endswith("memory.current") else 1000
        return None

    def read_meminfo(path, *args, **kwargs):
        if str(path) != "/proc/meminfo":
            return original_read_text(path, *args, **kwargs)
        return "MemAvailable: 1024 kB\n" if resource == "host" else "MemAvailable: 4096 kB\n"

    monkeypatch.setattr(limits, "_read_cgroup_number", cgroup_number)
    monkeypatch.setattr(limits.Path, "read_text", read_meminfo)
    monkeypatch.setattr(limits.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024 * 1024))
    monkeypatch.setattr(admission, "check_runtime_resources", limits.check_runtime_resources)
    middleware = admission.RenderAdmissionMiddleware(unexpected_app)
    sent = asyncio.run(invoke(
        middleware, headers=[(b"content-length", b"200")], receive=unread_body,
    ))

    assert_response(sent, status, message, "resource_limit")
    assert_warning(
        rejection_records,
        status=status,
        stage="admission",
        category=category,
        limit_name=limit_name,
        limit_value=limit_value,
        observed=observed,
        content_length=200,
    )
    assert middleware._pending == 0
    assert not middleware._slot.locked()


def test_invalid_content_length_is_not_a_resource_warning(rejection_records):
    async def scenario():
        previous = RequestDiagnostics(request_bytes=123, options={"type": "jpeg"})
        token = current_diagnostics.set(previous)
        try:
            middleware = admission.RenderAdmissionMiddleware(unexpected_app)
            sent = await invoke(
                middleware,
                headers=[(b"content-length", b"PRIVATE-INVALID-HEADER-NOT-FOR-LOGS")],
                receive=unread_body,
            )
            assert current_diagnostics.get() is previous
            assert middleware._pending == 0
            return sent
        finally:
            current_diagnostics.reset(token)

    sent = asyncio.run(scenario())
    assert_response(sent, 400, "Invalid Content-Length", "request_body")
    assert rejection_records == []


@pytest.mark.parametrize("declared_length", [None, len(BODY_SENTINEL)])
def test_admitted_body_sizes_remain_available_and_context_does_not_leak(
    declared_length, rejection_records,
):
    async def scenario():
        previous = RequestDiagnostics(request_bytes=999, options={"type": "jpeg"})
        token = current_diagnostics.set(previous)
        seen = []

        async def app(scope, receive, send):
            diagnostics = current_diagnostics.get()
            assert diagnostics is not None and diagnostics is not previous
            assert diagnostics.options == {}
            seen.append(diagnostics)
            message = await receive()
            assert message == {
                "type": "http.request", "body": BODY_SENTINEL, "more_body": False,
            }
            diagnostics.options = {"type": "png"}
            await send_success(send)

        middleware = admission.RenderAdmissionMiddleware(app)
        headers = [] if declared_length is None else [
            (b"content-length", str(declared_length).encode()),
        ]
        try:
            sent = await invoke(middleware, headers=headers, chunks=(BODY_SENTINEL[:3], BODY_SENTINEL[3:]))
            assert sent[0]["status"] == 200
            assert current_diagnostics.get() is previous
            assert seen[0].content_length == declared_length
            assert seen[0].request_bytes == len(BODY_SENTINEL)
            assert previous.request_bytes == 999
            rejected = await invoke(
                middleware, headers=[(b"content-length", b"257")], receive=unread_body,
            )
            assert current_diagnostics.get() is previous
            return rejected
        finally:
            current_diagnostics.reset(token)

    rejected = asyncio.run(scenario())
    assert_response(rejected, 413, "Request exceeds T2I_MAX_REQUEST_BYTES", "request_body")
    assert_warning(
        rejection_records,
        status=413,
        stage="request_body",
        category="request_body",
        limit_name="T2I_MAX_REQUEST_BYTES",
        limit_value=256,
        observed=257,
        content_length=257,
    )


@pytest.mark.parametrize("outcome", ["cancel", "disconnect", "app_error"])
def test_context_and_admission_slot_are_restored_on_interruption(outcome, rejection_records):
    async def scenario():
        previous = RequestDiagnostics(request_bytes=123)
        token = current_diagnostics.set(previous)
        reading = asyncio.Event()
        never = asyncio.Event()
        restored = []

        async def receive():
            if outcome == "disconnect":
                return {"type": "http.disconnect"}
            if outcome == "app_error":
                return {"type": "http.request", "body": BODY_SENTINEL, "more_body": False}
            reading.set()
            await never.wait()
            raise AssertionError("cancelled request resumed")

        async def app(scope, receive, send):
            raise RuntimeError("application failed")

        middleware = admission.RenderAdmissionMiddleware(app)

        async def run_request():
            try:
                return await invoke(middleware, receive=receive)
            finally:
                restored.append(current_diagnostics.get())

        try:
            if outcome == "cancel":
                task = asyncio.create_task(run_request())
                try:
                    await reading.wait()
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                assert task.cancelled()
            elif outcome == "app_error":
                with pytest.raises(RuntimeError, match="application failed"):
                    await run_request()
            else:
                assert await run_request() == []
            assert restored == [previous]
            assert current_diagnostics.get() is previous
            assert middleware._pending == 0
            assert not middleware._slot.locked()
        finally:
            current_diagnostics.reset(token)

    asyncio.run(scenario())
    assert rejection_records == []
