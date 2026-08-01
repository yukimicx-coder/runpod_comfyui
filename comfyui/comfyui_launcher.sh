#!/usr/bin/bash

pushd ~/comfy

COMFY_CLI="uv run comfy"

_bg=--background
_cpu=
uv run python -c 'import torch, sys; sys.exit(0) if torch.cuda.is_available() else sys.exit(1)' || _cpu=--cpu
_verbose=
[ -n "$COMFY_VERBOSE" ] && _verbose=--verbose
_listen=
[ -n "$COMFY_LISTEN" ] && _listen="--listen $COMFY_LISTEN"
_port=8188
[ -n "$COMFY_PORT" ] && _port="$COMFY_PORT"

while [ -n "$1" ]; do
    case "$1" in
        restart)
            opt_restart=1
            ;;
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

check_status() {
    uv run python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:$_port/').getcode() == 200 else 1)" > /dev/null 2>&1
}

if check_status; then
    if [ -z "$opt_restart" ] && [ -z "$opt_stop" ]; then
        echo "ComfyUI is running"
        exit 0
    fi 
    $COMFY_CLI stop
    sleep 2
    if check_status; then
        sleep 5
        if check_status; then
            echo "can not stop ComfyUI server"
            exit 1
        fi
    fi
fi

if [ -n "$opt_stop" ]; then
    exit 0
fi


$COMFY_CLI --recent launch $_bg -- $_cpu $_verbose $_listen --port $_port

popd

