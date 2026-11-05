#!/usr/bin/env bash
# 不读取标准输入，原样交给钩子；两条分支都启用 UTF-8 与隔离模式。
if command -v python >/dev/null 2>&1; then
    exec python -X utf8 -I "$@"
else
    exec py -X utf8 -I "$@"
fi
