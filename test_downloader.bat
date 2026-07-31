@echo off

setlocal

pushd %~dp0downloader

if "%1" == "build" goto BUILD

goto RUN

:BUILD
docker build --tag ghcr.io/yukimicx-coder/runpod_model_downloader:latest .

:RUN
set _mount_opt=--mount type=bind,source=..\workspace,target=/workspace
set _env_opt=--env-file .env
set _network_opt=--network comfy_net

docker run -d --rm --name runpod_downloader_test %_mount_opt% %_env_opt% %_network_opt% ghcr.io/yukimicx-coder/runpod_model_downloader:latest
