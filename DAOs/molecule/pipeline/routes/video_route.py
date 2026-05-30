"""Video routing: ffmpeg frames + whisper.cpp + vision → frames.jsonl."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_PIPELINE = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PIPELINE.parent.parent.parent
_ARTICLE_PIPELINE = _REPO_ROOT / "articles" / "pipeline"
if str(_ARTICLE_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_ARTICLE_PIPELINE))

from openai import OpenAI

from vision_client import load_image, vision_describe  # noqa: E402

from ._utils import parse_json_response, safe_stem

_PROMPTS = _PIPELINE / "prompts"

WHISPER_CPP_BIN = os.environ.get("WHISPER_CPP_BIN", "whisper-cli")
WHISPER_MODEL_PATH = os.environ.get("WHISPER_MODEL_PATH", "models/ggml-small.bin")
AUDIO_CHUNK_SEC = 5.0


def _run(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )


def _probe_duration_sec(video: Path) -> float:
    result = _run([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video),
    ])
    return float(result.stdout.strip())


def _frame_interval_sec(duration: float) -> float:
    return 5.0 if duration < 300.0 else 10.0


def _extract_frame_at(video: Path, timestamp_sec: float, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y",
        "-ss", str(timestamp_sec),
        "-i", str(video),
        "-frames:v", "1",
        "-q:v", "2",
        str(dest),
    ])


def _extract_audio_wav(video: Path, dest: Path) -> None:
    _run([
        "ffmpeg", "-y",
        "-i", str(video),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(dest),
    ])


def _parse_whisper_time(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if value > 1000 else float(value)
    if isinstance(value, str):
        # "00:00:05,000" or "5.2"
        if ":" in value:
            parts = value.replace(",", ".").split(":")
            if len(parts) == 3:
                h, m, s = parts
                return int(h) * 3600 + int(m) * 60 + float(s)
        return float(value)
    return 0.0


def _transcribe_whisper_segments(wav_path: Path) -> list[dict[str, Any]]:
    """Run whisper.cpp and return segments with start/end/text."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        out_prefix = tmp_path / "whisper_out"
        cmd = [
            WHISPER_CPP_BIN,
            "-m", WHISPER_MODEL_PATH,
            "-f", str(wav_path),
            "-oj",
            "-of", str(out_prefix),
        ]
        try:
            _run(cmd)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            raise RuntimeError(
                f"whisper.cpp failed ({WHISPER_CPP_BIN}). "
                "Set WHISPER_CPP_BIN and WHISPER_MODEL_PATH."
            ) from e

        json_path = out_prefix.with_suffix(".json")
        if not json_path.exists():
            candidates = list(tmp_path.glob("*.json"))
            if not candidates:
                return []
            json_path = candidates[0]

        data = json.loads(json_path.read_text(encoding="utf-8"))
        transcription = data.get("transcription", data)
        if isinstance(transcription, list):
            segments = []
            for item in transcription:
                if not isinstance(item, dict):
                    continue
                offsets = item.get("offsets") or item.get("timestamps") or {}
                start = _parse_whisper_time(offsets.get("from", item.get("start", 0)))
                end = _parse_whisper_time(offsets.get("to", item.get("end", start)))
                segments.append({
                    "start": start,
                    "end": end if end > start else start + AUDIO_CHUNK_SEC,
                    "text": str(item.get("text", "")).strip(),
                })
            return segments

        if isinstance(transcription, str):
            return [{"start": 0.0, "end": 0.0, "text": transcription.strip()}]
        return []


def _normalize_whisper_segments(raw_segments: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    """Group whisper output into 5-second audio chunks."""
    if not raw_segments:
        return []

    # If segments already have reasonable timestamps
    if any(s.get("end", 0) > 0 for s in raw_segments):
        chunks: list[dict[str, Any]] = []
        t = 0.0
        while t < duration:
            end = min(t + AUDIO_CHUNK_SEC, duration)
            texts = [
                s["text"]
                for s in raw_segments
                if s.get("text")
                and float(s.get("start", 0)) < end
                and float(s.get("end", duration)) > t
            ]
            chunks.append({
                "start_sec": t,
                "end_sec": end,
                "text": " ".join(texts).strip(),
            })
            t = end
        return chunks

    # Single blob — split evenly by 5s windows
    full_text = " ".join(s.get("text", "") for s in raw_segments).strip()
    if not full_text:
        return []
    words = full_text.split()
    if not words:
        return []
    chunks = []
    window_count = max(1, int(duration // AUDIO_CHUNK_SEC) + (1 if duration % AUDIO_CHUNK_SEC else 0))
    per_window = max(1, len(words) // window_count)
    t = 0.0
    idx = 0
    while t < duration and idx < len(words):
        end = min(t + AUDIO_CHUNK_SEC, duration)
        chunk_words = words[idx : idx + per_window]
        idx += per_window
        chunks.append({"start_sec": t, "end_sec": end, "text": " ".join(chunk_words)})
        t = end
    return chunks


def _audio_for_window(
    chunks: list[dict[str, Any]],
    window_start: float,
    window_end: float,
) -> str:
    texts = [
        c.get("text", "")
        for c in chunks
        if c.get("text")
        and c["start_sec"] < window_end
        and c["end_sec"] > window_start
    ]
    return " ".join(texts).strip()


def process_video(
    source: Path,
    bundle_dir: Path,
    *,
    vision_client: OpenAI,
    vision_model: str,
    keep_temp: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    stem = safe_stem(source.name)
    video_dir = bundle_dir / "videos" / stem
    temp_dir = video_dir / "_temp" / "frames"
    jsonl_path = video_dir / "frames.jsonl"

    if jsonl_path.exists() and not overwrite:
        return {"route": "video", "output_path": str(jsonl_path), "skipped": True}

    video_dir.mkdir(parents=True, exist_ok=True)
    duration = _probe_duration_sec(source)
    interval = _frame_interval_sec(duration)

    timestamps: list[float] = []
    t = 0.0
    while t <= duration:
        timestamps.append(round(t, 3))
        t += interval
    if not timestamps:
        timestamps = [0.0]

    with tempfile.TemporaryDirectory() as audio_tmp:
        wav_path = Path(audio_tmp) / "audio.wav"
        try:
            _extract_audio_wav(source, wav_path)
            raw_segments = _transcribe_whisper_segments(wav_path)
        except RuntimeError:
            raw_segments = []
        audio_chunks = _normalize_whisper_segments(raw_segments, duration)

    prompt = (_PROMPTS / "video_frame_description_prompt.md").read_text(encoding="utf-8")
    lines: list[str] = []

    for idx, ts in enumerate(timestamps):
        frame_name = f"frame_{int(ts * 1000)}.png"
        frame_path = temp_dir / frame_name
        _extract_frame_at(source, ts, frame_path)

        pil = load_image(frame_path)
        raw = vision_describe(vision_client, vision_model, pil, prompt, max_tokens=2048)
        try:
            frame_data = parse_json_response(raw)
        except (json.JSONDecodeError, ValueError):
            frame_data = {"description": raw, "transcribed_text": "", "labels": []}

        window_start = 0.0 if idx == 0 else timestamps[idx - 1]
        window_end = ts if ts > window_start else window_start + interval

        record = {
            "timestamp_sec": ts,
            "frame_file": f"_temp/frames/{frame_name}",
            "description": frame_data.get("description", ""),
            "transcribed_text": frame_data.get("transcribed_text", ""),
            "labels": frame_data.get("labels", []),
            "audio_transcript": _audio_for_window(audio_chunks, window_start, window_end),
        }
        lines.append(json.dumps(record, ensure_ascii=False))

    jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if not keep_temp and temp_dir.exists():
        shutil.rmtree(video_dir / "_temp", ignore_errors=True)

    return {"route": "video", "output_path": str(jsonl_path), "frame_count": len(lines)}
