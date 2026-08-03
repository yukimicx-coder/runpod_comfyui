#!/bin/bash

# run this scropt on the container that ComfyUI is already installed.

set -e # Exit the script if any statement returns a non-true return value

pushd ~/comfy

_restore_opt=
if [ -f ComfyUI/main.py ]; then
    echo "ComfyUI already exists. use --restore option"
    _restore_opt=--restore
fi

uv run comfy --here --where local --skip-prompt install $_restore_opt --fast-deps --nvidia\
        --cuda-version ${COMFYCLI_CUDA} --version ${COMFYCLI_TAG}
uv cache clear

popd

echo "start archiving comfy to /workspace ..."
tar czf /workspace/ComfyUI_v${COMFYCLI_TAG}_cu${COMFYCLI_CUDA}.tar.gz ~/comfy
