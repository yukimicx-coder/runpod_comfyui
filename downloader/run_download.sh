#!/bin/bash

if [ -z "$MODEL_DL_AUTOSTART" ] || [ "$MODEL_DL_AUTOSTART" -eq 0 ]; then
    exit 0
fi

if [ ! -f "${MODEL_DL_LIST:=/workspace/models/dl_list.yaml}" ]; then
    echo "$MODEL_DL_LIST is missing"
    exit 1
fi

uv pip install --system huggingface_hub pyyaml requests python-dotenv

python ~/download.py $MODEL_DL_ARGS

exit 0
