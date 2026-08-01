FROM python:3.12-slim

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
    git \
    ca-certificates \
    ffmpeg \
    libgl1 \
    libglib2.0-0


# PIP/UV
ARG PIP_BREAK_SYSTEM_PACKAGES=1
ARG PIP_ROOT_USER_ACTION=ignore
ENV PIP_NO_CACHE_DIR=1
RUN pip install --no-cache-dir uv

ENV PATH=/root/.local/bin:$PATH

# Open-SSH
EXPOSE 22
COPY ./sshd_config /etc/ssh/sshd_config
RUN chmod 0644 /etc/ssh/sshd_config
RUN mkdir -p /root/.ssh && chmod 0700 /root/.ssh

ENV TZ=Asia/Tokyo

# 起動スクリプト
COPY ./comfyui_launcher.sh /root/.local/bin/comfyui_launcher.sh
RUN chmod 0755 /root/.local/bin/comfyui_launcher.sh

ENV COMFY_NO_TELEMETRY=1

COPY ./start.sh /start.sh
RUN chmod 0755 /start.sh

EXPOSE 8188
VOLUME ["/workspace/"]

CMD [ "/start.sh" ]

    
