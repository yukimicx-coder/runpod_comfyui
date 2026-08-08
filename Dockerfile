FROM python:3.12-slim AS base

# 環境変数の設定 (Pythonのバッファ無効化・対話無効化)
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

WORKDIR /

RUN \
    --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    init-system-helpers \
    openssh-server \
    procps \
    jq \
    git \
    ca-certificates \
    ffmpeg \
    libgl1 \
    libglib2.0-0

# Open-SSH
EXPOSE 22
RUN mkdir -p /root/.ssh && chmod 0700 /root/.ssh
COPY ./base/sshd_config /etc/ssh/sshd_config
RUN chmod 0644 /etc/ssh/sshd_config

ENV TZ=Asia/Tokyo

# PIP/UV
ARG PIP_BREAK_SYSTEM_PACKAGES=1
ARG PIP_ROOT_USER_ACTION=ignore
ENV PIP_NO_CACHE_DIR=1
RUN pip install --no-cache-dir uv

ENV PATH=/root/.local/bin:$PATH

COPY ./base/start.sh /start.sh
RUN chmod 0755 /start.sh

VOLUME ["/workspace/"]

CMD [ "/start.sh" ]



FROM base AS comfy_cli

# comfy-cli only
ENV UV_PROJECT=/root/comfy
ENV UV_CACHE_DIR=${UV_PROJECT}/.cache/uv
WORKDIR ${UV_PROJECT}

RUN uv venv --no-managed-python --no-python-downloads --seed .venv && \
    uv pip install comfy-cli && \
    uv run comfy tracking disable 

ENV COMFY_NO_TELEMETRY=1

EXPOSE 8188

COPY ./comfyui/extra_model_paths.yaml /root/comfy/extra_model_paths.yaml

FROM comfy_cli AS comfyui_ready

# スクリプト

COPY ./comfyui/setup_comfyui.sh /root/setup_comfyui.sh
COPY ./comfyui/comfyui_launcher.sh /root/.local/bin/comfyui_launcher.sh
RUN chmod 0755 /root/.local/bin/comfyui_launcher.sh

WORKDIR ${UV_PROJECT}


FROM comfyui_ready AS comfyui

ARG COMFYCLI_TAG=0.29.2
ARG COMFYCLI_CUDA=12.8

ENV COMFYCLI_TAG=${COMFYCLI_TAG}
ENV COMFYCLI_CUDA=${COMFYCLI_CUDA}

WORKDIR ${UV_PROJECT}

RUN uv run comfy --here --where local --skip-prompt install --fast-deps --nvidia\
        --cuda-version ${COMFYCLI_CUDA} --version ${COMFYCLI_TAG}

