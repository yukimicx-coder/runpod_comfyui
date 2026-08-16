@echo off

setlocal

if "%2" == "" goto HELP

goto INIT

:HELP
echo "usage: test_runpod.bat <workspace> <compose_dir> [use_gpu]"


exit /b 1

:INIT
if not defined COMFYCLI_TAG set COMFYCLI_TAG=0.33.1
if not defined COMFYCLI_CUDA set COMFYCLI_CUDA=13.0

set WORKSPACE=%1

set compose_files=-f docker-compose.yaml

set compose_dir=%~dp0%2
if not exist %compose_dir% (
    echo %compose_dir% is not found
    exit /b 1
)

pushd %compose_dir%

if "%3" == "use_gpu" (
    if exist GPU-compose.yaml set  compose_files=%compose_files% -f GPU-compose.yaml
)

docker compose %compose_files% up -d

popd

goto :EOF

