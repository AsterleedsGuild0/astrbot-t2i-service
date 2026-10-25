import asyncio
import json
import sys
from pathlib import Path

from jinja2.exceptions import SecurityError

from .limits import LIMITS, ResourceLimitError, check_html_size


async def render_template(template: str, data: dict) -> str:
    check_html_size(template)
    payload = json.dumps({"template": template, "data": data}, ensure_ascii=False).encode("utf-8")
    if len(payload) > LIMITS.max_request_bytes:
        raise ResourceLimitError("Template worker input exceeds T2I_MAX_REQUEST_BYTES")
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
            raise ResourceLimitError("Template exceeded T2I_TEMPLATE_TIMEOUT_SECONDS", 504) from error
        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace")[:400]
            if message.startswith("SECURITY:"):
                raise SecurityError(message)
            if message.startswith("LIMIT:") or process.returncode < 0:
                raise ResourceLimitError(message or "Template worker exceeded its resource budget")
            raise ValueError(message or "Template rendering failed")
        if len(stdout) > LIMITS.max_html_bytes:
            raise ResourceLimitError("Generated HTML exceeds T2I_MAX_HTML_BYTES")
        return stdout.decode("utf-8")
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.wait_for(process.wait(), LIMITS.cleanup_timeout)
