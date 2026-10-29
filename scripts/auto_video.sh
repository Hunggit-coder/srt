#!/usr/bin/env bash
# auto_video.sh — Pipeline TỰ ĐỘNG: Ý tưởng → Video whiteboard hoàn chỉnh (giọng đọc + phụ đề)
#
# Chỉ cần API key Google trong config/google.json (apiKey) hoặc biến môi trường GEMINI_API_KEY.
#
# Cách dùng:
#   ./scripts/auto_video.sh "Ý tưởng: vì sao mèo luôn đáp đất bằng bốn chân" myproject
#   Bật phụ đề burned-in:  WB_BURN_SRT=1 ./scripts/auto_video.sh "..." myproject
#   Chạy lại từng bước:    pipeline đã skip sản phẩm tồn tại — xoá file tương ứng rồi chạy lại.
set -euo pipefail

IDEA="${1:?Cần ý tưởng video (tham số 1)}"
PROJ_NAME="${2:-demo}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJ="$ROOT/assets/whiteboard/$PROJ_NAME"
PY_SYS="python3"
BURN_FLAG="${WB_BURN_SRT:+--burn-srt}"

echo "== [0] Chuẩn bị môi trường =="
$PY_SYS "$ROOT/scripts/prepare_env.py" > /tmp/wb_env.txt
ENV_PY=$(grep -E '^ENV_PY=' /tmp/wb_env.txt | tail -1 | cut -d= -f2)
echo "ENV_PY=$ENV_PY"
"$ENV_PY" -c "import httpx" 2>/dev/null || "$ENV_PY" -m pip install --quiet httpx

echo "== [1] Ý tưởng → kịch bản + SRT + chiến lược (model text, 2 lần gọi) =="
if [ ! -f "$PROJ/storyboard.json" ]; then
  $PY_SYS "$ROOT/scripts/auto_storyboard.py" "$IDEA" --outdir "$PROJ" ${WB_TOTAL_DURATION:+--duration-total "$WB_TOTAL_DURATION"}
else
  echo "[skip] storyboard.json đã tồn tại (xoá để tạo lại)"
fi

echo "== [2] Sinh ảnh nét chì cho từng cảnh (model ảnh, 1 lần/cảnh) =="
$PY_SYS "$ROOT/scripts/auto_generate_images.py" --proj "$PROJ"

echo "== [3] Tự gán vùng kể chuyện bằng model vision (1 lần/cảnh) =="
$PY_SYS "$ROOT/scripts/auto_annotate.py" --proj "$PROJ"

echo "== [4] Giọng đọc cho từng cảnh (model TTS, 1 lần/cảnh) =="
$PY_SYS "$ROOT/scripts/auto_narration.py" --proj "$PROJ"

echo "== [5] Render từng cảnh (local, KHÔNG tốn quota API) =="
shopt -s nullglob
for png in "$PROJ"/scene-*.png; do
  ann="${png%.png}.annotation.json"
  [ -f "$ann" ] || continue
  out="${png%.png}-whiteboard.mp4"
  [ -f "$out" ] && { echo "[skip] $out"; continue; }
  "$ENV_PY" "$ROOT/scripts/render_stream_whiteboard.py" "$png" "$ann" "$out" \
    "$ROOT/assets/drawing-hand.png" --ink-path grid --color-fill contour-wipe
done

echo "== [6] Ghép giọng đọc + phụ đề + hợp nhất (ffmpeg, không gọi API) =="
"$ENV_PY" "$ROOT/scripts/auto_finalize.py" --proj "$PROJ" $BURN_FLAG

echo ""
echo "XONG ✅  Video hoàn chỉnh: $PROJ/final_video.mp4"
echo "Kiểm tra nhanh: ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 \"$PROJ/final_video.mp4\""
