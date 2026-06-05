# syntax=docker/dockerfile:1.7
#
# Review-Generator container.
#
# Multi-stage:
#   builder  — CUDA devel image; compiles llama.cpp + whisper.cpp with CUDA.
#   runtime  — CUDA cudnn runtime image; bakes Python/Node/age + built binaries
#              + repo. Entry: /app/entrypoint.sh (decrypt → install → models →
#              llama-servers → orchestrate.py).
#
# Image tag must be lowercase; container name preserves case.
# Build:   docker build -t descai-agent_core .
# Run:     docker run --rm --gpus all --name DeScAi-Agent_core \
#            -e AGE_SECRET_KEY_ENV="$(cat .env.age-key.txt)" \
#            -e AGE_SECRET_KEY_ARWEAVE="$(cat arweave-keyfile-…json.age-key.txt)" \
#            -v review-models:/app/models \
#            descai-agent_core
#
# Optional build args:
#   --build-arg LLAMA_CPP_REF=master
#   --build-arg WHISPER_CPP_REF=master
#   --build-arg CMAKE_CUDA_ARCHITECTURES="90"            # default = H100/H200 (Hopper)

ARG CUDA_VERSION=12.6.3

##############################################################################
# Stage 1: build llama.cpp and whisper.cpp with CUDA
##############################################################################
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# CUDA driver stub: ggml-cuda links against libcuda.so.1, which is only provided
# by the host driver at runtime (via --gpus). Place the build-time stub on the
# default linker search path (and refresh ldconfig) so `ld` resolves the
# transitive libcuda.so.1 dependency when linking llama-server / whisper-cli.
# Builder stage only — the real driver takes over at runtime in the slim image.
RUN ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/lib/x86_64-linux-gnu/libcuda.so.1 \
 && ldconfig

ARG CMAKE_CUDA_ARCHITECTURES="90"
ARG LLAMA_CPP_REF=master
ARG WHISPER_CPP_REF=master

# llama.cpp — build llama-server with CUDA; skip libcurl (we pass local -m paths).
RUN git clone --depth=1 --branch "${LLAMA_CPP_REF}" \
        https://github.com/ggml-org/llama.cpp.git /opt/llama.cpp \
 && cmake -S /opt/llama.cpp -B /opt/llama.cpp/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_CUDA_ARCHITECTURES="${CMAKE_CUDA_ARCHITECTURES}" \
        -DGGML_CUDA=ON \
        -DLLAMA_CURL=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=ON \
        -DLLAMA_BUILD_SERVER=ON \
        -DCMAKE_EXE_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -Wl,-rpath-link,/usr/local/cuda/lib64/stubs" \
        -DCMAKE_SHARED_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -Wl,-rpath-link,/usr/local/cuda/lib64/stubs" \
 && cmake --build /opt/llama.cpp/build --config Release -j"$(nproc)" --target llama-server

# whisper.cpp — build whisper-cli with CUDA.
RUN git clone --depth=1 --branch "${WHISPER_CPP_REF}" \
        https://github.com/ggml-org/whisper.cpp.git /opt/whisper.cpp \
 && cmake -S /opt/whisper.cpp -B /opt/whisper.cpp/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_CUDA_ARCHITECTURES="${CMAKE_CUDA_ARCHITECTURES}" \
        -DGGML_CUDA=ON \
        -DWHISPER_BUILD_TESTS=OFF \
        -DWHISPER_BUILD_EXAMPLES=ON \
        -DCMAKE_EXE_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -Wl,-rpath-link,/usr/local/cuda/lib64/stubs" \
        -DCMAKE_SHARED_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -Wl,-rpath-link,/usr/local/cuda/lib64/stubs" \
 && cmake --build /opt/whisper.cpp/build --config Release -j"$(nproc)" --target whisper-cli


##############################################################################
# Stage 2: runtime
##############################################################################
FROM nvidia/cuda:${CUDA_VERSION}-cudnn-runtime-ubuntu22.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG NODE_MAJOR=20

# System runtime deps:
#   age           — decrypt-secrets.sh
#   ffmpeg        — whisper.cpp pulls audio from video files
#   curl, ca-certificates, gnupg — health checks + apt repos + hf downloads
#   git           — used by some pipeline helpers / pip installs
#   python3 + pip — global install for requirements.txt
#   libgomp1      — OpenMP runtime for built binaries
RUN apt-get update && apt-get install -y --no-install-recommends \
        age \
        ca-certificates \
        curl \
        ffmpeg \
        git \
        gnupg \
        libgomp1 \
        python3 \
        python3-dev \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Node.js (NodeSource) — npm comes with it.
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && node --version && npm --version

# Bring built binaries + their shared libs from the builder stage.
COPY --from=builder /opt/llama.cpp/build   /opt/llama.cpp/build
COPY --from=builder /opt/whisper.cpp/build /opt/whisper.cpp/build

# Expose binaries on PATH; .so files live under build/bin and build/src in modern llama.cpp.
ENV PATH=/opt/llama.cpp/build/bin:/opt/whisper.cpp/build/bin:${PATH} \
    LD_LIBRARY_PATH=/opt/llama.cpp/build/bin:/opt/llama.cpp/build/src:/opt/llama.cpp/build/ggml/src:/opt/whisper.cpp/build/bin:/opt/whisper.cpp/build/src:/opt/whisper.cpp/build/ggml/src

RUN ln -sf /opt/llama.cpp/build/bin/llama-server /usr/local/bin/llama-server \
 && ln -sf /opt/whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli

# App
WORKDIR /app
COPY . /app

# Python deps — global install (no venv). Touch the marker so entrypoint skips reinstall.
RUN python3 -m pip install --upgrade pip \
 && python3 -m pip install --no-cache-dir -r /app/requirements.txt \
 && touch /app/.entrypoint-pip-done

# Crawl4AI / Playwright browsers + chromium system deps (needs root; available in build).
RUN crawl4ai-setup || true

# Node deps — pre-install so first container start is fast (entrypoint re-runs idempotently).
RUN npm install --prefix /app/uploader \
 && npm install --prefix /app/crawlers/molecule/crawler

# Scripts executable.
RUN chmod +x /app/entrypoint.sh /app/decrypt-secrets.sh

# Persist downloaded GGUFs across container restarts.
VOLUME ["/app/models", "/app/logs"]

ENTRYPOINT ["/app/entrypoint.sh"]
