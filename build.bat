@echo off

setlocal

pushd %~dp0

set IMG_TAG_base=ghcr.io/yukimicx-coder/runpod

if not defined COMFYCLI_TAG set COMFYCLI_TAG=0.29.2
if not defined COMFYCLI_CUDA set COMFYCLI_CUDA=12.8

call :build comfyui_ready latest

if "%1" == "with_comfyui" goto with_comfyui

goto :EOF

:with_comfyui

call :build comfyui v%COMFYCLI_TAG%_cu%COMFYCLI_CUDA%

goto :EOF

:build
docker build --tag "%IMG_TAG_base%_%1:%2" --target %1 .

goto :EOF

