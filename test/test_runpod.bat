@echo off

setlocal

set COMFYCLI_TAG=0.29.2
set COMFYCLI_CUDA=12.8

set COMFYUI_TAR_ROOT=U:\Docker\RunPod\archive

set WORKSPACE=u:\workspace

set compose_files=-f docker-compose.yaml

set work_dir=%~dp0client
if "%1" == "server" (
    set work_dir=%~dp0server
)

if "%1" == "downloader" (
    set work_dir=%~dp0downloader
) else (
    if "%USERDOMAIN%" == "KUROSUKE" set compose_files=%compose_files% -f GPU-compose.yaml
)

pushd %work_dir%
docker compose %compose_files% up -d

