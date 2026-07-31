#
# Stage1
#
FROM python:3.12-slim AS comfy_ready

# 環境変数の設定 (Pythonのバッファ無効化・対話無効化)
ARG DEBIAN_FRONTEND=noninteractive

WORKDIR /

# 2. 最小限のシステム依存関係のみインストール
RUN \
    --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    git \
    ffmpeg \
    libgl1 \
    libglib2.0-0


# PIP/UV
ARG PIP_BREAK_SYSTEM_PACKAGES=1
ARG PIP_ROOT_USER_ACTION=ignore
ENV PIP_NO_CACHE_DIR=1
RUN pip install --no-cache-dir uv


#
# Stage2
#
FROM comfy_ready AS comfy_only

RUN mkdir -p /root/comfy
WORKDIR /root/comfy

ENV PATH=/root/.local/bin:$PATH

# comfy-cliはvenv前提で作られているので必須
RUN uv venv --no-managed-python --no-python-downloads --seed .venv

# ComfyUI 
ARG COMFYUI_VER=0.28.3
ARG CUDA_VER=13.0

RUN uv pip install comfy-cli &&\
    uv run comfy --here --where local --skip-prompt install --skip-manager --fast-deps --nvidia\
        --cuda-version ${CUDA_VER} --version ${COMFYUI_VER} &&\
    uv cache clear

RUN uv run comfy tracking disable &&\
    uv run comfy set-default --where local /root/comfy/ComfyUI

ENV COMFY_NO_TELEMETRY=1

COPY ./extra_model_paths.yaml /root/comfy/ComfyUI/extra_model_paths.yaml

EXPOSE 8188

VOLUME ["/workspace/"]

CMD ["uv", "run", "comfy", "launch"]

#
# Stage3
#
FROM comfy_only AS runpod_comfyui

ENV TZ=Asia/Tokyo

# 2. 最小限のシステム依存関係のみインストール
RUN \
    --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    init-system-helpers \
    openssh-server \
    procps 

# Open-SSH
EXPOSE 22
COPY ./sshd_config /etc/ssh/sshd_config
RUN chmod 0644 /etc/ssh/sshd_config
RUN mkdir -p /root/.ssh && chmod 0700 /root/.ssh

# tini
ARG TINI_VERSION=v0.19.0
ADD https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini /tini
RUN chmod +x /tini

# 起動スクリプト
COPY ./comfyui_launcher.sh /root/.local/bin/comfyui_launcher.sh
RUN chmod 0755 /root/.local/bin/comfyui_launcher.sh

COPY ./start.sh /root/start.sh
RUN chmod 0755 /root/start.sh

ENTRYPOINT [ "/tini", "--", "/usr/bin/bash", "/root/start.sh" ]

    
