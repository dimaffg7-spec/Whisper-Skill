"""
Общая обвязка для примеров.

Решает две задачи:
1. Автовыбор бэкенда — определяет железо и подгружает оптимальный (mlx / faster-whisper / whisper.cpp)
2. Унифицированный интерфейс — `transcribe(audio_path, language)` возвращает одинаковую структуру вне зависимости от бэкенда

Использование:
    from examples.common import transcribe, save_srt
    result = transcribe("input.mp3", language="ru")
    save_srt(result, "output.srt")
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


# ─── Auto-detect available backends ─────────────────────────────────────────


def _has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def _has_cuda() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


def _pick_backend() -> str:
    """Выбрать оптимальный установленный бэкенд."""
    if _is_apple_silicon() and _has_module("mlx_whisper"):
        return "mlx"
    if _has_module("faster_whisper"):
        return "faster"
    if _has_module("whisperx"):
        return "whisperx"
    if _has_module("pywhispercpp"):
        return "cpp"
    raise RuntimeError(
        "Не найден ни один whisper-бэкенд. Запусти scripts/detect_env.py — "
        "он подскажет какой ставить под твоё железо."
    )


def _pick_device() -> str:
    if _has_cuda():
        return "cuda"
    if _is_apple_silicon():
        return "mps"   # для PyTorch (whisperx). Для mlx-whisper это вообще не используется.
    return "cpu"


def _pick_compute_type(device: str) -> str:
    if device == "cuda":
        return os.environ.get("WHISPER_COMPUTE_TYPE", "float16")
    return os.environ.get("WHISPER_COMPUTE_TYPE", "int8")


# ─── Unified output ─────────────────────────────────────────────────────────


@dataclass
class Word:
    word: str
    start: float
    end: float
    speaker: Optional[str] = None


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: Optional[str] = None
    words: list[Word] = field(default_factory=list)


@dataclass
class Result:
    text: str
    language: str
    segments: list[Segment]
    backend: str
    model: str


# ─── Transcribe (universal) ─────────────────────────────────────────────────


_loaded_models: dict[tuple, object] = {}


def transcribe(
    audio_path: str | Path,
    language: Optional[str] = None,
    model_name: Optional[str] = None,
    word_timestamps: bool = True,
    backend: Optional[str] = None,
    verbose: bool = False,
) -> Result:
    """
    Один файл → транскрибат. Универсально для всех бэкендов.

    audio_path     — путь к аудио/видео. Поддерживается всё что ffmpeg умеет.
    language       — "ru", "en", "kk", ... ; None = auto-detect (медленнее)
    model_name     — имя модели. None = "large-v3-turbo" (рекомендованный дефолт)
    word_timestamps — пословные метки (для CapCut-стиля сабов)
    backend        — "mlx" | "faster" | "whisperx" | "cpp" | None (auto)
    verbose        — печать прогресса
    """
    audio_path = str(Path(audio_path).resolve())
    if not Path(audio_path).exists():
        raise FileNotFoundError(audio_path)

    backend = backend or _pick_backend()
    model_name = model_name or os.environ.get("WHISPER_MODEL", "large-v3-turbo")

    if verbose:
        print(f"[whisper-skill] backend={backend} model={model_name} device={_pick_device()}")

    if backend == "mlx":
        return _transcribe_mlx(audio_path, language, model_name, word_timestamps, verbose)
    if backend == "faster":
        return _transcribe_faster(audio_path, language, model_name, word_timestamps, verbose)
    if backend == "whisperx":
        return _transcribe_whisperx(audio_path, language, model_name, word_timestamps, verbose)
    if backend == "cpp":
        return _transcribe_cpp(audio_path, language, model_name, word_timestamps, verbose)
    raise ValueError(f"unknown backend: {backend}")


def _transcribe_mlx(audio, lang, model_name, word_ts, verbose):
    import mlx_whisper

    repo = (
        model_name if "/" in model_name
        else f"mlx-community/whisper-{model_name}"
    )
    res = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=repo,
        language=lang,
        word_timestamps=word_ts,
        verbose=verbose,
    )
    segs = [
        Segment(
            start=s["start"], end=s["end"], text=s["text"],
            words=[Word(w["word"], w["start"], w["end"]) for w in s.get("words", [])],
        )
        for s in res["segments"]
    ]
    return Result(
        text=res["text"],
        language=res.get("language", lang or "?"),
        segments=segs,
        backend="mlx",
        model=model_name,
    )


def _transcribe_faster(audio, lang, model_name, word_ts, verbose):
    from faster_whisper import WhisperModel

    device = _pick_device() if _has_cuda() else "cpu"
    compute_type = _pick_compute_type(device)
    key = (model_name, device, compute_type)
    model = _loaded_models.get(key)
    if model is None:
        if verbose:
            print(f"[whisper-skill] loading {model_name} on {device}/{compute_type}...")
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        _loaded_models[key] = model

    segments_iter, info = model.transcribe(
        audio,
        language=lang,
        word_timestamps=word_ts,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    segs: list[Segment] = []
    text_parts: list[str] = []
    for s in segments_iter:
        words = []
        for w in (s.words or []):
            words.append(Word(word=w.word, start=w.start, end=w.end))
        seg = Segment(start=s.start, end=s.end, text=s.text, words=words)
        segs.append(seg)
        text_parts.append(s.text)

    return Result(
        text="".join(text_parts).strip(),
        language=info.language,
        segments=segs,
        backend="faster",
        model=model_name,
    )


def _transcribe_whisperx(audio, lang, model_name, word_ts, verbose):
    import whisperx

    device = _pick_device()
    if device == "mps":  # whisperx не поддерживает MPS
        device = "cpu"
    compute_type = _pick_compute_type(device)

    key = ("whisperx", model_name, device, compute_type)
    model = _loaded_models.get(key)
    if model is None:
        model = whisperx.load_model(model_name, device, compute_type=compute_type)
        _loaded_models[key] = model

    audio_arr = whisperx.load_audio(audio)
    res = model.transcribe(audio_arr, batch_size=16, language=lang)
    if word_ts and res.get("segments"):
        try:
            align_model, metadata = whisperx.load_align_model(
                language_code=res["language"], device=device
            )
            res = whisperx.align(
                res["segments"], align_model, metadata, audio_arr, device,
                return_char_alignments=False,
            )
        except Exception as e:
            if verbose:
                print(f"[whisper-skill] alignment skipped: {e}")

    segs = []
    text_parts = []
    for s in res.get("segments", []):
        words = [Word(w["word"], w.get("start", 0.0), w.get("end", 0.0)) for w in s.get("words", [])]
        seg = Segment(start=s["start"], end=s["end"], text=s["text"], words=words)
        segs.append(seg)
        text_parts.append(s["text"])

    return Result(
        text="".join(text_parts).strip(),
        language=res.get("language", lang or "?"),
        segments=segs,
        backend="whisperx",
        model=model_name,
    )


def _transcribe_cpp(audio, lang, model_name, word_ts, verbose):
    from pywhispercpp.model import Model

    key = ("cpp", model_name)
    model = _loaded_models.get(key)
    if model is None:
        model = Model(model_name)
        _loaded_models[key] = model

    segments = model.transcribe(audio, language=lang or "auto")
    segs = [
        Segment(start=s.t0 / 100.0, end=s.t1 / 100.0, text=s.text)
        for s in segments
    ]
    return Result(
        text="\n".join(s.text for s in segs).strip(),
        language=lang or "?",
        segments=segs,
        backend="cpp",
        model=model_name,
    )


# ─── Output writers ─────────────────────────────────────────────────────────


def _ts(seconds: float, comma: bool = True) -> str:
    """1234.567 → 00:20:34,567"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    sep = "," if comma else "."
    return f"{h:02d}:{m:02d}:{int(s):02d}{sep}{int((s - int(s)) * 1000):03d}"


def save_srt(result: Result, out_path: str | Path) -> None:
    out_path = Path(out_path)
    with out_path.open("w", encoding="utf-8") as f:
        for i, seg in enumerate(result.segments, start=1):
            speaker_prefix = f"{seg.speaker}: " if seg.speaker else ""
            f.write(f"{i}\n")
            f.write(f"{_ts(seg.start)} --> {_ts(seg.end)}\n")
            f.write(f"{speaker_prefix}{seg.text.strip()}\n\n")


def save_vtt(result: Result, out_path: str | Path) -> None:
    out_path = Path(out_path)
    with out_path.open("w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for seg in result.segments:
            speaker_prefix = f"<v {seg.speaker}>" if seg.speaker else ""
            f.write(f"{_ts(seg.start, comma=False)} --> {_ts(seg.end, comma=False)}\n")
            f.write(f"{speaker_prefix}{seg.text.strip()}\n\n")


def save_txt(result: Result, out_path: str | Path) -> None:
    Path(out_path).write_text(result.text, encoding="utf-8")


def save_json(result: Result, out_path: str | Path) -> None:
    payload = {
        "text": result.text,
        "language": result.language,
        "backend": result.backend,
        "model": result.model,
        "segments": [
            {
                "start": s.start,
                "end": s.end,
                "text": s.text,
                "speaker": s.speaker,
                "words": [
                    {"word": w.word, "start": w.start, "end": w.end, "speaker": w.speaker}
                    for w in s.words
                ],
            }
            for s in result.segments
        ],
    }
    Path(out_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─── Audio extraction (yt-dlp) ──────────────────────────────────────────────


def download_audio_from_url(url: str, out_dir: str | Path = ".") -> Path:
    """Скачать аудио из TikTok / YouTube / Reels через yt-dlp.

    Возвращает путь к скачанному файлу (mp3 или m4a).
    """
    if subprocess.call(["which", "yt-dlp"], stdout=subprocess.DEVNULL) != 0:
        raise RuntimeError(
            "yt-dlp не найден. Установи: pip install yt-dlp"
        )

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_template = out_dir / "%(id)s.%(ext)s"

    cmd = [
        "yt-dlp",
        "-x", "--audio-format", "mp3",
        "--audio-quality", "0",
        "-o", str(out_template),
        url,
    ]
    subprocess.run(cmd, check=True)

    # Найти скачанный файл
    files = sorted(out_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise RuntimeError("Файл не скачался")
    return files[0]
