#!/usr/bin/env python3
"""
auto_finalize.py — Bước 5: ghép giọng đọc TTS + (tuỳ chọn) phụ đề burned-in vào các cảnh, rồi hợp nhất.

Việc trộn âm thanh/phụ đề KHÔNG cần gọi API — chỉ dùng ffmpeg:
  1. Với mỗi cảnh: lấy <cảnh>-whiteboard.mp4 (video câm) + narration/scene-XX.wav
     → mux thành <cảnh>-final.mp4 (có tiếng). Nếu có --burn-srt, ghi phụ đề lên hình.
  2. Hợp nhất toàn bộ cảnh thành final_video.mp4 (ffmpeg concat, re-encode để đồng nhất).

Nếu thiếu audio/video cho một cảnh, vẫn gắn khoảng lặng đúng thời lượng cảnh.

Cách dùng:
  <ENV_PY> scripts/auto_finalize.py --proj assets/whiteboard/myproject \
      [--video-suffix -whiteboard.mp4] [--burn-srt] [--output final_video.mp4]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run(cmd: list[str]) -> None:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Lệnh thất bại: {' '.join(cmd[:6])}...\n{res.stderr[-800:]}")


def ms_to_srt_tc(ms: int) -> str:
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def write_scene_srt(scene: dict, path: Path) -> None:
    base = scene["startMs"]
    blocks = []
    for i, c in enumerate(scene["cues"], 1):
        blocks.append(f"{i}\n{ms_to_srt_tc(c['startMs'] - base)} --> {ms_to_srt_tc(c['endMs'] - base)}\n{c['text']}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")


def probe_dur(path: Path) -> float:
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 0.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Mux giọng đọc + phụ đề + hợp nhất video hoàn chỉnh")
    ap.add_argument("--proj", required=True)
    ap.add_argument("--video-suffix", default="-whiteboard.mp4")
    ap.add_argument("--burn-srt", action="store_true", help="Ghi phụ đề burned-in lên hình")
    ap.add_argument("--output", default="final_video.mp4")
    args = ap.parse_args(argv)

    if not shutil.which("ffmpeg"):
        print("[err] Cần ffmpeg trong PATH cho bước âm thanh/hợp nhất.", file=sys.stderr)
        return 1

    proj = Path(args.proj)
    sb = json.loads((proj / "storyboard.json").read_text(encoding="utf-8-sig"))
    tmp = Path(tempfile.mkdtemp(prefix="wb-final-"))
    finals: list[Path] = []

    for sc in sb["scenes"]:
        idx = sc["index"]
        candidates = sorted(proj.glob(f"scene-{idx:02d}-*{args.video_suffix}"))
        if not candidates:
            print(f"[skip] Cảnh {idx}: không tìm thấy video '*{args.video_suffix}'", file=sys.stderr)
            continue
        video = candidates[0]
        dur = probe_dur(video) or sc["sceneDurationMs"] / 1000
        wav = proj / "narration" / f"scene-{idx:02d}.wav"
        out = proj / f"scene-{idx:02d}-final.mp4"

        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video)]
        amap = "1:a:0"
        if wav.exists():
            cmd += ["-i", str(wav)]
        else:
            # khoảng lặng vừa đủ cho cảnh
            cmd += ["-f", "lavfi", "-i", f"anullsrc=r=24000:cl=mono:d={dur:.3f}"]
            amap = "1:a:0"
            print(f"[warn] Cảnh {idx}: thiếu WAV, dùng khoảng lặng.", file=sys.stderr)

        vmap = "0:v:0"
        if args.burn_srt:
            srt = tmp / f"scene-{idx:02d}.srt"
            write_scene_srt(sc, srt)
            style = "FontName=Arial,FontSize=16,PrimaryColour=&H00202020,OutlineColour=&H50000000,BorderStyle=3,BackColour=&HA0F5EBD7,MarginV=28"
            safe = srt.as_posix().replace(":", "\\:").replace("'", "\\'")
            cmd += ["-vf", f"subtitles='{safe}':force_style='{style}'"]

        cmd += ["-map", vmap, "-map", amap, "-map_metadata", "-1",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k", "-shortest",
                "-pix_fmt", "yuv420p", str(out)]
        run(cmd)
        finals.append(out)
        print(f"[ok] Cảnh {idx} -> {out.name} ({probe_dur(out):.1f}s)", file=sys.stderr)

    if not finals:
        print("[err] Không có cảnh nào để hợp nhất.", file=sys.stderr)
        return 1

    merged = proj / args.output
    if len(finals) == 1:
        shutil.copy(finals[0], merged)
    else:
        lst = tmp / "list.txt"
        lst.write_text("\n".join(f"file '{p.resolve().as_posix()}'" for p in finals), encoding="utf-8")
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-c:a", "aac", "-b:a", "160k", "-pix_fmt", "yuv420p", str(merged)])
    print(f"[ok] Video hoàn chỉnh: {merged} ({probe_dur(merged):.1f}s)", file=sys.stderr)
    print(merged)
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
