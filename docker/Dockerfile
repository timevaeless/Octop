# 注：不使用 `# syntax=docker/dockerfile:1` 指令 —— 它会强制 BuildKit 每次构建
# 都去 docker.io 解析 frontend 镜像元数据（本机网络下超时 60s+）。
# Docker 29 内置 frontend 已支持本文件全部特性（RUN --mount=type=cache、COPY --from=<image>）。
# =============================================================================
# Octop — 多阶段 Docker 构建
#
# 阶段 1: 构建 React/TypeScript 前端
# 阶段 2: Python 运行时 + 预构建前端
#
# 用法（在仓库根目录执行，构建上下文为根目录）:
#   docker build -f docker/Dockerfile -t octop:latest .
#   docker run -d -p 8088:8088 -v octop-data:/data/.octop -e HOME=/data octop:latest
#
# 国内加速（可选，需 BuildKit，docker/docker_build.sh 默认已开启）:
#   PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
#   PIP_TRUSTED_HOST=mirrors.cloud.tencent.com \
#   NPM_REGISTRY=https://mirrors.cloud.tencent.com/npm/ \
#   APT_MIRROR=mirrors.cloud.tencent.com \
#   bash docker/docker_build.sh
#
# 或直接传 build-arg:
#   docker build \
#     -f docker/Dockerfile \
#     --build-arg PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
#     --build-arg PIP_TRUSTED_HOST=mirrors.cloud.tencent.com \
#     --build-arg NPM_REGISTRY=https://mirrors.cloud.tencent.com/npm/ \
#     --build-arg APT_MIRROR=mirrors.cloud.tencent.com \
#     --build-arg NODE_MAX_OLD_SPACE_SIZE=1024 \
#     -t octop:latest .
#
# 或使用 Compose:
#   docker compose -f docker/docker-compose.yml up -d
# =============================================================================

# ---------------------------------------------------------------------------
# 阶段 1 — 前端构建
# ---------------------------------------------------------------------------
FROM node:20-slim AS frontend-builder

ARG NPM_REGISTRY=
# Node heap ceiling for the vite build. The build peaks above 2G and V8
# aborts (exit 134); default 4096. Lower this (e.g. 1024) on low-memory
# build hosts (< 4G RAM) to avoid OOM kills — the value only caps the max,
# it is not pre-allocated.
ARG NODE_MAX_OLD_SPACE_SIZE=4096
WORKDIR /build/dashboard

COPY dashboard/package.json dashboard/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    set -eu; \
    if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi; \
    npm ci --no-audit \
    || { echo "npm ci failed — retrying clean"; rm -rf node_modules; npm ci --no-audit; }; \
    test -x node_modules/.bin/vite

COPY dashboard/ ./

RUN mkdir -p ../src/octop/dashboard

# 镜像构建跳过完整 tsc；生产打包由 vite/esbuild 完成（使用 lockfile 中的 vite，不用 npx）
RUN NODE_ENV=production NODE_OPTIONS="--max-old-space-size=${NODE_MAX_OLD_SPACE_SIZE}" npm run build:docker


# ---------------------------------------------------------------------------
# 阶段 2 — Python 运行时
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL maintainer="OrcaKit Team"
LABEL org.opencontainers.image.title="Octop"
LABEL org.opencontainers.image.description="Smarter self-hosted AI assistant — multi-user, multi-agent."
LABEL org.opencontainers.image.source="https://github.com/TencentCloud/orca"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/data \
    OCTOP_BIND_HOST=0.0.0.0 \
    OCTOP_PORT=8088 \
    OCTOP_LOG_LEVEL=info \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

ARG PIP_INDEX_URL=
ARG PIP_TRUSTED_HOST=
ARG APT_MIRROR=

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    if [ -n "$APT_MIRROR" ]; then \
        printf 'Types: deb\nURIs: https://%s/debian\nSuites: trixie trixie-updates\nComponents: main contrib non-free non-free-firmware\n\nTypes: deb\nURIs: https://%s/debian-security\nSuites: trixie-security\nComponents: main contrib non-free non-free-firmware\n' \
            "$APT_MIRROR" "$APT_MIRROR" > /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libgl1 \
        libglib2.0-0t64 \
        curl \
        git \
        ripgrep \
        fonts-noto-cjk \
        unzip \
        zip \
        wget \
        jq \
        file \
        tree \
        less \
        nano \
        vim-tiny \
        procps \
        psmisc \
        iputils-ping \
        net-tools \
        rsync \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/

WORKDIR /app

# 先安装锁定依赖，仅改源码时可复用此层缓存
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -n "$PIP_INDEX_URL" ]; then \
        export UV_INDEX_URL="$PIP_INDEX_URL"; \
        if [ -n "$PIP_TRUSTED_HOST" ]; then export UV_INSECURE_HOST="$PIP_TRUSTED_HOST"; fi; \
    fi \
    && uv sync --frozen --no-install-project --no-dev --extra browser --extra local-embedding --extra knowledge-ocr

ENV PATH="/app/.venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright

# ---------------------------------------------------------------------------
# 数字员工构建工具链（可插拔：--build-arg DEV_TOOLS= 空值即跳过）
# Temurin JDK 8 + 17、Node 16 + 20、Maven 3.9（tarball，多架构自适应）。
# 默认 PATH = JDK 17 + Node 20 + Maven；切版本用环境前缀：
#   JAVA_HOME=/opt/jdk8 PATH=/opt/jdk8/bin:$PATH mvn test
#   PATH=/opt/node16/bin:$PATH node -v
# JDK 源默认清华 Adoptium；Node/Maven 源默认腾讯云（均可用 build-arg 覆盖）。
#
# ⚠️ 本层必须位于 `COPY src/` 之前：源码变更只应重跑末尾的依赖同步层，
#    不能连坐工具链重下。下载走 cache mount（/opt/toolchain-cache）：
#    即使本层失效（如改了 mirror 配置），缓存命中即跳过下载，秒级恢复。
#    JDK 版本动态解析目录页最新版（固定小版本号会随镜像下架 404），
#    tarball 以「文件名含版本号」缓存，升级新版自动重新下载。
# ---------------------------------------------------------------------------
ARG DEV_TOOLS=1
ARG ADOPTIUM_MIRROR=mirrors.tuna.tsinghua.edu.cn/Adoptium
RUN --mount=type=cache,target=/opt/toolchain-cache,sharing=locked \
    set -eu; \
    if [ -z "$DEV_TOOLS" ]; then \
        echo "DEV_TOOLS disabled — skipping toolchain layer"; exit 0; \
    fi; \
    case "$(uname -m)" in \
        x86_64) jdk_a=x64; node_a=x64 ;; \
        aarch64) jdk_a=aarch64; node_a=arm64 ;; \
        *) echo "unsupported arch: $(uname -m)"; exit 1 ;; \
    esac; \
    jdk8=$(curl -fsSL "https://${ADOPTIUM_MIRROR}/8/jdk/${jdk_a}/linux/" | grep -o "OpenJDK8U-jdk_${jdk_a}_linux_hotspot_[0-9ub+]*\.tar\.gz" | sort -V | tail -1); \
    jdk17=$(curl -fsSL "https://${ADOPTIUM_MIRROR}/17/jdk/${jdk_a}/linux/" | grep -o "OpenJDK17U-jdk_${jdk_a}_linux_hotspot_[0-9._+]*\.tar\.gz" | sort -V | tail -1); \
    [ -n "$jdk8" ] && [ -n "$jdk17" ] || { echo "failed to resolve Adoptium latest"; exit 1; }; \
    echo "toolchain: $jdk8 / $jdk17"; \
    dl() { f="/opt/toolchain-cache/$1"; if [ ! -s "$f" ]; then echo "downloading $1 ..."; curl -fsSL "$2" -o "$f"; fi; }; \
    dl "$jdk8" "https://${ADOPTIUM_MIRROR}/8/jdk/${jdk_a}/linux/$jdk8"; \
    mkdir -p /opt/jdk8 && tar -xzf "/opt/toolchain-cache/$jdk8" -C /opt/jdk8 --strip-components=1; \
    dl "$jdk17" "https://${ADOPTIUM_MIRROR}/17/jdk/${jdk_a}/linux/$jdk17"; \
    mkdir -p /opt/jdk17 && tar -xzf "/opt/toolchain-cache/$jdk17" -C /opt/jdk17 --strip-components=1; \
    dl "node-v16.20.2-linux-${node_a}.tar.gz" "https://mirrors.cloud.tencent.com/nodejs-release/v16.20.2/node-v16.20.2-linux-${node_a}.tar.gz"; \
    mkdir -p /opt/node16 && tar -xzf "/opt/toolchain-cache/node-v16.20.2-linux-${node_a}.tar.gz" -C /opt/node16 --strip-components=1; \
    dl "node-v20.18.1-linux-${node_a}.tar.gz" "https://mirrors.cloud.tencent.com/nodejs-release/v20.18.1/node-v20.18.1-linux-${node_a}.tar.gz"; \
    mkdir -p /opt/node20 && tar -xzf "/opt/toolchain-cache/node-v20.18.1-linux-${node_a}.tar.gz" -C /opt/node20 --strip-components=1; \
    dl "apache-maven-3.9.16-bin.tar.gz" "https://mirrors.cloud.tencent.com/apache/maven/maven-3/3.9.16/binaries/apache-maven-3.9.16-bin.tar.gz"; \
    mkdir -p /opt/maven && tar -xzf "/opt/toolchain-cache/apache-maven-3.9.16-bin.tar.gz" -C /opt/maven --strip-components=1

ENV PATH="/opt/node20/bin:/opt/jdk17/bin:/opt/maven/bin:$PATH" \
    JAVA_HOME=/opt/jdk17

# pnpm 交给 corepack。坑：Node 内置 corepack（0.29.4 及更早）对 pnpm 新签名做
# keyid 校验失败（npm 更换了 pnpm 的签名密钥，报 "Cannot find matching keyid"），
# 故先升级 corepack 覆盖内置版再 enable；pnpm 版本按项目 packageManager 首用时下载。
ARG NPM_REGISTRY=
RUN /opt/node20/bin/npm install -g corepack@0.36.0 ${NPM_REGISTRY:+--registry="$NPM_REGISTRY"} >/dev/null 2>&1 || true; \
    /opt/node16/bin/npm install -g corepack@0.36.0 ${NPM_REGISTRY:+--registry="$NPM_REGISTRY"} >/dev/null 2>&1 || true; \
    corepack enable || true

COPY src/ ./src/
COPY --from=frontend-builder /build/src/octop/dashboard/ ./src/octop/dashboard/

COPY .env.example ./
COPY docker/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 安装项目本身（依赖层已在上方缓存；源码变更仅重跑本层，uv 缓存挂载保证依赖不重下）
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -n "$PIP_INDEX_URL" ]; then \
        export UV_INDEX_URL="$PIP_INDEX_URL"; \
        if [ -n "$PIP_TRUSTED_HOST" ]; then export UV_INSECURE_HOST="$PIP_TRUSTED_HOST"; fi; \
    fi \
    && uv sync --frozen --no-dev --extra browser --extra local-embedding --extra knowledge-ocr \
    && .venv/bin/python -c "import cv2, fastembed, huggingface_hub, rapidocr, onnxruntime, pymupdf" \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/* /tmp/*

RUN mkdir -p /data/.octop

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["sh", "-c", "curl -f http://localhost:${OCTOP_PORT:-8088}/api/health || exit 1"]

ENTRYPOINT ["docker-entrypoint.sh"]
CMD []
