"""Render Jinja in a disposable process, not in the API event loop."""

import json
import sys

from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment

from .limits import LIMITS, ResourceLimitError


def main() -> int:
    try:
        try:
            import resource
        except ImportError:
            resource = None
        if resource is not None:
            for kind, budget in (
                (resource.RLIMIT_AS, LIMITS.template_memory_bytes),
                (resource.RLIMIT_CPU, LIMITS.template_timeout),
            ):
                _, hard = resource.getrlimit(kind)
                limit = budget if hard == resource.RLIM_INFINITY else min(hard, budget)
                resource.setrlimit(kind, (limit, limit))

        raw = sys.stdin.buffer.read(LIMITS.max_request_bytes + 1)
        if len(raw) > LIMITS.max_request_bytes:
            raise ResourceLimitError("Template worker input exceeds request limit")
        payload = json.loads(raw)
        output = bytearray()
        template = SandboxedEnvironment().from_string(payload["template"])
        for chunk in template.generate(payload["data"]):
            encoded = chunk.encode("utf-8")
            if len(output) + len(encoded) > LIMITS.max_html_bytes:
                raise ResourceLimitError("Generated HTML exceeds T2I_MAX_HTML_BYTES")
            output.extend(encoded)
        sys.stdout.buffer.write(output)
        return 0
    except SecurityError:
        sys.stderr.write("SECURITY: Template violates sandbox restrictions")
    except (MemoryError, ResourceLimitError):
        sys.stderr.write("LIMIT: Template exceeded its memory or output budget")
    except Exception as error:
        sys.stderr.write(f"ERROR: {type(error).__name__}: {str(error)[:300]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
