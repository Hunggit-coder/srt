#!/usr/bin/env python3
"""
google_lib.py — Client Google Generative Language API (REST, chỉ dùng httpx)

Không cần cài SDK `google-genai`; mọi lời gọi đi qua REST v1beta.
Mọi model được định tuyến theo CHỨC NĂNG trong config/google.json:
  - script : viết kịch bản + SRT        (model text rẻ, vd gemini-2.5-flash)
  - plan   : lập chiến lược minh họa    (model text rẻ)
  - image  : sinh ảnh nét chì           (model sinh ảnh, vd gemini-2.5-flash-image / imagen)
  - vision : đọc ảnh để tự gán vùng     (model đọc ảnh, vd gemini-2.5-flash)
  - tts    : giọng đọc                  (gemini-2.5-flash-preview-tts)
Việc chia nhiều model như trên giúp rải tải, tránh vượt quota của MỘT model
khi pipeline phải gọi nhiều lần (ảnh + vision + tts là 3 nhóm quota riêng).

Cơ chế bảo vệ quota:
  - Nghỉ `sleepBetweenCallsSec` giữa các lần gọi.
  - Retry mũ (exponential backoff) khi gặp HTTP 429/5xx, tối đa `retryMax` lần.
  - Trần số lần gọi cho từng loại (`maxImageCalls`, `maxVisionCalls`, `maxTtsCalls`).

Dùng độc lập để kiểm tra cấu hình/key:
  python scripts/google_lib.py --check          # gọi thử 1 câu text ngắn
  python scripts/google_lib.py --list-voices    # liệt kê giọng TTS khả dụng
  python scripts/google_lib.py --gen-img "con mèo" -o /tmp/cat.png
"""
from __future__ import annotations

import base64
import json
import re
import sys
import time
import wave
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover
    print("[err] thiếu httpx: pip install httpx", file=sys.stderr)
    raise

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SKILL_ROOT / "config" / "google.json"


# ──────────────────────────────────────────────────────────────
# Cấu hình
# ──────────────────────────────────────────────────────────────
def load_config(path: str | Path | None = None) -> dict:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    else:
        cfg = {}
    key = (cfg.get("apiKey") or "").strip()
    if not key:
        import os
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    cfg["apiKey"] = key
    cfg.setdefault("baseUrl", "https://generativelanguage.googleapis.com")
    cfg.setdefault("apiVersion", "v1beta")
    cfg.setdefault("routing", {})
    cfg.setdefault("limits", {})
    return cfg


class QuotaError(RuntimeError):
    pass


class GoogleClient:
    def __init__(self, config: dict | None = None, config_path: str | Path | None = None):
        self.cfg = config if config is not None else load_config(config_path)
        if not self.cfg.get("apiKey"):
            raise RuntimeError(
                "Thiếu API key: điền apiKey vào config/google.json "
                "hoặc đặt biến môi trường GEMINI_API_KEY."
            )
        self.base = f"{self.cfg['baseUrl'].rstrip('/')}/{self.cfg['apiVersion']}"
        self.headers = {"x-goog-api-key": self.cfg["apiKey"], "Content-Type": "application/json"}
        limits = self.cfg.get("limits", {})
        self.sleep_between = float(limits.get("sleepBetweenCallsSec", 2.0))
        self.retry_max = int(limits.get("retryMax", 3))
        self.backoff_base = float(limits.get("retryBackoffBaseSec", 4.0))
        self._call_counters: dict[str, int] = {}
        self._last_call_ts = 0.0

    # helpers ---------------------------------------------------
    def _route(self, role: str) -> dict:
        route = self.cfg.get("routing", {}).get(role)
        if not route or not route.get("model"):
            raise RuntimeError(f"config/google.json thiếu routing.{role}.model")
        return route

    def _check_quota(self, bucket: str, cap: int | None):
        if cap is None:
            return
        n = self._call_counters.get(bucket, 0)
        if n >= cap:
            raise QuotaError(
                f"Trần an toàn quota cho '{bucket}' đã đạt ({cap} lần gọi). "
                f"Nếu muốn gọi tiếp, tăng limits trong config/google.json hoặc chạy lại theo mẻ."
            )

    def _throttle(self):
        wait = self.sleep_between - (time.monotonic() - self._last_call_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_call_ts = time.monotonic()

    def _post(self, model: str, method: str, payload: dict, bucket: str,
              cap: int | None = None) -> dict:
        self._check_quota(bucket, cap)
        url = f"{base_join(self.base, model)}:{method}"
        last_err: Exception | None = None
        for attempt in range(self.retry_max + 1):
            self._throttle()
            try:
                with httpx.Client(timeout=httpx.Timeout(180.0, connect=20.0)) as client:
                    resp = client.post(url, headers=self.headers, json=payload)
            except httpx.HTTPError as e:
                last_err = e
                delay = self.backoff_base * (2 ** attempt)
                print(f"[warn] lỗi mạng {e.__class__.__name__}, nghỉ {delay:.0f}s rồi thử lại "
                      f"({attempt + 1}/{self.retry_max})", file=sys.stderr)
                time.sleep(delay)
                continue
            if resp.status_code == 200:
                self._call_counters[bucket] = self._call_counters.get(bucket, 0) + 1
                return resp.json()
            if resp.status_code in (429, 500, 502, 503):
                delay = self.backoff_base * (2 ** attempt)
                print(f"[warn] HTTP {resp.status_code} (quota/loris), nghỉ {delay:.0f}s rồi thử lại "
                      f"({attempt + 1}/{self.retry_max})", file=sys.stderr)
                time.sleep(delay)
                continue
            raise RuntimeError(f"HTTP {resp.status_code} {url}\n{resp.text[:500]}")
        raise RuntimeError(f"Mọi lần gọi '{model}:{method}' đều thất bại: {last_err}")

    # API -------------------------------------------------------
    def generate_text(self, role: str, prompt: str, system: str | None = None,
                      max_output_tokens: int = 8192) -> str:
        """Sinh văn bản theo routing role (script/plan/vision...). Trả về chuỗi thuần."""
        route = self._route(role)
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        gen_cfg: dict = {"maxOutputTokens": max_output_tokens}
        if route.get("temperature") is not None:
            gen_cfg["temperature"] = route["temperature"]
        payload: dict = {"contents": contents, "generationConfig": gen_cfg}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        data = self._post(route["model"], "generateContent", payload, bucket=f"text:{role}")
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts).strip()

    def generate_json(self, role: str, prompt: str, system: str | None = None,
                      max_output_tokens: int = 8192) -> dict | list:
        """Như generate_text nhưng ép JSON object và parse có dung sai."""
        s = (system or "") + "\nTrả về DUY NHẤT một JSON hợp lệ, không markdown fence, không giải thích."
        raw = self.generate_text(role, prompt, system=s.strip(), max_output_tokens=max_output_tokens)
        return extract_json(raw)

    def generate_image(self, prompt: str, out_png: Path, aspect_ratio: str = "16:9") -> Path:
        """Sinh 1 ảnh theo routing.image; lưu PNG; trả về đường dẫn."""
        route = self._route("image")
        caps = self.cfg.get("limits", {})
        gen_cfg: dict = {"responseModalities": route.get("responseModalities", ["IMAGE"])}
        if aspect_ratio:
            gen_cfg["imageConfig"] = {"aspectRatio": aspect_ratio}
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": gen_cfg}
        data = self._post(route["model"], "generateContent", payload,
                          bucket="image", cap=caps.get("maxImageCalls"))
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        for p in parts:
            inline = p.get("inlineData") or p.get("inline_data")
            if inline and inline.get("data"):
                img_bytes = base64.b64decode(inline["data"])
                out_png.parent.mkdir(parents=True, exist_ok=True)
                out_png.write_bytes(img_bytes)
                return out_png
        raise RuntimeError("Model ảnh không trả về inlineData (bị chặn nội dung?)")

    def analyze_image(self, prompt: str, image_path: Path, role: str = "vision",
                      cap: int | None = None) -> str:
        """Gửi ảnh + câu hỏi cho model vision (đọc bố cục ảnh để tự gán vùng...)."""
        route = self._route(role)
        mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        b64 = base64.b64encode(image_path.read_bytes()).decode()
        payload = {"contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"inlineData": {"mimeType": mime, "data": b64}},
        ]}], "generationConfig": {"temperature": route.get("temperature", 0.1),
                                   "maxOutputTokens": 8192}}
        data = self._post(route["model"], "generateContent", payload,
                          bucket=f"vision:{image_path.name}", cap=cap)
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts).strip()

    def tts_to_wav(self, text: str, out_wav: Path, voice_name: str | None = None,
                   model: str | None = None) -> Path:
        """Tổng hợp giọng đọc → WAV PCM 24kHz mono."""
        caps = self.cfg.get("limits", {})
        voice_cfg = self.cfg.get("voice", {})
        mdl = model or voice_cfg.get("model") or self._route("tts").get("model")
        if not mdl:
            raise RuntimeError("config thiếu voice.model / routing.tts.model")
        vn = voice_name or voice_cfg.get("voiceName", "Aoede")
        payload = {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speech": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": vn}}},
            },
        }
        data = self._post(mdl, "generateContent", payload,
                          bucket="tts", cap=caps.get("maxTtsCalls"))
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        for p in parts:
            inline = p.get("inlineData") or p.get("inline_data")
            if inline and inline.get("data"):
                pcm = base64.b64decode(inline["data"])
                mime = inline.get("mimeType", "audio/L16;codec=pcm;rate=24000")
                rate = 24000
                m = re.search(r"rate=(\d+)", mime)
                if m:
                    rate = int(m.group(1))
                write_wav(out_wav, pcm, rate)
                return out_wav
        raise RuntimeError("TTS không trả về audio (kiểm tra model preview-tts và quyền key)")

    def list_voices(self) -> list[str]:
        """Liệt kê giọng có hỗ trợ TTS preview (vd: voices/Kore)."""
        url = f"{self.base}/voices?key={self.cfg['apiKey']}"
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        out: list[str] = []
        for v in resp.json().get("voices", []):
            if "PREVIEWS-TTS-1" in (v.get("supportedModelNames") or []):
                out.append(v.get("name", "").replace("voices/", ""))
        return out


def base_join(base: str, model: str) -> str:
    return f"{base}/models/{model}"


def write_wav(path: Path, pcm16: bytes, rate: int, channels: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)
    return path


def wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as w:
        return int(w.getnframes() / w.getframerate() * 1000)


def extract_json(raw: str):
    """Trích JSON từ text có thể lẫn markdown fence/giải thích."""
    s = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = s.find(opener), s.rfind(closer)
        if i != -1 and j > i:
            frag = s[i:j + 1]
            try:
                return json.loads(frag)
            except json.JSONDecodeError:
                # thử xoá dấu phẩy thừa
                frag2 = re.sub(r",\s*([}\]])", r"\1", frag)
                try:
                    return json.loads(frag2)
                except json.JSONDecodeError:
                    continue
    raise ValueError(f"Không parse được JSON từ model:\n{raw[:600]}")


# CLI kiểm tra nhanh --------------------------------------------
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Kiểm tra cấu hình Google AI")
    ap.add_argument("--config", default=None)
    ap.add_argument("--check", action="store_true", help="gọi thử 1 câu text ngắn")
    ap.add_argument("--list-voices", action="store_true")
    ap.add_argument("--gen-img", metavar="PROMPT")
    ap.add_argument("-o", "--out", default="/tmp/gen-test.png")
    args = ap.parse_args(argv)
    client = GoogleClient(load_config(args.config))
    if args.list_voices:
        for v in client.list_voices():
            print(v)
        return 0
    if args.gen_img:
        p = client.generate_image(args.gen_img, Path(args.out),
                                  client.cfg.get("image", {}).get("aspectRatio", "16:9"))
        print(f"OK -> {p}")
        return 0
    # mặc định / --check
    out = client.generate_text("script", "Trả lời đúng 1 câu: bạn là mô hình nào?")
    print("[ok] Text API hoạt động. Model trả lời:", out[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
