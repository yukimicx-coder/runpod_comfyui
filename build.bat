@echo off

setlocal

pushd %~dp0

set IMG_TAG_base=ghcr.io/yukimicx-coder/runpod_comfyui

if not defined COMFYCLI_TAG set COMFYCLI_TAG=0.29.2
if not defined COMFYCLI_CUDA set COMFYCLI_CUDA=12.8

docker build --tag "%IMG_TAG_base%_ready:latest" --target runpod  .

if "%1" == "with_comfyui" goto with_comfyui

goto :EOF

:with_comfyui

docker build --tag "%IMG_TAG_base%:v%COMFYCLI_TAG%_cu%COMFYCLI_CUDA%" --target runpod_with_comfyui .

