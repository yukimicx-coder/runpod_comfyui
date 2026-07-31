@echo off

setlocal

pushd %~dp0comfyui

if "%1" == "build_comfyonly" goto BUILD_COMFY

if "%1" == "build" goto BUILD_START

goto RUN

:BUILD_COMFY
docker build --tag local/comfyui_only:latest --target comfy_only .

:BUILD_START
docker build --tag ghcr.io/yukimicx-coder/runpod_comfyui:latest .

:RUN
set _mount_opt=--mount type=bind,source=..\workspace,target=/workspace
set _env_opt=--env-file .env
set _network_opt=--network comfy_net

docker run -d --rm --name runpod_comfyui_test %_mount_opt% %_env_opt% %_network_opt% ghcr.io/yukimicx-coder/runpod_comfyui:latest
