#!/usr/bin/env python3
"""
auto_narration.py — Bước 4: phụ đề từng cảnh → file giọng đọc WAV (model TTS)

Chiến lược chống vượt quota:
  - MỘT lời gọi TTS cho CẢ CẢNH (gộp text các cue của cảnh), không gọi theo từng cue.
    => Số lần gọi TTS = số cảnh (thường 3–5), nằm trong limits.maxTtsCalls.
  - Prompt đọc được thiết kế để audio dài KHỚP với sceneDurationMs
    ("đọc trọn nội dung trong khoảng X giây"), vì ảnh/vùng đã dựng timeline theo SRT gốc.
  - Nếu audio lệch quá 20% so với sceneDurationMs, script in cảnh báo (bạn có thể
    chạy lại --only <cảnh> hoặc chỉnh sceneDurationMs trong annotation).

Kết quả: <proj>/narration/scene-XX.wav + ghi đường dẫn vào storyboard.json

Cách dùng:
  python scripts/auto_narration.py --proj assets/whiteboard/myproject [--voice Kore] [--only 1] [--force]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from google_lib import GoogleClient, load_config, wav_duration_ms  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sinh giọng đọc cho từng cảnh bằng model TTS")
    ap.add_argument("--proj", required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--voice", default=None, help="Giọng TTS (mặc định: config.voice.voiceName)")
    ap.add_argument("--only", default=None, help="Chỉ làm vài cảnh, vd '1,2'")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    proj = Path(args.proj)
    sb_path = proj / "storyboard.json"
    sb = json.loads(sb_path.read_text(encoding="utf-8-sig"))
    client = GoogleClient(load_config(args.config))
    voice_cfg = client.cfg.get("voice", {})
    voice = args.voice or voice_cfg.get("voiceName", "Aoede")
    lang = voice_cfg.get("language", "vi-VN")
    style = voice_cfg.get("style", "tự nhiên, rõ ràng")

    nar_dir = proj / "narration"
    nar_dir.mkdir(exist_ok=True)
    wanted = {int(x) for x in args.only.split(",")} if args.only else None

    done = 0
    for sc in sb["scenes"]:
        if wanted and sc["index"] not in wanted:
            continue
        wav = nar_dir / f"scene-{sc['index']:02d}.wav"
        if wav.exists() and not args.force:
            print(f"[skip] đã có {wav.name}", file=sys.stderr)
            sc["narrationPath"] = str(wav)
            continue
        dur_s = sc["sceneDurationMs"] / 1000
        text = sc.get("text") or " ".join(c["text"] for c in sc["cues"])
        tts_prompt = (
            f"Hãy đọc đoạn văn sau bằng tiếng ({lang}) với phong cách: {style}. "
            f"Đọc trọn vẹn, tự nhiên, KHÔNG thêm/bớt chữ, và canh tốc độ để toàn bộ "
            f"phần đọc kéo dài khoảng {dur_s:.0f} giây:\n\n{text}"
        )
        print(f"[..] Cảnh {sc['index']}: gọi TTS ({voice}) ~{dur_s:.0f}s ...", file=sys.stderr)
        try:
            client.tts_to_wav(tts_prompt, wav, voice_name=voice)
        except Exception as e:
            print(f"[err] Cảnh {sc['index']} TTS thất bại: {e}", file=sys.stderr)
            continue
        got = wav_duration_ms(wav) / 1000
        drift = abs(got - dur_s) / max(dur_s, 1)
        flag = "  [CHÊNH LỆNH >20% — cân nhắc chạy lại hoặc chỉnh sceneDurationMs]" if drift > 0.2 else ""
        print(f"[ok] {wav.name}: {got:.1f}s so với mục tiêu {dur_s:.1f}s{flag}", file=sys.stderr)
        sc["narrationPath"] = str(wav)
        sc["narrationDurMs"] = int(got * 1000)
        done += 1

    sb_path.write_text(json.dumps(sb, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] Giọng đọc xong {done} cảnh.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
