#!/bin/bash
set -e # Exit the script if any statement returns a non-true return value

mkdir -p ~/comfy
cd ~/comfy

# comfy-cli needs venv
uv venv --no-managed-python --no-python-downloads --seed .venv

uv pip install comfy-cli
uv run comfy tracking disable 

_restore_opt=
if [ -f ComfyUI/main.py ]; then
    echo "ComfyUI already exists. using restore"
    _restore_opt=--restore
fi

uv run comfy --here --where local --skip-prompt install $_restore_opt --fast-deps --nvidia\
        --cuda-version ${COMFYCLI_CUDA} --version ${COMFYCLI_TAG}
uv cache clear

cd ~

echo "start archiving comfy/* ..."
tar czf /run/output/ComfyUI-v${COMFYCLI_TAG}.tar.gz comfy
