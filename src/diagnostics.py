"""Content-free, request-local diagnostics for resource rejections."""

import json
from contextvars import ContextVar
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class RequestDiagnostics:
    content_length: int | None = None
    request_bytes: int | None = None
    template_source_bytes: int | None = None
    template_input_bytes: int | None = None
    template_output_bytes: int | None = None
    html_bytes: int | None = None
    options: dict = field(default_factory=dict)


current_diagnostics: ContextVar[RequestDiagnostics | None] = ContextVar(
    "resource_diagnostics", default=None
)


def record_sizes(*, template_source_bytes=None, template_input_bytes=None,
                 template_output_bytes=None, html_bytes=None):
    context = current_diagnostics.get()
    if context is not None:
        for name, value in (
            ("template_source_bytes", template_source_bytes),
            ("template_input_bytes", template_input_bytes),
            ("template_output_bytes", template_output_bytes),
            ("html_bytes", html_bytes),
        ):
            if value is not None:
                setattr(context, name, value)


def log_rejection(*, status, stage, category, limit_name, limit_value, observed=None):
    context = current_diagnostics.get() or RequestDiagnostics()
    # Every string below is an internal classification or a validated option enum.
    # Never interpolate exception messages, headers, paths, or arbitrary body fields.
    fields = {
        "status": status, "stage": stage, "category": category,
        "limit_name": limit_name, "limit_value": limit_value, "observed": observed,
        "content_length": context.content_length,
        "request_bytes": context.request_bytes,
        "template_source_bytes": context.template_source_bytes,
        "template_input_bytes": context.template_input_bytes,
        "template_output_bytes": context.template_output_bytes,
        "html_bytes": context.html_bytes,
        "options": context.options,
    }
    logger.bind(resource_rejection=fields).warning(
        "resource_rejection {}", json.dumps(fields, ensure_ascii=True, allow_nan=False)
    )
