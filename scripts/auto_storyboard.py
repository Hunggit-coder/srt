#!/usr/bin/env python3
"""
auto_storyboard.py — Bước 1 của pipeline tự động: Ý tưởng → Kịch bản + SRT + Chiến lược minh họa

Chỉ dùng 2 lời gọi text (model rẻ theo routing.script và routing.plan trong config/google.json):
  1. Viết kịch bản voice-over chia theo cảnh (mỗi cảnh ~25–35 giây) + sinh SRT với timecode thật.
  2. Lập chiến lược minh họa cho từng cảnh (1 ý chính/cảnh, mô tả chủ thể để ra ảnh).

Kết quả (lưu vào <outdir>):
  script.md                 — kịch bản đầy đủ
  subtitles.srt             — phụ đề có timecode (đúng định dạng parse_srt.py đọc được)
  storyboard.json           — gộp: scenes[{index, startMs, endMs, sceneDurationMs, cues[], artDirection{...}}]
  plan.txt                  — tóm tắt chiến lược để người dùng duyệt nhanh

Sau bước này hãy DUYỆT storyboard.json / subtitles.srt trước khi chạy auto_generate_images.py.

Cách dùng:
  python scripts/auto_storyboard.py "Ý tưởng: vì sao mèo luôn đáp đất bằng bốn chân" \
      --outdir assets/whiteboard/myproject [--duration-total 90] [--lang vi]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from google_lib import GoogleClient, load_config  # noqa: E402

SYSTEM_SCRIPT = """Bạn là biên kịch video kiến thức dạng kể chuyện (voice-over) cho hoạt hình bảng trắng.
Quy tắc cứng:
- Chia nội dung thành các CẢNH, mỗi cảnh dài 25–35 giây nói (khoảng 65–90 từ tiếng Việt đã dấu / cảnh ở tốc độ đọc vừa).
- Mỗi cảnh CHỈ truyền tải MỘT ý chính; mở đầu hấp dẫn, kết thúc có tổng kết ngắn.
- Trong mỗi cảnh chia thành 3–5 CUE phụ đề, mỗi cue 6–20 từ, câu ngắn gọn khẩu ngữ.
- Timecode: cue đầu tiên bắt đầu tại 00:00:00,000; các cue nối tiếp nhau KHÔNG chồng lấn,
  mỗi cue bắt đầu ngay khi cue trước kết thúc (liền mạch), kết thúc đúng lúc sang cue sau.
- Trả lời bằng JSON duy nhất theo schema:
{"title": string, "scenes": [{"index": 1, "summary": "tóm tắt 1 câu",
  "cues": [{"start":"HH:MM:SS,mmm","end":"HH:MM:SS,mmm","text":"..."}]}]}"""

SYSTEM_PLAN = """Bạn là đạo diễn hình ảnh cho hoạt hình vẽ tay trên nền giấy màu be ấm (#F5EBD7), nét chì xám tối giản kiểu Notion doodle.
Mỗi cảnh chỉ minh họa MỘT ý chính bằng hình ảnh ẩn dụ trực quan, nhiều khoảng trống giữa các chủ thể.
TUYỆT đối không dùng chữ viết/từ/số/nhãn trong khung hình.
Trả lời bằng JSON duy nhất theo schema:
{"scenes": [{"index": 1, "coreIdea": "ý chính", "subjects": ["chủ thể 1", "chủ thể 2", "chủ thể 3"],
  "composition": "bố cục trái/phải/giữa", "imagePrompt": "mô tả tiếng Anh chi tiết để sinh ảnh nét chì 16:9"}]}"""


def fmt_ms(ms: int) -> str:
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def parse_tc(tc: str) -> int:
    import re
    m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", tc.strip())
    if not m:
        raise ValueError(f"timecode lỗi: {tc}")
    h, mi, s, ms = m.groups()
    return ((int(h) * 60 + int(mi)) * 60 + int(s)) * 1000 + int(ms.ljust(3, "0"))


def build_srt(scenes: list[dict]) -> str:
    blocks, idx = [], 1
    for sc in scenes:
        for cue in sc["cues"]:
            blocks.append(f"{idx}\n{cue['start']} --> {cue['end']}\n{cue['text']}\n")
            idx += 1
    return "\n".join(blocks)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ý tưởng → kịch bản + SRT + chiến lược minh họa (2 lần gọi model text)")
    ap.add_argument("idea", help="Ý tưởng video (1–3 câu cũng đủ)")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--duration-total", type=int, default=0, help="Tổng thời lượng mục tiêu (giây); 0 = model tự quyết (3–5 cảnh)")
    ap.add_argument("--lang", default="vi", help="Ngôn ngữ kịch bản (vi/en)")
    args = ap.parse_args(argv)

    client = GoogleClient(load_config(args.config))
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    dur_hint = f"- Tổng thời lượng mục tiêu xấp xỉ {args.duration_total} giây." if args.duration_total else "- Tự chọn số cảnh phù hợp (thường 3–5 cảnh)."
    user_prompt = (
        f"Ý tưởng video: {args.idea}\n"
        f"Ngôn ngữ kịch bản: {args.lang}\n" + dur_hint +
        "\nViết kịch bản voice-over chia cảnh kèm cue phụ đề và timecode như yêu cầu."
    )
    print("[..] Gọi model SCRIPT (routing.script) viết kịch bản + SRT ...", file=sys.stderr)
    sb = client.generate_json("script", user_prompt, system=SYSTEM_SCRIPT)
    scenes = sb.get("scenes") or []
    if not scenes:
        print("[err] Model không trả về scenes", file=sys.stderr)
        return 1

    # chuẩn hoá & sửa timecode: đảm bảo liền mạch, tăng đơn điệu
    cursor = 0
    for sc in scenes:
        fixed = []
        for cue in sc["cues"]:
            try:
                st, en = parse_tc(cue["start"]), parse_tc(cue["end"])
            except ValueError:
                st, en = cursor, cursor + 4000
            st = max(st, cursor)
            if en <= st:
                en = st + 4000
            cue["startMs"], cue["endMs"] = st, en
            cue["start"], cue["end"] = fmt_ms(st), fmt_ms(en)
            fixed.append(cue)
            cursor = en
        sc["cues"] = fixed
        sc["startMs"] = sc["cues"][0]["startMs"]
        sc["endMs"] = sc["cues"][-1]["endMs"]
        sc["sceneDurationMs"] = sc["endMs"] - sc["startMs"]
        sc["text"] = " ".join(c["text"] for c in sc["cues"])

    print("[..] Gọi model PLAN (routing.plan) lập chiến lược minh họa ...", file=sys.stderr)
    plan_input = json.dumps(
        {"scenes": [{"index": s["index"], "text": s["text"]} for s in scenes]},
        ensure_ascii=False)
    plan = client.generate_json("plan",
                                "Dựa trên kịch bản sau, lập chiến lược minh họa cho TỪNG cảnh:\n" + plan_input,
                                system=SYSTEM_PLAN)
    plans = {p.get("index"): p for p in plan.get("scenes", [])}

    for sc in scenes:
        p = plans.get(sc["index"], {})
        sc["artDirection"] = {
            "coreIdea": p.get("coreIdea", sc["summary"]),
            "subjects": p.get("subjects", []),
            "composition": p.get("composition", ""),
            "imagePrompt": p.get("imagePrompt", ""),
        }

    (outdir / "subtitles.srt").write_text(build_srt(scenes), encoding="utf-8")
    (outdir / "storyboard.json").write_text(
        json.dumps({"title": sb.get("title", ""), "scenes": scenes}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    md = [f"# {sb.get('title', 'Video whiteboard')}", ""]
    for sc in scenes:
        md.append(f"## Cảnh {sc['index']} ({fmt_ms(sc['startMs'])} → {fmt_ms(sc['endMs'])})")
        md.append(f"- Tóm tắt: {sc['summary']}")
        ad = sc["artDirection"]
        md.append(f"- Ý minh họa: {ad['coreIdea']} | Chủ thể: {', '.join(ad['subjects'])}")
        for c in sc["cues"]:
            md.append(f"  - [{c['start']} → {c['end']}] {c['text']}")
        md.append("")
    (outdir / "script.md").write_text("\n".join(md), encoding="utf-8")
    (outdir / "plan.txt").write_text("\n\n".join(
        f"Cảnh {sc['index']}: {sc['artDirection']['coreIdea']}\n  Prompt: {sc['artDirection']['imagePrompt'][:300]}"
        for sc in scenes), encoding="utf-8")

    total_s = scenes[-1]["endMs"] / 1000
    print(f"[ok] {len(scenes)} cảnh, tổng {total_s:.0f}s. Đã lưu: {outdir}/subtitles.srt, storyboard.json, script.md, plan.txt",
          file=sys.stderr)
    print(outdir / "storyboard.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
