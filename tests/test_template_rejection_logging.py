import asyncio
import json
import sys
from dataclasses import replace

import pytest
from loguru import logger

from src import api, diagnostics, limits, template


SECRET = "PRIVATE_TEMPLATE_PAYLOAD_7dd45\nPRIVATE_SECOND_LINE"
PUBLIC_WORKER_LIMIT = "LIMIT: Template exceeded its memory or output budget"


class TemplateRenderer:
    async def from_jinja_template(self, source, data):
        await template.render_template(source, data)
        pytest.fail("A rejected template must not reach the browser")


@pytest.fixture
def rejection_records():
    records = []
    sink = logger.add(lambda message: records.append(message.record), level="WARNING")
    try:
        yield records
    finally:
        logger.remove(sink)


@pytest.fixture
def budgets(monkeypatch):
    configured = replace(
        limits.LIMITS,
        max_request_bytes=8192,
        max_html_bytes=1024,
        template_timeout=10,
        template_memory_bytes=1024 * 1024 * 1024,
    )
    for module in (limits, template, api):
        monkeypatch.setattr(module, "LIMITS", configured)
    # Children re-read the inherited environment, not monkeypatched module globals.
    monkeypatch.setenv("T2I_MAX_REQUEST_BYTES", str(configured.max_request_bytes))
    monkeypatch.setenv("T2I_MAX_HTML_BYTES", str(configured.max_html_bytes))
    monkeypatch.setenv("T2I_TEMPLATE_TIMEOUT_SECONDS", str(configured.template_timeout))
    monkeypatch.setenv("T2I_TEMPLATE_MEMORY_MB", "1024")
    monkeypatch.setattr(api, "check_runtime_resources", lambda: None)
    monkeypatch.setattr(api, "rate_limit_max_requests", 0)
    monkeypatch.setattr(api, "render", TemplateRenderer())
    return configured


def payload_bytes(source, data):
    return len(json.dumps({"template": source, "data": data}, ensure_ascii=False).encode("utf-8"))


def invoke(source, data):
    body = {
        "tmpl": source,
        "tmpldata": data,
        "options": {"path": SECRET, "mask": [{"selector": SECRET}], "type": "png", "wait_until": "load"},
    }
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    context = diagnostics.RequestDiagnostics(content_length=len(raw), request_bytes=len(raw))
    token = diagnostics.current_diagnostics.set(context)
    try:
        response = asyncio.run(api.text2img(api.GenerateRequest(**body)))
    finally:
        diagnostics.current_diagnostics.reset(token)
    return response, context


def assert_rejection(records, response, context, *, category, stage, name, value, observed,
                     source, input_bytes=None, output_bytes=None, status=413, message=None):
    assert response.status_code == status
    body = json.loads(response.body)
    assert body["data"]["stage"] == "resource_limit"
    if message is not None:
        assert body["message"] == message
    assert len(records) == 1
    record = records[0]
    assert record["level"].name == "WARNING"
    fields = record["extra"]["resource_rejection"]
    assert fields["status"] == status
    assert fields["category"] == category
    assert fields["stage"] == stage
    assert fields["limit_name"] == name
    assert fields["limit_value"] == value
    assert fields["observed"] == observed
    assert fields["request_bytes"] == context.request_bytes
    assert fields["content_length"] == context.content_length
    assert fields["template_source_bytes"] == len(source.encode("utf-8"))
    assert fields["template_input_bytes"] == input_bytes
    assert fields["template_output_bytes"] == output_bytes
    assert fields["options"]["type"] == "png"
    assert fields["options"]["wait_until"] == "load"
    assert "path" not in fields["options"]
    assert "mask" not in fields["options"]
    assert "PRIVATE_TEMPLATE_PAYLOAD_7dd45" not in str(record)
    assert "PRIVATE_SECOND_LINE" not in str(record)
    assert "\n" not in record["message"]


def test_source_refusal_counts_utf8_and_logs_once(budgets, rejection_records):
    source = SECRET + "群" * budgets.max_html_bytes
    response, context = invoke(source, {"private": SECRET})
    assert_rejection(
        rejection_records, response, context, category="template_input", stage="template_input",
        name="T2I_MAX_HTML_BYTES", value=budgets.max_html_bytes,
        observed=len(source.encode("utf-8")), source=source,
        message="HTML or template exceeds T2I_MAX_HTML_BYTES",
    )


def test_serialized_input_refusal(budgets, rejection_records):
    source = "{{ value }}"
    data = {"value": SECRET * budgets.max_request_bytes}
    raw = json.dumps({"template": source, "data": data}, ensure_ascii=False).encode("utf-8")
    response, context = invoke(source, data)
    assert_rejection(
        rejection_records, response, context, category="template_input", stage="template_input",
        name="T2I_MAX_REQUEST_BYTES", value=budgets.max_request_bytes,
        observed=len(raw), source=source, input_bytes=len(raw),
        message="Template worker input exceeds T2I_MAX_REQUEST_BYTES",
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Worker address-space budgets require Linux")
def test_real_worker_output_refusal_includes_failed_chunk(monkeypatch, budgets, rejection_records):
    worker_limit = budgets.max_html_bytes // 2
    monkeypatch.setenv("T2I_MAX_HTML_BYTES", str(worker_limit))
    source = "{{ private }}{% for n in range(100) %}{{ piece }}{% endfor %}"
    data = {"private": SECRET, "piece": "群" * 100}
    prefix_bytes = len(SECRET.encode("utf-8"))
    chunk_bytes = len(data["piece"].encode("utf-8"))
    failed_size = prefix_bytes + ((worker_limit - prefix_bytes) // chunk_bytes + 1) * chunk_bytes
    response, context = invoke(source, data)
    assert_rejection(
        rejection_records, response, context, category="template_output", stage="template_output",
        name="T2I_MAX_HTML_BYTES", value=worker_limit, observed=failed_size,
        source=source, input_bytes=payload_bytes(source, data), output_bytes=failed_size,
        message=PUBLIC_WORKER_LIMIT,
    )


def launch_controlled_worker(monkeypatch, action):
    original = asyncio.create_subprocess_exec
    script = (
        "from src import template_worker as worker\n"
        "try:\n"
        "    import resource\n"
        "    initial_cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)[1]\n"
        "    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
        "except ImportError:\n"
        "    resource = None\n"
        "def controlled_source(self, source):\n"
        "    if resource is not None:\n"
        "        budget = worker.LIMITS.template_timeout\n"
        "        soft = budget if initial_cpu_hard == resource.RLIM_INFINITY else min(initial_cpu_hard, budget)\n"
        "        hard = soft + 1 if initial_cpu_hard == resource.RLIM_INFINITY else min(initial_cpu_hard, soft + 1)\n"
        "        assert resource.getrlimit(resource.RLIMIT_CPU) == (soft, hard)\n"
        f"    {action}\n"
        "worker.SandboxedEnvironment.from_string = controlled_source\n"
        "raise SystemExit(worker.main())\n"
    )

    async def launch(*args, **kwargs):
        return await original(sys.executable, "-c", script, **kwargs)

    monkeypatch.setattr(template.asyncio, "create_subprocess_exec", launch)


@pytest.mark.skipif(sys.platform != "linux", reason="Worker address-space budgets require Linux")
def test_real_worker_memory_error_marker_is_content_free(monkeypatch, budgets, rejection_records):
    worker_memory_mb = 768
    monkeypatch.setenv("T2I_TEMPLATE_MEMORY_MB", str(worker_memory_mb))
    launch_controlled_worker(monkeypatch, f"raise MemoryError({SECRET!r})")
    source, data = "{{ private }}", {"private": SECRET}
    response, context = invoke(source, data)
    assert_rejection(
        rejection_records, response, context, category="template_memory", stage="template_worker",
        name="T2I_TEMPLATE_MEMORY_MB", value=worker_memory_mb,
        observed=None, source=source, input_bytes=payload_bytes(source, data),
        message=PUBLIC_WORKER_LIMIT,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Worker address-space budgets require Linux")
def test_real_worker_cpu_signal_is_not_memory(monkeypatch, budgets, rejection_records):
    launch_controlled_worker(monkeypatch, "import os, signal; os.kill(os.getpid(), signal.SIGXCPU)")
    source, data = "{{ private }}", {"private": SECRET}
    response, context = invoke(source, data)
    assert_rejection(
        rejection_records, response, context, category="template_cpu", stage="template_worker",
        name="T2I_TEMPLATE_TIMEOUT_SECONDS", value=budgets.template_timeout,
        observed=None, source=source, input_bytes=payload_bytes(source, data),
        message="Template worker exceeded its resource budget",
    )


class ControlledProcess:
    def __init__(self, returncode=None, stdout=b"", stderr=b"", timeout=False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timeout = timeout
        self.killed = False
        self.waited = False

    async def communicate(self, payload):
        if self.timeout:
            raise TimeoutError
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        self.waited = True
        return self.returncode


def install_process(monkeypatch, process):
    async def launch(*args, **kwargs):
        return process

    monkeypatch.setattr(template.asyncio, "create_subprocess_exec", launch)


@pytest.mark.skipif(sys.platform != "linux", reason="Worker address-space budgets require Linux")
def test_real_worker_input_limit_is_reported_not_parent_limit(monkeypatch, budgets, rejection_records):
    source, data = "{{ private }}", {"private": SECRET}
    size = payload_bytes(source, data)
    worker_limit = size - 1
    # A stricter inherited worker budget exercises its own bounded read, while
    # the HTTP process admits the serialized input.
    monkeypatch.setenv("T2I_MAX_REQUEST_BYTES", str(worker_limit))
    response, context = invoke(source, data)
    assert_rejection(
        rejection_records, response, context, category="template_input", stage="template_input",
        name="T2I_MAX_REQUEST_BYTES", value=worker_limit,
        observed=size, source=source, input_bytes=size, message=PUBLIC_WORKER_LIMIT,
    )


def test_sigkill_does_not_guess_oom_or_cpu(monkeypatch, budgets, rejection_records):
    install_process(monkeypatch, ControlledProcess(returncode=-9))
    source, data = "{{ private }}", {"private": SECRET}
    response, context = invoke(source, data)
    assert_rejection(
        rejection_records, response, context, category="template_worker_signal", stage="template_worker",
        name="worker_exit_code", value=0, observed=-9,
        source=source, input_bytes=payload_bytes(source, data),
        message="Template worker exceeded its resource budget",
    )


def test_wall_clock_timeout_logs_once_and_reaps_worker(monkeypatch, budgets, rejection_records):
    process = ControlledProcess(timeout=True)
    install_process(monkeypatch, process)
    source, data = "{{ private }}", {"private": SECRET}
    response, context = invoke(source, data)
    assert process.killed and process.waited
    assert_rejection(
        rejection_records, response, context, category="template_timeout", stage="template_worker",
        name="T2I_TEMPLATE_TIMEOUT_SECONDS", value=budgets.template_timeout, observed=None,
        source=source, input_bytes=payload_bytes(source, data), status=504,
        message="Template exceeded T2I_TEMPLATE_TIMEOUT_SECONDS",
    )


def test_parent_output_guard_records_size(monkeypatch, budgets, rejection_records):
    output = (SECRET + "群" * budgets.max_html_bytes).encode("utf-8")
    install_process(monkeypatch, ControlledProcess(returncode=0, stdout=output))
    source, data = "{{ private }}", {"private": SECRET}
    response, context = invoke(source, data)
    assert_rejection(
        rejection_records, response, context, category="template_output", stage="template_output",
        name="T2I_MAX_HTML_BYTES", value=budgets.max_html_bytes, observed=len(output),
        source=source, input_bytes=payload_bytes(source, data), output_bytes=len(output),
        message="Generated HTML exceeds T2I_MAX_HTML_BYTES",
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Worker address-space budgets require Linux")
def test_successful_worker_sizes_survive_later_screenshot_refusal(monkeypatch, tmp_path, budgets, rejection_records):
    source, data = "<p>{{ private }}</p>", {"private": SECRET}
    output = f"<p>{SECRET}</p>"

    class ScreenshotRefusalRenderer:
        async def from_jinja_template(self, source, data):
            result = await template.render_template(source, data)
            path = tmp_path / "rendered.html"
            path.write_text(result, encoding="utf-8")
            return path.name, str(path)

        async def html2pic(self, path, options):
            limits.check_screenshot_size(budgets.max_screenshot_dimension + 1, 1, 1)

    monkeypatch.setattr(api, "render", ScreenshotRefusalRenderer())
    response, context = invoke(source, data)
    assert_rejection(
        rejection_records, response, context, category="screenshot_dimension", stage="screenshot",
        name="T2I_MAX_SCREENSHOT_DIMENSION", value=budgets.max_screenshot_dimension,
        observed=budgets.max_screenshot_dimension + 1, source=source,
        input_bytes=payload_bytes(source, data), output_bytes=len(output.encode("utf-8")),
        message="render error: Screenshot exceeds T2I_MAX_SCREENSHOT_DIMENSION",
    )
    error_data = json.loads(response.body)["data"]
    assert error_data["error_type"] == "ResourceLimitError"
    assert error_data["html_file"] == "rendered.html"
