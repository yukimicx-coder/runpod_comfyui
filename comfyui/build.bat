@echo off

setlocal

set IMG_TAG=ghcr.io/yukimicx-coder/runpod_comfyui_ready:latest

set COMFYCLI_TAG=0.28.3
set COMFYCLI_CUDA=13.0

if "%1" == "skip_build" goto ARCHIVE
docker build --tag "%IMG_TAG%" .

if "%1" == "build_only" goto :EOF

:ARCHIVE

set _mount_opt=--mount type=bind,source=./output/,target=/run/output/
set _mount_opt=%_mount_opt% --mount type=bind,source=./make_comfyui_tar.sh,target=/root/.local/bin/make_comfyui_tar.sh
set _env_opt=-e COMFYCLI_TAG=%COMFYCLI_TAG% -e COMFYCLI_CUDA=%COMFYCLI_CUDA%

if defined COMFYUI_SRC (
    set _mount_opt=%_mount_opt% --mount type=bind,source=%COMFYUI_SRC%,target=/root/comfy/ComfyUI
)

docker run --rm %_mount_opt% %_env_opt% %IMG_TAG% make_comfyui_tar.sh

