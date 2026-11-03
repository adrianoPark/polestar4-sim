#!/usr/bin/env bash
# resolve_assemble.py 를 DaVinci Resolve(무료/Studio) Scripts 메뉴에 등록
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
mkdir -p "$DEST"
cp "$HERE/resolve_assemble.py" "$DEST/resolve_assemble.py"
echo "설치됨: $DEST/resolve_assemble.py"
echo "Resolve 재시작 후 Workspace > Scripts > resolve_assemble 로 실행"
command -v ffmpeg >/dev/null || echo "※ ffmpeg 없음: brew install ffmpeg"
