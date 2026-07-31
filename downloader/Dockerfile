FROM python:3.12-slim

ENV TZ=Asia/Tokyo

COPY requirements.txt /requirements.txt
COPY download.py /download.py
COPY model_list.yaml /model_list.yaml

RUN \
    --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    pip install --upgrade pip && pip install --no-cache-dir -r /requirements.txt

VOLUME ["/workspace/"]

CMD ["python", "/download.py"]
