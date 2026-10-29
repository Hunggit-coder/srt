#!/usr/bin/env python3
"""
auto_generate_images.py — Bước 2: storyboard.json → ảnh nét chì cho từng cảnh (model sinh ảnh)

Dùng routing.image trong config/google.json. Mỗi cảnh đúng MỘT lời gọi sinh ảnh
(trần an toàn: limits.maxImageCalls). Prompt luôn được gắn BỘ QUY CHUẨN THỊ GIÁC
thống nhất (giấy be ấm #F5EBD7, nét chì xám, tối giản, KHÔNG chữ/số, 16:9).

Kết quả: <projdir>/scene-XX-<slug>.png + cập nhật đường dẫn vào storyboard.json

Cách dùng:
  python scripts/auto_generate_images.py --proj assets/whiteboard/myproject [--only 1,3] [--force]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from google_lib import GoogleClient, load_config  # noqa: E402

STYLE_GUARDRAIL = (
    "STYLE (mandatory, identical across the whole series): minimal hand-drawn pencil sketch on warm "
    "beige paper background (#F5EBD7), soft dark-grey graphite lines, Notion-style restrained doodle "
    "aesthetic, conceptual not realistic, generous white space between subjects, flat 2D illustration. "
    "Absolutely NO text, NO words, NO letters, NO numbers, NO labels, no photorealism, no 3D, no busy "
    "backgrounds, no saturated colors; at most small red/orange/blue accents. Aspect ratio 16:9."
)


def slugify(text: str) -> str:
    s = unicodedata.normalize("NFD", text or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return (s[:40] or "scene")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sinh ảnh nét chì theo storyboard (mỗi cảnh 1 lần gọi model ảnh)")
    ap.add_argument("--proj", required=True, help="Thư mục dự án chứa storyboard.json")
    ap.add_argument("--config", default=None)
    ap.add_argument("--only", default=None, help="Chỉ sinh vài cảnh, vd: '1,3'")
    ap.add_argument("--force", action="store_true", help="Ghi đè ảnh đã tồn tại")
    args = ap.parse_args(argv)

    proj = Path(args.proj)
    sb_path = proj / "storyboard.json"
    sb = json.loads(sb_path.read_text(encoding="utf-8-sig"))
    client = GoogleClient(load_config(args.config))
    aspect = client.cfg.get("image", {}).get("aspectRatio", "16:9")

    wanted = None
    if args.only:
        wanted = {int(x) for x in re.split(r"[,\s]+", args.only) if x.strip()}

    made = 0
    for sc in sb["scenes"]:
        idx = sc["index"]
        if wanted and idx not in wanted:
            continue
        ad = sc.get("artDirection", {})
        base = f"scene-{idx:02d}-{slugify(ad.get('coreIdea') or sc.get('summary') or '')}"
        png = proj / f"{base}.png"
        if png.exists() and not args.force:
            print(f"[skip] đã có {png.name}", file=sys.stderr)
            sc["imagePath"] = str(png)
            continue
        prompt = (f"{STYLE_GUARDRAIL}\n\nSCENE CONTENT: {ad.get('imagePrompt') or ad.get('coreIdea') or sc['text']}\n"
                  f"Main subjects: {', '.join(ad.get('subjects', []))}.\n"
                  f"Composition: {ad.get('composition', 'balanced, centered with clear left/middle/right zones')}.\n"
                  f"The image must contain exactly 2 to 4 well-separated drawable subjects so later "
                  f"regions can be annotated without heavy overlap.")
        print(f"[..] Cảnh {idx}: gọi model IMAGE -> {png.name}", file=sys.stderr)
        try:
            client.generate_image(prompt, png, aspect_ratio=aspect)
        except Exception as e:
            print(f"[err] Cảnh {idx} thất bại: {e}", file=sys.stderr)
            continue
        sc["imagePath"] = str(png)
        made += 1
        # kích thước thật để các bước sau dùng làm canvas
        try:
            from PIL import Image
            with Image.open(png) as im:
                sc["imageWidth"], sc["imageHeight"] = im.size
        except Exception:
            pass

    sb_path.write_text(json.dumps(sb, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] Đã sinh {made} ảnh. Kiểm tra thủ công trước khi chạy auto_annotate.py.", file=sys.stderr)
    return 0 if made or not wanted else 1


if __name__ == "__main__":
    sys.exit(main())
