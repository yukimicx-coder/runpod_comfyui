#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

uv pip install --system httpx pyyaml tqdm

python "$SCRIPT_DIR/download.py" $MODEL_DL_ARGS

