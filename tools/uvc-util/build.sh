#!/bin/sh
# Builds uvc-util (macOS only) as a universal binary: tools/uvc-util/build/uvc-util.
# Needs the Xcode command line tools: xcode-select --install
set -eu
cd "$(dirname "$0")"
mkdir -p build
xcrun clang -fno-objc-arc -O2 -arch arm64 -arch x86_64 -mmacosx-version-min=13.0 -framework Foundation -framework IOKit -o build/uvc-util src/uvc-util.m src/UVCController.m src/UVCType.m src/UVCValue.m
echo "built $(pwd)/build/uvc-util"
