#!/bin/bash

pushd ${UV_PROJECT:-~/comfy}

uv pip install huggingface_hub pyyaml requests python-dotenv

uv run download.py $MODEL_DL_ARGS

popd
