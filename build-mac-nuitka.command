#!/bin/bash
# Finder 双击入口：实际打包逻辑在 build_mac.sh
cd "$(dirname "$0")" || exit 1
exec ./build_mac.sh
