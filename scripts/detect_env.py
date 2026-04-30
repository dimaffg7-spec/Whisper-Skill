#!/usr/bin/env python3
"""
Whisper Stack — автодетектор железа.

Запусти один раз перед установкой Whisper. Скрипт сам определит:
  - ОС (macOS / Linux / Windows / WSL)
  - CPU (Apple Silicon / x86_64 / ARM)
  - GPU (NVIDIA CUDA / AMD ROCm / Apple Metal / нет)
  - RAM / VRAM
  - Версии Python / PyTorch / CUDA / ffmpeg

И выдаст рекомендованный бэкенд + модель + готовые команды установки.

Запуск:
    python scripts/detect_env.py

Зависимости (только из stdlib):
    нет — работает на чистом Python 3.8+
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

# ─── ANSI colors ────────────────────────────────────────────────────────────

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    if not USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def green(t: str) -> str: return _c("32", t)
def yellow(t: str) -> str: return _c("33", t)
def red(t: str) -> str: return _c("31", t)
def blue(t: str) -> str: return _c("34", t)
def bold(t: str) -> str: return _c("1", t)
def dim(t: str) -> str: return _c("2", t)


# ─── Detection ──────────────────────────────────────────────────────────────


@dataclass
class Env:
    os_name: str = ""           # "macOS" / "Linux" / "Windows" / "WSL"
    os_version: str = ""
    arch: str = ""              # "arm64" / "x86_64"
    cpu_brand: str = ""         # "Apple M2 Pro" / "AMD Ryzen 9 5900X" / ...
    is_apple_silicon: bool = False
    ram_gb: float = 0.0
    has_nvidia_gpu: bool = False
    nvidia_gpu_name: str = ""
    nvidia_vram_gb: float = 0.0
    cuda_version: str = ""
    has_amd_gpu: bool = False
    amd_gpu_name: str = ""
    rocm_version: str = ""
    has_apple_gpu: bool = False
    metal_supported: bool = False
    python_version: str = ""
    pip_available: bool = False
    ffmpeg_installed: bool = False
    ffmpeg_version: str = ""


def detect_os(env: Env) -> None:
    sys_name = platform.system()
    if sys_name == "Darwin":
        env.os_name = "macOS"
        env.os_version = platform.mac_ver()[0]
    elif sys_name == "Linux":
        # Check if WSL
        try:
            with open("/proc/version") as f:
                if "microsoft" in f.read().lower():
                    env.os_name = "WSL"
                else:
                    env.os_name = "Linux"
        except Exception:
            env.os_name = "Linux"
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME"):
                        env.os_version = line.split("=", 1)[1].strip().strip('"')
                        break
        except Exception:
            env.os_version = platform.release()
    elif sys_name == "Windows":
        env.os_name = "Windows"
        env.os_version = platform.version()
    else:
        env.os_name = sys_name


def detect_cpu(env: Env) -> None:
    env.arch = platform.machine()

    if env.os_name == "macOS":
        try:
            out = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
            ).strip()
            env.cpu_brand = out
            env.is_apple_silicon = "Apple" in out and any(
                k in out for k in ("M1", "M2", "M3", "M4", "M5")
            )
        except Exception:
            env.cpu_brand = platform.processor() or "unknown"
    elif env.os_name in ("Linux", "WSL"):
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        env.cpu_brand = line.split(":", 1)[1].strip()
                        break
        except Exception:
            env.cpu_brand = platform.processor() or "unknown"
    else:  # Windows
        env.cpu_brand = platform.processor() or "unknown"


def detect_ram(env: Env) -> None:
    try:
        if env.os_name == "macOS":
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
            env.ram_gb = round(int(out) / 1024**3, 1)
        elif env.os_name in ("Linux", "WSL"):
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        env.ram_gb = round(kb / 1024**2, 1)
                        break
        elif env.os_name == "Windows":
            out = subprocess.check_output(
                ["wmic", "computersystem", "get", "totalphysicalmemory"], text=True
            )
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    env.ram_gb = round(int(line) / 1024**3, 1)
                    break
    except Exception:
        env.ram_gb = 0.0


def detect_nvidia(env: Env) -> None:
    if shutil.which("nvidia-smi") is None:
        return
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        if not out:
            return
        first_gpu = out.splitlines()[0]
        parts = [p.strip() for p in first_gpu.split(",")]
        if len(parts) >= 2:
            env.has_nvidia_gpu = True
            env.nvidia_gpu_name = parts[0]
            mem_str = parts[1]  # like "12288 MiB"
            try:
                mb = int(mem_str.split()[0])
                env.nvidia_vram_gb = round(mb / 1024, 1)
            except Exception:
                env.nvidia_vram_gb = 0
    except Exception:
        return

    # Detect CUDA via nvcc
    if shutil.which("nvcc"):
        try:
            out = subprocess.check_output(["nvcc", "--version"], text=True)
            for line in out.splitlines():
                if "release" in line.lower():
                    parts = line.split("release")
                    if len(parts) > 1:
                        env.cuda_version = parts[1].strip().split(",")[0].strip()
                    break
        except Exception:
            pass


def detect_amd(env: Env) -> None:
    if shutil.which("rocm-smi") is None and shutil.which("rocminfo") is None:
        return
    try:
        if shutil.which("rocminfo"):
            out = subprocess.check_output(["rocminfo"], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if "Marketing Name" in line:
                    name = line.split(":", 1)[1].strip()
                    if name and "CPU" not in name:
                        env.has_amd_gpu = True
                        env.amd_gpu_name = name
                        break
    except Exception:
        return

    # rocm version
    if shutil.which("hipconfig"):
        try:
            out = subprocess.check_output(["hipconfig", "--version"], text=True).strip()
            env.rocm_version = out
        except Exception:
            pass


def detect_apple_gpu(env: Env) -> None:
    if env.os_name != "macOS":
        return
    if env.is_apple_silicon:
        env.has_apple_gpu = True
        env.metal_supported = True


def detect_python(env: Env) -> None:
    env.python_version = platform.python_version()
    env.pip_available = (
        shutil.which("pip") is not None or shutil.which("pip3") is not None
    )


def detect_ffmpeg(env: Env) -> None:
    if shutil.which("ffmpeg") is None:
        return
    env.ffmpeg_installed = True
    try:
        out = subprocess.check_output(
            ["ffmpeg", "-version"], text=True, stderr=subprocess.STDOUT
        )
        first_line = out.splitlines()[0]
        # like "ffmpeg version 6.0 ..."
        env.ffmpeg_version = first_line.split("version", 1)[1].strip().split()[0] if "version" in first_line else first_line[:50]
    except Exception:
        env.ffmpeg_version = "unknown"


def detect_all() -> Env:
    env = Env()
    detect_os(env)
    detect_cpu(env)
    detect_ram(env)
    detect_nvidia(env)
    detect_amd(env)
    detect_apple_gpu(env)
    detect_python(env)
    detect_ffmpeg(env)
    return env


# ─── Recommendation engine ──────────────────────────────────────────────────


@dataclass
class Recommendation:
    backend: str = ""           # "mlx-whisper" / "faster-whisper" / "whisper-cpp" / "whisperx"
    backend_card: str = ""      # "backends/mlx-whisper.md"
    rationale: str = ""
    model: str = ""             # "large-v3" / "large-v3-turbo" / "small" / ...
    model_rationale: str = ""
    install_commands: list[str] = field(default_factory=list)
    test_command: str = ""
    warnings: list[str] = field(default_factory=list)


def recommend(env: Env) -> Recommendation:
    rec = Recommendation()

    # Step 1: pick backend
    if env.os_name == "macOS" and env.is_apple_silicon:
        rec.backend = "mlx-whisper"
        rec.backend_card = "backends/mlx-whisper.md"
        rec.rationale = (
            "Apple Silicon — нативная поддержка Metal через MLX. "
            "Это самый быстрый вариант на Mac (в 1.5-3x быстрее чем faster-whisper на том же Mac)."
        )
    elif env.os_name == "macOS" and not env.is_apple_silicon:
        rec.backend = "faster-whisper"
        rec.backend_card = "backends/faster-whisper.md"
        rec.rationale = (
            "Intel Mac без NVIDIA GPU — faster-whisper в режиме CPU. "
            "Рассматривать переход на Apple Silicon — там Whisper в 5-10x быстрее."
        )
    elif env.has_nvidia_gpu and env.nvidia_vram_gb >= 4:
        rec.backend = "faster-whisper"
        rec.backend_card = "backends/faster-whisper.md"
        rec.rationale = (
            f"NVIDIA GPU ({env.nvidia_gpu_name}, {env.nvidia_vram_gb} GB VRAM) — "
            "идеально для faster-whisper на CUDA. Realtime+ скорость на large-v3."
        )
    elif env.has_amd_gpu and env.os_name in ("Linux", "WSL"):
        rec.backend = "faster-whisper"
        rec.backend_card = "backends/faster-whisper.md"
        rec.rationale = (
            f"AMD GPU ({env.amd_gpu_name}) на Linux — faster-whisper через ROCm. "
            "Чуть сложнее установка чем на NVIDIA, но работает."
        )
        rec.warnings.append(
            "AMD ROCm support может быть нестабильным. Если упадёт на установке — переключайся на whisper.cpp."
        )
    else:
        # CPU only
        rec.backend = "whisper-cpp"
        rec.backend_card = "backends/whisper-cpp.md"
        rec.rationale = (
            "Нет дискретного GPU → whisper.cpp оптимизирован под CPU (AVX2/AVX512/Neon). "
            "Один бинарник, минимум зависимостей. Скорость ~0.3-1.0× от realtime на large-v3."
        )

    # Step 2: pick model
    # Logic:
    #   - apple silicon: large-v3-turbo by default (быстро + высокое качество)
    #   - NVIDIA >= 10GB VRAM: large-v3
    #   - NVIDIA 4-10GB: large-v3-turbo
    #   - CPU-only с RAM < 8GB: small
    #   - CPU-only с RAM 8-16GB: medium
    #   - CPU-only с RAM >= 16GB: large-v3-turbo

    if env.is_apple_silicon and env.ram_gb >= 16:
        rec.model = "large-v3-turbo"
        rec.model_rationale = (
            "На Apple Silicon с 16+ GB unified memory turbo даёт 8x скорость при минимальной потере. "
            "Для редких языков (казахский, узбекский, татарский) переключай на large-v3."
        )
    elif env.is_apple_silicon and env.ram_gb >= 8:
        rec.model = "small"
        rec.model_rationale = (
            "На M-чипе с 8GB unified memory — small баланс. "
            "Для прода-качества лучше апгрейд RAM или small/medium."
        )
    elif env.has_nvidia_gpu and env.nvidia_vram_gb >= 10:
        rec.model = "large-v3"
        rec.model_rationale = (
            f"VRAM {env.nvidia_vram_gb} GB ≥ 10 → large-v3 без проблем. "
            "Это эталонная модель — лучшее качество на всех языках."
        )
    elif env.has_nvidia_gpu and env.nvidia_vram_gb >= 4:
        rec.model = "large-v3-turbo"
        rec.model_rationale = (
            f"VRAM {env.nvidia_vram_gb} GB → large-v3-turbo (8x быстрее large-v3, ~2% потери качества). "
            "Если упадёт OOM — компилируй с compute_type=int8_float16."
        )
    elif not env.has_nvidia_gpu and env.ram_gb >= 16:
        rec.model = "large-v3-turbo"
        rec.model_rationale = (
            "RAM 16+ GB на CPU — turbo помещается, скорость ~0.5-1.0x realtime. "
            "Терпимо для коротких видео, медленно для подкастов."
        )
    elif not env.has_nvidia_gpu and env.ram_gb >= 8:
        rec.model = "medium"
        rec.model_rationale = (
            "RAM 8-16 GB на CPU — medium лучший компромисс. "
            "Качество значительно лучше small, скорость терпимая."
        )
    else:
        rec.model = "small"
        rec.model_rationale = (
            "Слабое железо — small — единственный вариант с приемлемой скоростью. "
            "Качество ~75% от large на английском, 60-70% на русском."
        )

    # Step 3: install commands
    if rec.backend == "mlx-whisper":
        rec.install_commands = [
            "# 1) Поставь Homebrew если ещё нет: https://brew.sh",
            "# 2) ffmpeg для извлечения аудио из видео:",
            "brew install ffmpeg",
            "# 3) Создай venv и поставь mlx-whisper:",
            "python3 -m venv .venv && source .venv/bin/activate",
            "pip install mlx-whisper",
        ]
        rec.test_command = (
            f'mlx_whisper --model mlx-community/whisper-{rec.model}-mlx '
            f'--language ru tests/sample.wav'
        )
    elif rec.backend == "faster-whisper" and env.has_nvidia_gpu:
        rec.install_commands = [
            "# 1) Убедись что есть CUDA Toolkit 12.x: nvidia-smi показывает Driver, nvcc показывает версию.",
            "# Если нет — поставь: https://developer.nvidia.com/cuda-downloads",
            "# 2) Создай venv:",
            "python3 -m venv .venv && source .venv/bin/activate  # Linux/Mac",
            "# (Windows: .venv\\Scripts\\activate)",
            "# 3) Установи faster-whisper:",
            "pip install faster-whisper",
            "# 4) ffmpeg:",
            "# Linux: sudo apt install ffmpeg  |  Windows: winget install ffmpeg  |  Mac: brew install ffmpeg",
        ]
        rec.test_command = (
            f"python -c \"from faster_whisper import WhisperModel; "
            f"m = WhisperModel('{rec.model}', device='cuda', compute_type='float16'); "
            f"print(list(m.transcribe('tests/sample.wav', language='ru')[0]))\""
        )
    elif rec.backend == "faster-whisper":
        rec.install_commands = [
            "python3 -m venv .venv && source .venv/bin/activate",
            "pip install faster-whisper",
            "# ffmpeg: Linux=apt | Windows=winget | Mac=brew",
        ]
        rec.test_command = (
            f"python -c \"from faster_whisper import WhisperModel; "
            f"m = WhisperModel('{rec.model}', device='cpu', compute_type='int8'); "
            f"print(list(m.transcribe('tests/sample.wav', language='ru')[0]))\""
        )
    elif rec.backend == "whisper-cpp":
        if env.os_name == "macOS":
            rec.install_commands = [
                "brew install whisper-cpp",
                f"# Скачай модель large-v3-turbo (~1.5 GB):",
                "mkdir -p models && cd models",
                f"curl -LO https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{rec.model}.bin",
                "cd ..",
            ]
        elif env.os_name == "Linux":
            rec.install_commands = [
                "sudo apt update && sudo apt install -y build-essential cmake ffmpeg",
                "git clone https://github.com/ggerganov/whisper.cpp.git",
                "cd whisper.cpp && cmake -B build && cmake --build build --config Release",
                f"bash ./models/download-ggml-model.sh {rec.model}",
            ]
        else:  # Windows
            rec.install_commands = [
                "# Через WSL2:",
                "wsl --install Ubuntu-22.04   # если ещё не стоит",
                "# Затем внутри WSL команды как для Linux выше",
                "# ИЛИ нативно через chocolatey:",
                "choco install whisper-cpp",
            ]
        rec.test_command = (
            f"./build/bin/whisper-cli -m models/ggml-{rec.model}.bin -l ru -f tests/sample.wav"
        )

    # Step 4: warnings
    if env.python_version:
        try:
            major, minor = map(int, env.python_version.split(".")[:2])
            if (major, minor) < (3, 9):
                rec.warnings.append(
                    f"Python {env.python_version} устарел. Whisper требует Python 3.9+. Поставь свежий."
                )
        except Exception:
            pass
    if not env.ffmpeg_installed:
        rec.warnings.append(
            "ffmpeg не найден. Он нужен для извлечения аудио из видео-файлов. "
            "Установка включена в команды ниже."
        )
    if env.ram_gb < 4 and not env.has_nvidia_gpu:
        rec.warnings.append(
            f"RAM {env.ram_gb} GB маловато даже для small. "
            "Рассмотри облачный путь (но это уже не локально)."
        )

    return rec


# ─── Reporting ──────────────────────────────────────────────────────────────


def section(title: str):
    print(f"\n{bold(blue('═══ ' + title + ' ═══'))}")


def row(label: str, value: str, ok: Optional[bool] = None):
    if ok is True:
        marker = green("✓")
    elif ok is False:
        marker = red("✗")
    else:
        marker = " "
    print(f"  {marker} {bold(label)}: {value}")


def report(env: Env, rec: Recommendation) -> None:
    print(bold("\n🎤 Whisper Stack — environment detector\n"))

    section("Hardware")
    row("ОС", f"{env.os_name} {env.os_version}", True)
    row("Архитектура", env.arch)
    row("CPU", env.cpu_brand or "unknown")
    row("RAM", f"{env.ram_gb} GB" if env.ram_gb else "?")

    if env.has_nvidia_gpu:
        row(
            "GPU (NVIDIA)",
            f"{env.nvidia_gpu_name}, {env.nvidia_vram_gb} GB VRAM",
            ok=True,
        )
        if env.cuda_version:
            row("CUDA", env.cuda_version, ok=True)
        else:
            row("CUDA Toolkit", yellow("не найден (будет установлен через PyTorch)"), ok=None)
    elif env.has_amd_gpu:
        row("GPU (AMD)", env.amd_gpu_name, ok=True)
        if env.rocm_version:
            row("ROCm", env.rocm_version, ok=True)
    elif env.has_apple_gpu:
        row("GPU (Apple)", "Metal (Apple Silicon)", ok=True)
    else:
        row("GPU", "not found — будет работать на CPU", ok=False)

    section("Software")
    py_ok = False
    if env.python_version:
        try:
            major, minor = map(int, env.python_version.split(".")[:2])
            py_ok = (major, minor) >= (3, 9)
        except Exception:
            py_ok = False
    row("Python", env.python_version or "?", ok=py_ok)
    row("pip", "доступен" if env.pip_available else "не найден", ok=env.pip_available)
    row(
        "ffmpeg",
        env.ffmpeg_version if env.ffmpeg_installed else "не установлен",
        ok=env.ffmpeg_installed,
    )

    section("Рекомендация")
    print(f"  Бэкенд:   {bold(green(rec.backend))}")
    print(f"  Модель:   {bold(green(rec.model))}")
    print(f"  Карточка: {dim(rec.backend_card)}")
    print()
    print(f"  {bold('Почему этот бэкенд:')}")
    print(f"    {rec.rationale}")
    print()
    print(f"  {bold('Почему эта модель:')}")
    print(f"    {rec.model_rationale}")

    if rec.warnings:
        section("⚠️  Замечания")
        for w in rec.warnings:
            print(f"  {yellow('⚠')}  {w}")

    section("Команды установки")
    for cmd in rec.install_commands:
        if cmd.startswith("#"):
            print(f"  {dim(cmd)}")
        else:
            print(f"  {green('$')} {cmd}")

    section("Тест после установки")
    print(f"  {green('$')} {rec.test_command}")

    section("Дальше")
    print(f"  1. Открой карточку бэкенда: {dim(rec.backend_card)}")
    print(f"  2. Прогони установку из секции выше")
    print(f"  3. Запусти готовый пример: {dim('python -m examples.transcribe_one input.mp3')}")
    print(f"  4. Если что-то падает — открой {dim('docs/known-issues.md')}")
    print()


def main() -> int:
    env = detect_all()
    rec = recommend(env)
    report(env, rec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
