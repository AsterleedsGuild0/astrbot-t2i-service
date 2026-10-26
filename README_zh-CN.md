# AstrBot Text2Image Service

中文 | [English](README.md) | [日本語](README_ja.md)

## 功能

一个简单的将 HTML/模板转换为图片的 Web 服务，支持图片生命周期管理。

## 环境变量配置

- `PORT`: 服务端口，默认 8999
- `TZ`: 容器默认时区为 `Asia/Shanghai`（北京时间，UTC+8），镜像内安装 `tzdata` 并设置系统时区。已有容器需要重建才会使用新的镜像或环境变量；Docker 引擎为 `docker logs --timestamps` 添加的 UTC 时间戳不受容器 `TZ` 影响。
- `IMAGE_LIFETIME_HOURS`: 图片生命时间（小时），默认 24 小时。超过此时间的图片文件将被自动清理
- `STORAGE_BACKEND`: 图片存储后端，支持 `local`（默认）、`s3` 和 `r2`
- `T2I_RENDER_WAIT_UNTIL`: Playwright 页面导航等待策略，默认 `domcontentloaded`
- `T2I_SKIP_FONT_READY`: 是否跳过 Playwright 截图前内部的 `document.fonts.ready` 等待，默认 `true`
- `RATE_LIMIT_MAX_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS`: 进程内限流配置，两个值都大于 0 时启用

### Cloudflare R2 / S3 兼容对象存储

多副本部署时，可以将 JSON 模式生成的图片写入同一个 S3 兼容存储桶，
避免图片生成请求和后续读取请求落在不同实例时返回 `404`。API 路径和响应格式保持不变。

```env
STORAGE_BACKEND=r2
S3_ENDPOINT_URL=https://<ACCOUNT_ID>.r2.cloudflarestorage.com
S3_BUCKET=astrbot-t2i
AWS_ACCESS_KEY_ID=<ACCESS_KEY_ID>
AWS_SECRET_ACCESS_KEY=<SECRET_ACCESS_KEY>
AWS_DEFAULT_REGION=auto
S3_PREFIX=images
```

建议为存储桶配置对象生命周期规则以自动删除过期图片。
`IMAGE_LIFETIME_HOURS` 只清理本地文件，不会删除对象存储中的图片。

## API 接口

### POST /text2img/generate

html 转 img

> html 和 tmpl 任选一个。tmpl 和 tmpldata 一起提供。

- `str` html: html 文本
- `str` tmpl: jinja2 html 模板
- `dict` tmpldata: jinja2 模板 data
- `bool` json: 是否返回 json 格式（返回一个 id）
- `dict` `optional` options
  - timeout (float, optional): 截图超时时间.
  - wait_until (Literal["commit", "domcontentloaded", "load", "networkidle"], optional): 页面导航等待状态，默认使用服务端 `T2I_RENDER_WAIT_UNTIL` 配置.
  - type (Literal["jpeg", "png"], optional): 截图图片类型.
  - quality (int, optional): 截图质量，仅适用于 JPEG 格式图片. 服务端强制下限为 70，低于该值或未指定时会被抬升，以避免文字边缘出现块状压缩失真.
  - omit_background (bool, optional): 是否允许隐藏默认的白色背景，这样就可以截透明图了，仅适用于 PNG 格式
  - full_page (bool, optional): 是否截整个页面而不是仅设置的视口大小，默认为 True.
  - clip (FloatRect, optional): 截图后裁切的区域，xy为起点.
  - animations: (Literal["allow", "disabled"], optional): 是否允许播放 CSS 动画.
  - caret: (Literal["hide", "initial"], optional): 当设置为 `hide` 时，截图时将隐藏文本插入符号，默认为 `hide`.
    - scale: (Literal["css", "device"], optional): 页面缩放设置. 当设置为 `css` 时，则将设备分辨率与 CSS 中的像素一一对应，在高分屏上会使得截图变小. 当设置为 `device` 时，则根据设备的屏幕缩放设置或当前 Playwright 的 Page/Context 中的 device_scale_factor 参数来缩放.
    - viewport_width (int, optional): 自定义视口宽度，用于控制截图宽度. 优先级顺序：
      1. 在请求 options 中显式指定
      2. 从 HTML 的 `<meta name="viewport" content="width=...">` 自动解析
      3. 未指定时默认为 800px
    - viewport_height (int, optional): 自定义视口高度，用于控制截图高度. 优先级顺序：
      1. 在请求 options 中显式指定
      2. 从 HTML 的 `<meta name="viewport" content="height=...">` 自动解析
      3. 未指定时默认为 720px
    - device_scale_factor_level (Literal["normal", "high", "ultra"], optional): 设备像素比等级，未指定时默认为 "high". 不同等级使用独立的浏览器上下文池，提供更好的性能和资源隔离.
      - `normal`: 设备像素比 1.0
      - `high`: 设备像素比 1.3（默认）
      - `ultra`: 设备像素比 1.8

### GET /text2img/data/{id}

根据 id 返回对应的图像。

## Docker 渲染说明

容器镜像会安装 `fonts-noto-cjk` 与 `fonts-noto-color-emoji`，以提升中文与 emoji 的渲染稳定性。

### 小内存服务器的资源保护

仓库的 `compose.yaml` 提供 768 MiB 内存、1 核 CPU、256 PID 和日志轮转的起始预算，
并允许调用方使用 `ultra`，优先保留请求的分辨率、图片格式与 JPEG 质量，不默认降档。
调用方未指定缩放等级时仍使用原有的 `high`；仅当显式配置了更低的 `T2I_MAX_SCALE_LEVEL` 时才限制缩放档位。
这是止损预算，不是已经验证适合所有长图的容量；过大的报告会明确失败，不通过降低画质或裁切内容来绕过预算，仍可能触发容器内 OOM。
请按实际峰值调整，不要取消容器硬上限。升级前若已在自己的 Compose 中设置 `normal` 或 `high`，需要改成 `ultra` 并重建容器；只更新镜像不会覆盖已有环境变量。

服务端每进程只渲染一个任务，在解析请求体之前限制排队。默认保护参数：

- `T2I_MAX_QUEUE_SIZE=2`、`T2I_QUEUE_TIMEOUT_SECONDS=10`：最多两个等待请求，超额或等待超时返回 503。
- `T2I_MAX_REQUEST_BYTES=8388608`、`T2I_BODY_TIMEOUT_SECONDS=15`：限制实际请求体字节数和上传耗时，包括分块请求。
- `T2I_MAX_HTML_BYTES=4194304`：限制输入模板、HTML 和模板展开后的 UTF-8 大小。
- `T2I_TEMPLATE_TIMEOUT_SECONDS=10`、`T2I_TEMPLATE_MEMORY_MB=256`：模板在独立子进程生成；Linux 下额外限制其地址空间与 CPU 时间，超时终止子进程。
- `T2I_OPERATION_TIMEOUT_MS=30000`：导航和截图的单步超时上限；调用方传入 0 或更大的值也不会关闭此保护。
- `T2I_RENDER_TIMEOUT_SECONDS=45`、`T2I_CLEANUP_TIMEOUT_SECONDS=3`：浏览器阶段总截止时间，以及每项资源回收的等待上限。回收耗时另计，严重系统卡顿时无法保证墙钟时间。
- `T2I_MAX_SCALE_LEVEL=ultra`：默认允许调用方原有的 ultra 请求，不因资源保护主动降低画质；可显式设置为 normal/high/ultra。此项只控制上限，不会把调用方请求的 normal/high 自动放大。
- `T2I_MAX_VIEWPORT_DIMENSION=4096`：视口任一边的 CSS 像素上限。
- `T2I_MAX_SCREENSHOT_PIXELS=16000000`、`T2I_MAX_SCREENSHOT_DIMENSION=16384`：截图前测量实际文档，限制预计物理像素面积和单边长度。整页截图同时检查完整栅格表面，即使指定 clip 或 CSS scale 也不能绕过。
- `T2I_BROWSER_MAX_RENDERS=100`：在任务边界定期回收浏览器，避免上下文长期积累。
- `T2I_MIN_AVAILABLE_MEMORY_MB=128`、`T2I_MIN_FREE_DISK_MB=128`：低于主机可用内存或磁盘空间预留值时拒绝新渲染；设置了 cgroup 内存上限时，达到其 90% 也拒绝新任务。
- `T2I_MAX_PAGE_EVENT_LOGS=20`：每个页面的事件日志上限，避免失效资源产生大量重复日志。

预算超限返回 413，资源不足或排队失败返回 503，浏览器总超时返回 504，磁盘空间不足返回 507。
错误仍使用 `code/message/data` 结构，`data.stage` 标明失败阶段。不会为了通过预算而静默裁掉长图。
直接返回图片时在传输结束后删除临时图片，HTML 在请求结束后删除；JSON 模式的本地图片继续按生命周期清理。

`GET /healthz` 在资源回收失败后返回 503；此时服务拒绝新渲染，避免反复启动浏览器造成进程积累。
需要检查容器并重启恢复；Docker 的 `restart` 策略不会仅因为 unhealthy 就自动重启。
客户端断开时会取消截图并尝试有界回收浏览器，应用退出时也会停止清理任务和浏览器。

这些保护不是不可信 HTML 的完整隔离方案：JavaScript、外链资源解码以及测量后变化的页面仍可能产生额外开销。
请保留容器内存/CPU/PID 硬上限，并通过防火墙或反向代理限制调用来源。增加 Uvicorn worker 或部署多个副本会分别创建浏览器和排队槽位，需要重新计算总预算。

### 构建与发布

推送到 `main` 或在 Actions 手动运行 **Build and publish GHCR image**，会在原生 amd64/arm64 runner 上构建并发布到 `ghcr.io/<仓库所有者>/<仓库名>`。
使用仓库内置的 `GITHUB_TOKEN` 和 `packages: write` 权限，不需要配置额外 PAT。

发布标签包括北京时间版本号（附运行 ID）、`sha-<12位提交号>`，以及 main 分支的 `latest`。
两种架构全部构建成功后才更新多架构标签；发布阶段只检查 registry manifest，不拉取或启动镜像，不运行测试。
Dockerfile 只做 Python 字节码编译检查，构建成功不代表功能或资源峰值已验证。
原有 **Build Docker Image Archives** 手动工作流仍保留，用于按需导出 tar。

首次发布的 GHCR 包可能需要在 GitHub Packages 中配置访问权限；公开部署前请确认包的可见性。
部署时可通过 `T2I_IMAGE` 指定不可变 SHA 标签代替 latest，再使用现有 Compose 流程更新。
