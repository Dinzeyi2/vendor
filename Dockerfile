# Unified Algorithm API: Python 3.12 + JDK 17 (Timefold) + Node 22 (Rapier, imports) + Julia (PySR),
# plus the C/C++ toolchain and CMake that GitHub imports build with (in the sandbox, offline).
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PYTHON_JULIACALL_HANDLE_SIGNALS=yes \
    # Precompile Julia code for generic x86-64 so the image runs on any host CPU.
    JULIA_CPU_TARGET="generic;sandybridge,-xsaveopt,clone_all;haswell,-rdrnd,base(1)"

# openjdk-17-jre-headless: Timefold's JVM. build-essential: hnswlib, and C/C++ imports.
# cmake/autoconf/automake/libtool/pkg-config + common -dev headers: native GitHub imports.
# libgl1/libglib2.0-0/libgomp1: OpenCV and PaddlePaddle native deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-17-jre-headless build-essential cmake ninja-build pkg-config autoconf automake libtool \
        zlib1g-dev libbz2-dev liblzma-dev libffi-dev \
        libgl1 libglib2.0-0 libgomp1 ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 (official build: npm + corepack for yarn/pnpm, and C headers so native addons of
# imported packages build offline). Verified against the release's published SHA-256 sums.
ARG NODE_VERSION=22.11.0
RUN cd /tmp && curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
    && curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" \
    && grep " node-v${NODE_VERSION}-linux-x64.tar.xz\$" SHASUMS256.txt | sha256sum -c - \
    && tar -xJf "node-v${NODE_VERSION}-linux-x64.tar.xz" -C /usr/local --strip-components=1 --no-same-owner \
    && rm -f "node-v${NODE_VERSION}-linux-x64.tar.xz" SHASUMS256.txt \
    && node --version && npm --version

WORKDIR /app

# CPU-only PyTorch (the default Linux wheel bundles CUDA and is several GB larger).
COPY requirements.txt .
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.15.0 \
    && pip install -r requirements.txt

COPY workers/rapier/package.json workers/rapier/package-lock.json workers/rapier/
RUN cd workers/rapier && npm ci --omit=dev

# Bake Julia + PySR precompilation into the image (takes several minutes; avoids a cold start at runtime).
RUN python -c "import pysr"

COPY app app
COPY workers/rapier/worker.mjs workers/rapier/

# Bake PaddleOCR's English models into the image.
RUN python -c "import app.ops.paddleocr_ops"

# Start every worker in the background at boot; /health answers immediately.
ENV WARM_WORKERS=all
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
