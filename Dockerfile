# Unified Algorithm API: Python 3.12 + JDK 17 (Timefold) + Node (Rapier) + Julia (PySR).
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PYTHON_JULIACALL_HANDLE_SIGNALS=yes \
    # Precompile Julia code for generic x86-64 so the image runs on any host CPU.
    JULIA_CPU_TARGET="generic;sandybridge,-xsaveopt,clone_all;haswell,-rdrnd,base(1)"

# openjdk-17-jre-headless: Timefold's JVM. nodejs: Rapier worker. build-essential: hnswlib.
# libgl1/libglib2.0-0/libgomp1: OpenCV and PaddlePaddle native deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-17-jre-headless nodejs npm build-essential \
        libgl1 libglib2.0-0 libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only PyTorch (the default Linux wheel bundles CUDA and is several GB larger).
COPY requirements.txt .
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.15.0 \
    && pip install -r requirements.txt \
    && apt-get purge -y build-essential && apt-get autoremove -y

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
