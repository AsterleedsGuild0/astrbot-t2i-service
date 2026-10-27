import asyncio
import json
import signal
import sys
from pathlib import Path

from jinja2.exceptions import SecurityError

from .diagnostics import record_sizes
from .limits import LIMITS, ResourceLimitError, check_html_size


async def render_template(template: str, data: dict) -> str:
    check_html_size(template, template=True)
    payload = json.dumps({"template": template, "data": data}, ensure_ascii=False).encode("utf-8")
    record_sizes(template_input_bytes=len(payload))
    if len(payload) > LIMITS.max_request_bytes:
        raise ResourceLimitError(
            "Template worker input exceeds T2I_MAX_REQUEST_BYTES",
            category="template_input", limit_name="T2I_MAX_REQUEST_BYTES",
            limit_value=LIMITS.max_request_bytes, observed=len(payload),
            rejection_stage="template_input",
        )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "src.template_worker",
        cwd=Path(__file__).resolve().parent.parent,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload), LIMITS.template_timeout
            )
        except TimeoutError as error:
            raise ResourceLimitError(
                "Template exceeded T2I_TEMPLATE_TIMEOUT_SECONDS", 504,
                category="template_timeout", limit_name="T2I_TEMPLATE_TIMEOUT_SECONDS",
                limit_value=LIMITS.template_timeout, rejection_stage="template_worker",
            ) from error
        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace")[:400]
            if message.startswith("SECURITY:"):
                raise SecurityError(message)
            public_limit_message = "LIMIT: Template exceeded its memory or output budget"
            if message.startswith(("LIMIT:INPUT:", "LIMIT:OUTPUT:")):
                _, marker, limit_text, observed_text = message.split(":")
                worker_limit = int(limit_text)
                observed = int(observed_text)
                is_output = marker == "OUTPUT"
                if is_output:
                    record_sizes(template_output_bytes=observed)
                raise ResourceLimitError(
                    public_limit_message,
                    category="template_output" if is_output else "template_input",
                    limit_name="T2I_MAX_HTML_BYTES" if is_output else "T2I_MAX_REQUEST_BYTES",
                    limit_value=worker_limit,
                    observed=observed,
                    rejection_stage="template_output" if is_output else "template_input",
                )
            if message.startswith("LIMIT:MEMORY:"):
                raise ResourceLimitError(
                    public_limit_message, category="template_memory",
                    limit_name="T2I_TEMPLATE_MEMORY_MB",
                    limit_value=float(message.removeprefix("LIMIT:MEMORY:")),
                    rejection_stage="template_worker",
                )
            if process.returncode < 0:
                worker_signal = -process.returncode
                is_cpu = worker_signal == getattr(signal, "SIGXCPU", None)
                raise ResourceLimitError(
                    message or "Template worker exceeded its resource budget",
                    category="template_cpu" if is_cpu else "template_worker_signal",
                    limit_name="T2I_TEMPLATE_TIMEOUT_SECONDS" if is_cpu else "worker_exit_code",
                    limit_value=LIMITS.template_timeout if is_cpu else 0,
                    observed=None if is_cpu else process.returncode,
                    rejection_stage="template_worker",
                )
            raise ValueError(message or "Template rendering failed")
        record_sizes(template_output_bytes=len(stdout))
        if len(stdout) > LIMITS.max_html_bytes:
            raise ResourceLimitError(
                "Generated HTML exceeds T2I_MAX_HTML_BYTES", category="template_output",
                limit_name="T2I_MAX_HTML_BYTES", limit_value=LIMITS.max_html_bytes,
                observed=len(stdout), rejection_stage="template_output",
            )
        return stdout.decode("utf-8")
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.wait_for(process.wait(), LIMITS.cleanup_timeout)
