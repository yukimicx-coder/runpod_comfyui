@echo off

setlocal

pushd %~dp0..\comfyui

set COMFYUI_TAR=.\output\ComfyUI-v0.28.3.tar.gz
set WORKSPACE=u:\ComfyUI

set _mount_opt=--mount type=bind,source=%WORKSPACE%,target=/workspace
set _mount_opt=%_mount_opt% --mount type=bind,source=%COMFYUI_TAR%,target=/workspace/ComfyUI-latest.tar.gz
set _env_opt=--env-file ..\test\comfyui.env
set _network_opt=-p "127.0.0.1:2222:22"

docker run -it --name runpod_comfyui_test %_mount_opt% %_env_opt% %_network_opt% %RUN_OPT% ghcr.io/yukimicx-coder/runpod_comfyui_ready:latest bash
