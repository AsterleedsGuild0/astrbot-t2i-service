# AstrBot Text2Image Service

[中文](README_zh-CN.md) | English | [日本語](README_ja.md)

## Features

A simple web service that converts HTML/templates to images, with image lifecycle management support.

## Environment Variables

- `PORT`: Service port, default is 8999
- `IMAGE_LIFETIME_HOURS`: Image lifetime in hours, default is 24 hours. Images older than this will be automatically cleaned up
- `STORAGE_BACKEND`: Image storage backend. Supports `local` (default), `s3`, and `r2`
- `T2I_RENDER_WAIT_UNTIL`: Playwright page navigation wait strategy. Defaults to `domcontentloaded`
- `T2I_SKIP_FONT_READY`: Whether to skip Playwright's internal `document.fonts.ready` wait before screenshotting. Defaults to `true`
- `RATE_LIMIT_MAX_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS`: In-process rate limit settings. Both must be greater than 0 to enable

### Cloudflare R2 / S3-compatible object storage

When running multiple replicas, JSON-mode images can be stored in one shared
S3-compatible bucket. This prevents a later image request from returning `404`
when it reaches a replica other than the one that rendered the image. API paths
and response formats remain unchanged.

```env
STORAGE_BACKEND=r2
S3_ENDPOINT_URL=https://<ACCOUNT_ID>.r2.cloudflarestorage.com
S3_BUCKET=astrbot-t2i
AWS_ACCESS_KEY_ID=<ACCESS_KEY_ID>
AWS_SECRET_ACCESS_KEY=<SECRET_ACCESS_KEY>
AWS_DEFAULT_REGION=auto
S3_PREFIX=images
```

Configure an object lifecycle rule on the bucket to expire old images.
`IMAGE_LIFETIME_HOURS` only cleans up local files and does not delete objects.

## API Endpoints

### POST /text2img/generate

Convert HTML to image

> Choose either html or tmpl. Provide tmpl and tmpldata together.

- `str` html: HTML text
- `str` tmpl: Jinja2 HTML template
- `dict` tmpldata: Jinja2 template data
- `bool` json: Whether to return JSON format (returns an id)
- `dict` `optional` options
  - timeout (float, optional): Screenshot timeout.
  - wait_until (Literal["commit", "domcontentloaded", "load", "networkidle"], optional): Page navigation wait state. Defaults to the service-level `T2I_RENDER_WAIT_UNTIL`.
  - type (Literal["jpeg", "png"], optional): Screenshot image type.
  - quality (int, optional): Screenshot quality, only applicable to JPEG format.
  - omit_background (bool, optional): Whether to hide the default white background, allowing transparent screenshots (PNG only).
  - full_page (bool, optional): Whether to capture the entire page instead of just the viewport, default is True.
  - clip (FloatRect, optional): Area to clip after screenshot, xy is the starting point.
  - animations: (Literal["allow", "disabled"], optional): Whether to allow CSS animations.
  - caret: (Literal["hide", "initial"], optional): When set to `hide`, the text caret will be hidden during screenshot, default is `hide`.
  - scale: (Literal["css", "device"], optional): Page scaling settings. When set to `css`, device resolution maps 1:1 with CSS pixels, making screenshots smaller on high-DPI screens. When set to `device`, scales according to device screen scaling or the device_scale_factor parameter in the current Playwright Page/Context.
  - viewport_width (int, optional): Custom viewport width to control screenshot width. Resolved in priority order:
    1. Explicitly set in request options
    2. Auto-parsed from `<meta name="viewport" content="width=...">` in HTML
    3. Defaults to 800px if not specified and no meta tag found
  - viewport_height (int, optional): Custom viewport height to control screenshot height. Resolved in priority order:
    1. Explicitly set in request options
    2. Auto-parsed from `<meta name="viewport" content="height=...">` in HTML
    3. Defaults to 720px if not specified and no meta tag found
  - device_scale_factor_level (Literal["normal", "high", "ultra"], optional): Device pixel ratio level, default is "normal". Different levels use independent browser context pools for better performance and resource isolation.
    - `normal`: Device pixel ratio 1.0 (default)
    - `high`: Device pixel ratio 1.3
    - `ultra`: Device pixel ratio 1.8

### GET /text2img/data/{id}

Returns the corresponding image by id.

## Docker rendering notes

The container image installs `fonts-noto-cjk` and `fonts-noto-color-emoji` to improve Chinese and emoji rendering consistency.

## Resource limits and rejection diagnostics

Limits apply before browser rendering and are configured per process. This change
does not increase their defaults.

| Setting | Default | Scope |
| --- | --- | --- |
| `T2I_MAX_REQUEST_BYTES` | 8 MiB | HTTP body and serialized template-worker input |
| `T2I_MAX_HTML_BYTES` | 4 MiB | HTML, template source, and generated HTML |
| `T2I_MAX_QUEUE_SIZE` | 2 | Waiting requests, excluding the active request |
| `T2I_QUEUE_TIMEOUT_SECONDS` | 10 | Waiting for an available render slot |
| `T2I_BODY_TIMEOUT_SECONDS` | 15 | Reading the admitted body |
| `T2I_TEMPLATE_TIMEOUT_SECONDS` | 10 | Template wall time and worker CPU budget |
| `T2I_TEMPLATE_MEMORY_MB` | 256 | Template-worker address-space budget |
| `T2I_RENDER_TIMEOUT_SECONDS` | 45 | Total browser-render deadline |
| `T2I_OPERATION_TIMEOUT_MS` | 30000 | Maximum navigation/screenshot operation timeout |
| `T2I_CLEANUP_TIMEOUT_SECONDS` | 3 | Individual browser cleanup waits |
| `T2I_MAX_VIEWPORT_DIMENSION` | 4096 | Maximum viewport width or height |
| `T2I_MAX_SCREENSHOT_DIMENSION` | 16384 | Maximum raster width or height |
| `T2I_MAX_SCREENSHOT_PIXELS` | 16000000 | Raster pixel budget, including device scale |
| `T2I_MAX_SCALE_LEVEL` | `ultra` | Maximum effective device scale level |
| `T2I_MIN_AVAILABLE_MEMORY_MB` | 128 | Host available-memory reserve |
| `T2I_MIN_FREE_DISK_MB` | 128 | Render storage free-space reserve |

Admission also refuses rendering at 90% of the container's cgroup memory limit.
Every resource rejection writes one `WARNING` line prefixed `resource_rejection`,
with a JSON object containing:

- `status`, `stage`, and a fixed `category`.
- `limit_name`, the current `limit_value`, and `observed` when measurable.
  Units follow the named setting: bytes, seconds, milliseconds, MiB, pixels, or a
  ratio. Non-configurable health/geometry invariants have descriptive limit names.
- `content_length` and `request_bytes` (actual bytes received, including the chunk
  that exceeded the body limit).
- `template_source_bytes`, `template_input_bytes` (serialized worker input),
  `template_output_bytes`, and `html_bytes`, where available. Template-file reads
  and incremental worker output stop at the limit: their recorded size may be a
  lower bound, not the full hypothetical output size.
- `options`: only validated, non-content screenshot fields such as `type`,
  `device_scale_factor_level`, viewport dimensions, and timeouts.

Unknown sizes are `null`. Rejections before body parsing have no options (`{}`);
the service does not consume a rejected body to extract diagnostic fields.
Templates, HTML, `tmpldata`, template names, URLs, authentication headers, and
exception text are never included in this rejection record.

| Category | Cause | HTTP status |
| --- | --- | --- |
| `request_body` | Declared or received body exceeds the byte limit | 413 |
| `template_input` | Template source or serialized worker input exceeds a byte limit | 413 |
| `template_output` | Generated HTML exceeds its byte limit | 413 |
| `template_memory` | Worker explicitly reports `MemoryError` | 413 |
| `template_cpu` | Worker exits on the CPU-budget signal (`SIGXCPU`) | 413 |
| `template_worker_signal` | Other fatal worker signal; not assumed to be OOM or CPU exhaustion | 413 |
| `html_size`, `viewport_dimension`, `screenshot_dimension`, `screenshot_pixels`, `screenshot_dimensions`, `screenshot_clip` | HTML or screenshot geometry limit | 413 |
| `request_body_timeout` | Body read deadline | 408 |
| `queue_full`, `queue_timeout` | Queue capacity or wait deadline | 503 |
| `container_memory`, `host_memory` | Memory safety reserve | 503 |
| `renderer_unhealthy`, `renderer_cleanup`, `browser_unavailable` | Browser unavailable or cleanup cannot be completed | 503 |
| `template_timeout`, `render_timeout` | Template or render wall-clock deadline | 504 |
| `disk_reserve` | Insufficient free storage | 507 |

The worker CPU soft limit remains the configured budget; its hard limit is one
second higher where the inherited hard limit permits it, so the parent can
distinguish `SIGXCPU` from an unexplained `SIGKILL`. Unknown kills are not diagnosed
as memory exhaustion. Worker resource enforcement requires the platform's
`resource` support (the production container runs Linux).

HTTP response bodies, stages, status codes, and `Retry-After` behavior remain
unchanged. Use these warning records rather than access-log status alone to decide
which limit, if any, needs adjustment.
