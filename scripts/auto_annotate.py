#!/usr/bin/env python3
"""
auto_annotate.py — Bước 3: ảnh + phụ đề → annotation.json tự động (model vision tự chọn vùng)

Mỗi cảnh đúng MỘT lời gọi vision (routing.vision, trần tổng: limits.maxVisionCalls):
gửi ẢNH THẬT + các cue phụ đề của cảnh, yêu cầu model trả về danh sách vùng chữ nhật
theo đúng thứ tự kể chuyện. Script sau đó:
  - kiểm tra/clip tọa độ vào canvas thật (đọc bằng PIL), scale nếu model dùng hệ 0-1000
  - dựng timeline TUẦN TỰ cho các vùng trong sceneDurationMs (không chồng lấn startMs)
  - ghi <png>.annotation.json đúng schema preview.html / render_stream_whiteboard.py

Cách dùng:
  python scripts/auto_annotate.py --proj assets/whiteboard/myproject [--only 1] [--force]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from google_lib import GoogleClient, load_config, extract_json  # noqa: E402

try:
    from PIL import Image
except ImportError:
    Image = None

SYSTEM_VISION = """Bạn là chuyên gia gán vùng (annotation) cho hoạt hình bảng trắng vẽ tay.
Nhiệm vụ: nhìn ẢNH và đọc các CUE phụ đề tương ứng, rồi chia ảnh thành 2–4 vùng chữ nhật
KHÔNG overlap nhiều, mỗi vùng gắn với một phần nội dung theo ĐÚNG thứ tự kể chuyện
(mở bối cảnh → nhân vật/chủ thể chính → hành động/biến đổi → phản ứng/kết quả).
Quy tắc:
- Vùng phải bao trọn chủ thể nhìn thấy trong ảnh; không cắt đôi chủ thể.
- Vẽ theo hướng từ trái sang phải nếu câu chuyện tuyến tính, hoặc theo bố cục tự nhiên của ảnh.
- Trả về JSON duy nhất:
{"elements": [{"label": "tên vùng tiếng Việt ngắn", "narrativeRole": "bối cảnh|chủ thể|hành động|phản ứng",
  "cueRange": [startCueIndex, endCueIndex], "type": "structure|character|action|reaction",
  "bbox": {"x": int, "y": int, "width": int, "height": int}, "direction": "top_to_bottom|left_to_right"}]}
- bbox theo HỆ TOẠ ĐỘ PIXEL của ảnh gốc (canvas width/height được cho trong prompt)."""


def build_annotation(scene: dict, image_path: Path, elements: list[dict],
                     canvas_w: int, canvas_h: int) -> dict:
    cues = scene["cues"]
    dur = int(scene["sceneDurationMs"])
    n = max(1, len(elements))
    lead = min(500, int(dur * 0.05))
    tail_hold = 500
    drawable = max(1000, dur - lead - tail_hold - 200 * (n - 1))
    # chia thời lượng theo số cue của từng vùng (tuần tự, không chồng lấn)
    weights = []
    for el in elements:
        cr = el.get("cueRange") or [1, len(cues)]
        try:
            a, b = int(cr[0]), int(cr[1])
        except Exception:
            a, b = 1, len(cues)
        span = sum(c["endMs"] - c["startMs"] for c in cues[max(0, a - 1):min(len(cues), b)])
        weights.append(max(span, dur / (n * 2)))
    wsum = sum(weights) or 1
    cursor = lead
    out_elements = []
    for i, el in enumerate(elements):
        d = int(drawable * weights[i] / wsum)
        d = max(800, d)
        x = int(el.get("bbox", {}).get("x", 0))
        y = int(el.get("bbox", {}).get("y", 0))
        w = int(el.get("bbox", {}).get("width", canvas_w // n))
        h = int(el.get("bbox", {}).get("height", canvas_h))
        # clip vào canvas
        x = max(0, min(canvas_w - 10, x)); y = max(0, min(canvas_h - 10, y))
        w = max(20, min(canvas_w - x, w)); h = max(20, min(canvas_h - y, h))
        cr = el.get("cueRange") or [1, len(cues)]
        sub = " ".join(c["text"] for c in cues[max(0, int(cr[0]) - 1):int(cr[1])]) if cues else ""
        direction = el.get("direction", "top_to_bottom")
        cx = x + w // 2
        if direction == "left_to_right":
            hp = {"start": [x, y + h // 2], "end": [x + w, y + h // 2]}
        else:
            hp = {"start": [cx, y], "end": [cx, y + h]}
        out_elements.append({
            "id": f"el-{i + 1}",
            "label": el.get("label", f"Vùng {i + 1}"),
            "sequence": i + 1,
            "narrativeRole": el.get("narrativeRole", "chủ thể"),
            "subtitle": sub,
            "type": el.get("type", "character"),
            "region": {"x": x, "y": y, "width": w, "height": h},
            "reveal": {
                "direction": direction,
                "startMs": cursor,
                "durationMs": d,
                "maskPaddingPx": 22,
                "protectedRegions": [],
            },
            "handPath": {**hp, "easing": "easeInOut"},
        })
        cursor += d + 200  # thở 200ms giữa các vùng
    last_end = out_elements[-1]["reveal"]["startMs"] + out_elements[-1]["reveal"]["durationMs"] if out_elements else dur
    return {
        "sceneId": f"scene-{scene['index']:02d}",
        "canvas": {"width": canvas_w, "height": canvas_h},
        "storyBasis": scene.get("summary", ""),
        "sceneDurationMs": max(dur, last_end + 500),
        "elements": out_elements,
    }


def annotate_scene(client: GoogleClient, scene: dict, png: Path, cap: int | None) -> dict | None:
    with Image.open(png) as im:  # noqa: S314 (PIL đã kiểm tra ở import)
        w, h = im.size
    cue_list = "\n".join(f"{i}. {c['text']}" for i, c in enumerate(scene["cues"], 1))
    prompt = (
        f"Canvas: {w} x {h} pixel (origin top-left). Ảnh minh họa cho cảnh {scene['index']}.\n"
        f"Các cue phụ đề theo thứ tự:\n{cue_list}\n\n"
        f"Hãy chia ảnh thành 2–4 vùng theo đúng thứ tự kể chuyện như yêu cầu."
    )
    raw = client.analyze_image(prompt, png, role="vision", cap=cap)
    data = extract_json(raw)
    elements = data.get("elements") if isinstance(data, dict) else data
    if not elements:
        print(f"[err] Cảnh {scene['index']}: vision không trả về elements", file=sys.stderr)
        return None
    # nếu model trả toạ độ tỉ lệ 0..1 hoặc 0..1000 → scale về pixel
    mx = max(int(e.get("bbox", {}).get("x", 0) + e.get("bbox", {}).get("width", 0)) for e in elements)
    my = max(int(e.get("bbox", {}).get("y", 0) + e.get("bbox", {}).get("height", 0)) for e in elements)
    if mx <= 10 and my <= 10:
        for e in elements:
            b = e["bbox"]
            e["bbox"] = {k: int(v * (w if k in ("x", "width") else h)) for k, v in b.items()}
    elif mx <= 1000 and (w > 1000 or h > 1000):
        for e in elements:
            b = e["bbox"]
            e["bbox"] = {k: int(v * (w / 1000) if k in ("x", "width") else v * (h / 1000)) for k, v in b.items()}
    ann = build_annotation(scene, png, elements, w, h)
    ann_path = png.with_suffix(".annotation.json")
    ann_path.write_text(json.dumps(ann, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] Cảnh {scene['index']}: {len(elements)} vùng -> {ann_path.name}", file=sys.stderr)
    return ann


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tự gán vùng annotation bằng model vision (1 lần gọi/cảnh)")
    ap.add_argument("--proj", required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--only", default=None, help="Chỉ làm vài cảnh, vd '1,2'")
    ap.add_argument("--force", action="store_true", help="Ghi đè annotation đã tồn tại")
    args = ap.parse_args(argv)
    if Image is None:
        print("[err] thiếu Pillow: pip install Pillow", file=sys.stderr)
        return 1

    proj = Path(args.proj)
    sb = json.loads((proj / "storyboard.json").read_text(encoding="utf-8-sig"))
    client = GoogleClient(load_config(args.config))
    caps = client.cfg.get("limits", {})
    total_cap = caps.get("maxVisionCalls", 16)

    wanted = {int(x) for x in args.only.split(",")} if args.only else None
    done = 0
    for sc in sb["scenes"]:
        if wanted and sc["index"] not in wanted:
            continue
        img = sc.get("imagePath")
        if not img or not Path(img).exists():
            img_p = sorted(proj.glob(f"scene-{sc['index']:02d}-*.png"))
            if not img_p:
                print(f"[skip] Cảnh {sc['index']}: chưa có ảnh", file=sys.stderr)
                continue
            img = str(img_p[0])
        png = Path(img)
        if png.with_suffix(".annotation.json").exists() and not args.force:
            print(f"[skip] đã có {png.stem}.annotation.json", file=sys.stderr)
            continue
        if annotate_scene(client, sc, png, total_cap) is None:
            return 1
        done += 1
    print(f"[ok] Annotation xong {done} cảnh. Mở assets/preview.html để kiểm tra/chỉnh trước khi render.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
