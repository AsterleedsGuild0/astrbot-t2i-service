"""Render Jinja in a disposable process, not in the API event loop."""

import json
import sys

from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment

from .limits import LIMITS


def main() -> int:
    memory_limit = LIMITS.template_memory_bytes
    try:
        try:
            import resource
        except ImportError:
            resource = None
        if resource is not None:
            _, hard = resource.getrlimit(resource.RLIMIT_AS)
            memory_limit = (
                LIMITS.template_memory_bytes
                if hard == resource.RLIM_INFINITY
                else min(hard, LIMITS.template_memory_bytes)
            )
            resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
            _, hard = resource.getrlimit(resource.RLIMIT_CPU)
            cpu_soft = (
                LIMITS.template_timeout
                if hard == resource.RLIM_INFINITY
                else min(hard, LIMITS.template_timeout)
            )
            cpu_hard = (
                cpu_soft + 1
                if hard == resource.RLIM_INFINITY
                else min(hard, cpu_soft + 1)
            )
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_soft, cpu_hard))

        raw = sys.stdin.buffer.read(LIMITS.max_request_bytes + 1)
        if len(raw) > LIMITS.max_request_bytes:
            sys.stderr.write(f"LIMIT:INPUT:{LIMITS.max_request_bytes}:{len(raw)}")
            return 1
        payload = json.loads(raw)
        output = bytearray()
        template = SandboxedEnvironment().from_string(payload["template"])
        for chunk in template.generate(payload["data"]):
            encoded = chunk.encode("utf-8")
            output_size = len(output) + len(encoded)
            if output_size > LIMITS.max_html_bytes:
                sys.stderr.write(f"LIMIT:OUTPUT:{LIMITS.max_html_bytes}:{output_size}")
                return 1
            output.extend(encoded)
        sys.stdout.buffer.write(output)
        return 0
    except SecurityError:
        sys.stderr.write("SECURITY: Template violates sandbox restrictions")
    except MemoryError:
        sys.stderr.write(f"LIMIT:MEMORY:{memory_limit / (1024 * 1024)}")
    except Exception as error:
        sys.stderr.write(f"ERROR: {type(error).__name__}: {str(error)[:300]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
