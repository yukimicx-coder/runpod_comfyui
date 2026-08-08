#!/usr/bin/bash

pushd ~/comfy > /dev/null

COMFY_CLI="uv run comfy --json --recent"

env_json=$($COMFY_CLI env)


_bg=--background

_cpu=
if [ "$(echo $env_json | jq -r .data.hardware.gpu)" == "null" ]; then
    _cpu=--cpu
fi

_verbose=
[ -n "$COMFY_VERBOSE" ] && _verbose=--verbose

_listen=
[ -n "$COMFY_LISTEN" ] && _listen="--listen $COMFY_LISTEN"

_port="--port 8188"
[ -n "$COMFY_PORT" ] && _port="--port $COMFY_PORT"

_emp_yaml="--extra-model-paths-config /root/comfy/extra_model_paths.yaml"

while [ -n "$1" ]; do
    case "$1" in
        stop)
            opt_stop=1
            ;;
        foreground)
            _bg=
            ;;
        listen)
            _listen="--listen $2"
            shift
            ;;
        port)
            _port="$2"
            shift
            ;;
        *)
            echo "unknown option: $1"
            ;;
    esac
    shift
done

# 常にstopする
$COMFY_CLI stop >& /dev/null

env_json=$($COMFY_CLI env)
if [ "$(echo $env_json | jq -r .data.server.running)" != "false" ]; then
    echo "ComfyUI is still running. kill it"
    pid=$(ps wx | grep [p]ython.*main.py | awk '{print $1}')
    kill -9 $pid

    env_json=$($COMFY_CLI env)
    if [ "$(echo $env_json | jq -r .data.server.running)" != "false" ]; then
        echo '{ "error": "Cannot kill running ComfyUI." }'
        exit 1
    fi
fi

if [ -n "$opt_stop" ]; then
    echo $env_json
    exit 0
fi

env_json=$($COMFY_CLI launch $_bg -- $_cpu $_verbose $_emp_yaml $_listen $_port $COMFY_ARGS)
if [ "$(echo $env_json | jq -r .error)" != "null" ]; then
    echo $env_json
    exit 1
fi

$COMFY_CLI env

popd > /dev/null

