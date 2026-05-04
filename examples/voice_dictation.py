"""
Voice Dictation — Push-to-Talk диктовка вместо клавиатуры.

Заменяет Superwhisper / Wispr Flow / Aqua Voice — но локально и бесплатно.

Использование:
    python -m examples.voice_dictation                         # с дефолтным конфигом
    python -m examples.voice_dictation --config my-config.json # свой конфиг
    python -m examples.voice_dictation --setup                 # сгенерить шаблон конфига

Как работает:
    1. Скрипт висит в фоне, слушает глобальный хоткей.
    2. Жмёшь хоткей (по дефолту Ctrl+Shift+Space) → начинается запись.
    3. Говоришь, держа хоткей.
    4. Отпускаешь → Whisper транскрибирует → текст вставляется в активное поле через clipboard.

Зависимости (поставит wizard, или вручную):
    pip install sounddevice soundfile pynput pyperclip pystray Pillow numpy

Пермишены:
    macOS — нужно дать разрешение на Accessibility и Microphone:
        Системные настройки → Конфиденциальность → Универсальный доступ → добавить Terminal/iTerm
        Системные настройки → Конфиденциальность → Микрофон → добавить Terminal/iTerm
    Linux — на Wayland могут быть проблемы с глобальным хоткеем (X11 ок).
    Windows — обычно работает out-of-box.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


# ─── Config ─────────────────────────────────────────────────────────────────


DEFAULT_CONFIG = {
    "hotkey": "<ctrl>+<shift>+<space>",  # формат pynput Listener
    "mode": "ptt",                       # "ptt" (push-to-talk) или "toggle"
    "language": None,                    # null = auto. Лучше указать ("ru", "en")
    "model": "large-v3-turbo",
    "backend": None,                     # "openvino" | "faster" | "mlx" | "cpp" | null = auto
    "ov_device": "GPU",                  # OpenVINO: GPU | NPU | CPU | AUTO
    "sample_rate": 16000,
    "channels": 1,
    "max_duration_sec": 60,
    "auto_paste": True,                  # вставить через Cmd+V/Ctrl+V после копирования
    "play_sound": True,                  # бипы на старт/стоп
    "show_tray": True,                   # значок в трее (если установлен pystray)
    "show_cursor_indicator": True,       # мигающая красная точка у курсора во время записи
    "cursor_indicator_color": "#ef4444", # цвет точки (CSS hex)
    "log_file": None,                    # путь к файлу лога или null = stdout
    "trim_silence_ms": 200,              # обрезать тишину в начале/конце записи
    "min_duration_ms": 300,              # игнорировать слишком короткие записи (промахи кнопкой)
    # macOS-специфика: pystray/Tk известно жрут CPU в фоне на macOS
    # (NSRunLoop в non-main thread + Tk thread-safety). Этот флаг автоматически
    # отключает show_tray и show_cursor_indicator на macOS, оставляя CLI-вывод
    # как единственный feedback. Если хочешь tray на Mac на свой страх и риск —
    # поставь false (тогда show_tray/show_cursor_indicator будут уважаться).
    "mac_low_cpu_mode": True,
}


def default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "whisper-skill" / "voice_dictation.json"


def load_config(path: Optional[Path] = None) -> dict:
    path = path or default_config_path()
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    user_cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(user_cfg)
    return cfg


def write_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# ─── Setup helper ───────────────────────────────────────────────────────────


def setup_wizard():
    """Создать дефолтный конфиг и подсказать что делать дальше."""
    path = default_config_path()
    if path.exists():
        print(f"Конфиг уже есть: {path}")
        print("Хочешь перезаписать? [y/N] ", end="", flush=True)
        if input().strip().lower() != "y":
            return
    write_config(path, DEFAULT_CONFIG)
    print(f"\n✓ Создал конфиг: {path}")
    print(f"\nДефолтный хоткей: {DEFAULT_CONFIG['hotkey']}")
    print(f"Дефолтная модель: {DEFAULT_CONFIG['model']}")
    print(f"\nЗапусти диктовку:")
    print(f"  python -m examples.voice_dictation\n")


# ─── Audio recording ────────────────────────────────────────────────────────


class AudioRecorder:
    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        self._frames: list = []
        self._stream = None
        self._recording = False

    def start(self) -> None:
        import sounddevice as sd
        import numpy as np

        self._frames = []
        self._recording = True

        def callback(indata, frames, time_info, status):
            if status:
                logging.warning(f"audio status: {status}")
            self._frames.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="float32",
            callback=callback,
        )
        self._stream.start()

    def stop(self) -> Optional[str]:
        """Остановить запись и сохранить в WAV. Вернуть путь к файлу."""
        import numpy as np
        import soundfile as sf

        if not self._recording or not self._stream:
            return None
        self._recording = False
        self._stream.stop()
        self._stream.close()
        self._stream = None

        if not self._frames:
            return None
        audio = np.concatenate(self._frames, axis=0)

        tmp = tempfile.NamedTemporaryFile(
            suffix=".wav", delete=False, prefix="voice_dictation_"
        )
        sf.write(tmp.name, audio, self.sample_rate, subtype="PCM_16")
        return tmp.name

    @property
    def duration_sec(self) -> float:
        if not self._frames:
            return 0.0
        import numpy as np
        total_samples = sum(f.shape[0] for f in self._frames)
        return total_samples / self.sample_rate


# ─── Text insertion ─────────────────────────────────────────────────────────


def copy_to_clipboard(text: str) -> None:
    try:
        import pyperclip
        pyperclip.copy(text)
    except Exception as e:
        logging.error(f"clipboard copy failed: {e}")


def save_clipboard() -> Optional[str]:
    """Прочитать текущее текстовое содержимое буфера для последующего восстановления.

    Возвращает None если буфер пуст / содержит не-текст / pyperclip не доступен —
    в этих случаях мы не пытаемся восстанавливать (всё равно не сохранили).
    """
    try:
        import pyperclip
        cur = pyperclip.paste()
        return cur if cur else None
    except Exception:
        return None


def restore_clipboard(saved: Optional[str]) -> None:
    """Положить старое содержимое буфера обратно. No-op если saved is None."""
    if saved is None:
        return
    try:
        import pyperclip
        pyperclip.copy(saved)
    except Exception as e:
        logging.error(f"clipboard restore failed: {e}")


def _windows_paste() -> None:
    """Надёжная симуляция Ctrl+V на Windows через Win32 SendInput.

    Принудительно отпускает все возможные "залипшие" модификаторы
    (после хоткея типа Ctrl+Alt пользователь может ещё их удерживать),
    затем выполняет чистый Ctrl+V.
    """
    import ctypes
    import time as _t
    user32 = ctypes.windll.user32

    KEYEVENTF_KEYUP = 0x0002
    VK = {
        "ctrl": 0x11, "lctrl": 0xA2, "rctrl": 0xA3,
        "alt": 0x12, "lalt": 0xA4, "ralt": 0xA5,
        "shift": 0x10, "lshift": 0xA0, "rshift": 0xA1,
        "lwin": 0x5B, "rwin": 0x5C,
        "v": 0x56,
    }
    # 1) Release any held modifiers (idempotent — release of unpressed key is no-op)
    for name in ("lctrl", "rctrl", "ctrl", "lalt", "ralt", "alt",
                 "lshift", "rshift", "shift", "lwin", "rwin"):
        user32.keybd_event(VK[name], 0, KEYEVENTF_KEYUP, 0)
    _t.sleep(0.03)
    # 2) Clean Ctrl+V
    user32.keybd_event(VK["ctrl"], 0, 0, 0)
    user32.keybd_event(VK["v"], 0, 0, 0)
    _t.sleep(0.02)
    user32.keybd_event(VK["v"], 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK["ctrl"], 0, KEYEVENTF_KEYUP, 0)


def paste_from_clipboard() -> None:
    """Симулировать Cmd+V (Mac) или Ctrl+V (Linux/Win).

    На macOS приоритет — osascript: он работает через System Events, для которого
    разрешение Accessibility даётся один раз на Terminal/iTerm, и срабатывает
    надёжно. pynput-путь оставлен как fallback (на случай отсутствия osascript
    или сломанного System Events).

    На Linux/Windows используется pynput напрямую.
    """
    # macOS: предпочитаем osascript (надёжнее с защитой Accessibility)
    if platform.system() == "Darwin":
        try:
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to keystroke "v" using command down'],
                check=True, capture_output=True, timeout=2,
            )
            return
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(errors="replace")
            logging.warning(f"osascript paste failed ({stderr.strip()}), trying pynput fallback")
        except Exception as e:
            logging.warning(f"osascript paste failed: {e}, trying pynput fallback")

    # Fallback и основной путь для Linux/Windows
    try:
        if platform.system() == "Windows":
            _windows_paste()
        elif platform.system() == "Darwin":
            from pynput.keyboard import Controller, Key
            kb = Controller()
            with kb.pressed(Key.cmd):
                kb.press("v")
                kb.release("v")
        else:
            from pynput.keyboard import Controller, Key
            kb = Controller()
            with kb.pressed(Key.ctrl):
                kb.press("v")
                kb.release("v")
    except Exception as e:
        logging.error(
            f"paste simulation failed: {e}\n"
            f"Текст в clipboard — вставь вручную через Cmd+V/Ctrl+V."
        )


def play_beep(frequency: int = 800, duration_ms: int = 80) -> None:
    """Короткий синтезированный бип через sounddevice. Сохранён для
    обратной совместимости и для платформ без winsound (Linux/macOS).

    На Windows предпочитай play_beep_system — он громче, гарантированно
    слышен и не конфликтует с активным sd.InputStream (запись микрофона).
    """
    try:
        import numpy as np
        import sounddevice as sd
        sample_rate = 44100
        t = np.linspace(0, duration_ms / 1000, int(sample_rate * duration_ms / 1000), False)
        tone = 0.15 * np.sin(2 * np.pi * frequency * t)
        fade = int(sample_rate * 0.005)
        envelope = np.ones_like(tone)
        envelope[:fade] = np.linspace(0, 1, fade)
        envelope[-fade:] = np.linspace(1, 0, fade)
        tone = tone * envelope
        sd.play(tone.astype(np.float32), sample_rate, blocking=True)
    except Exception:
        pass


def _make_dual_beep_wav(
    f1: int, f2: int, dur_ms: int = 60, gap_ms: int = 40,
    sample_rate: int = 22050, vol: float = 0.01,
    tail_silence_ms: int = 80,
) -> bytes:
    """Сгенерировать in-memory WAV с двумя тонами через паузу.

    Возвращает байты PCM-WAV пригодные для winsound.PlaySound(SND_MEMORY).

    tail_silence_ms — хвост тишины после второго тона. Нужен потому, что
    Windows audio mixer иногда обрезает последние ~30-50ms короткого WAV
    (артефакт буферизации). Просто добавляем «зазор» из нулей.
    """
    import math as _m
    import struct

    def _tone_samples(freq: int, dur_ms: int) -> list:
        n = int(sample_rate * dur_ms / 1000)
        fade_n = max(1, int(sample_rate * 0.015))  # 15ms fade — длинный ramp убирает крякание BT-кодеков на attack
        out = []
        two_pi_f = 2.0 * _m.pi * freq
        for i in range(n):
            env = 1.0
            if i < fade_n:
                env = i / fade_n
            elif i > n - fade_n:
                env = max(0.0, (n - i) / fade_n)
            sample = int(32767 * vol * env * _m.sin(two_pi_f * (i / sample_rate)))
            out.append(struct.pack("<h", sample))
        return out

    silence_samples = [b"\x00\x00"] * int(sample_rate * gap_ms / 1000)
    tail_samples = [b"\x00\x00"] * int(sample_rate * tail_silence_ms / 1000)
    samples = (
        _tone_samples(f1, dur_ms) + silence_samples
        + _tone_samples(f2, dur_ms) + tail_samples
    )
    data = b"".join(samples)

    # 16-bit mono PCM WAV header
    fmt_chunk = struct.pack(
        "<4sIHHIIHH",
        b"fmt ", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16,
    )
    data_chunk = struct.pack("<4sI", b"data", len(data)) + data
    riff = struct.pack("<4sI4s", b"RIFF", 4 + len(fmt_chunk) + len(data_chunk), b"WAVE")
    return riff + fmt_chunk + data_chunk


def _make_single_beep_wav(
    freq: int = 600, dur_ms: int = 100,
    sample_rate: int = 22050, vol: float = 0.01,
    fade_ms: int = 10, tail_silence_ms: int = 80,
) -> bytes:
    """Однотоновый WAV. Мягкий, не сливается с речью — для стоп-сигнала."""
    import math as _m
    import struct

    n = int(sample_rate * dur_ms / 1000)
    fade_n = max(1, int(sample_rate * fade_ms / 1000))
    two_pi_f = 2.0 * _m.pi * freq
    tone = []
    for i in range(n):
        env = 1.0
        if i < fade_n:
            env = i / fade_n
        elif i > n - fade_n:
            env = max(0.0, (n - i) / fade_n)
        sample = int(32767 * vol * env * _m.sin(two_pi_f * (i / sample_rate)))
        tone.append(struct.pack("<h", sample))
    tail = [b"\x00\x00"] * int(sample_rate * tail_silence_ms / 1000)
    data = b"".join(tone + tail)

    fmt_chunk = struct.pack(
        "<4sIHHIIHH",
        b"fmt ", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16,
    )
    data_chunk = struct.pack("<4sI", b"data", len(data)) + data
    riff = struct.pack("<4sI4s", b"RIFF", 4 + len(fmt_chunk) + len(data_chunk), b"WAVE")
    return riff + fmt_chunk + data_chunk


# Pre-render the two beep WAVs once at import time — playing them later
# is then just a fire-and-forget winsound call.
_BEEP_WAV_START: Optional[bytes] = None
_BEEP_WAV_STOP: Optional[bytes] = None
try:
    _BEEP_WAV_START = _make_dual_beep_wav(700, 900)  # rising
    _BEEP_WAV_STOP = _make_single_beep_wav(600, 100)
except Exception:
    pass


def _play_wav_bytes(wav: bytes) -> None:
    """Проиграть готовые WAV-байты через основную звуковую карту."""
    if platform.system() == "Windows":
        try:
            import winsound
            winsound.PlaySound(wav, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
            return
        except Exception as e:
            logging.error(f"winsound play failed: {e}")


def play_start_beep() -> None:
    if _BEEP_WAV_START is not None:
        _play_wav_bytes(_BEEP_WAV_START)


def play_stop_beep() -> None:
    if _BEEP_WAV_STOP is not None:
        _play_wav_bytes(_BEEP_WAV_STOP)


def play_dual_beep(f1: int, f2: int, dur_ms: int = 60, gap_ms: int = 40) -> None:
    """Двутоновый бип на произвольных частотах. Синтезирует WAV каждый раз —
    использовать только для редких/нестандартных тонов; для обычных
    старт/стоп есть play_start_beep / play_stop_beep с предрендеренным WAV.
    """
    if platform.system() == "Windows":
        try:
            _play_wav_bytes(_make_dual_beep_wav(f1, f2, dur_ms, gap_ms))
            return
        except Exception as e:
            logging.error(f"winsound dual beep failed: {e}")
    try:
        play_beep(f1, dur_ms)
        if gap_ms > 0:
            time.sleep(gap_ms / 1000.0)
        play_beep(f2, dur_ms)
    except Exception:
        pass


# ─── Tray icon ──────────────────────────────────────────────────────────────


class TrayIcon:
    """Иконка в трее. Показывает текущее состояние цветом."""

    def __init__(self):
        self.icon = None
        self._ready = False

    def start(self):
        try:
            import pystray
            from PIL import Image, ImageDraw

            def _make_image(color: str):
                img = Image.new("RGB", (64, 64), "white")
                d = ImageDraw.Draw(img)
                d.ellipse((8, 8, 56, 56), fill=color)
                return img

            self._images = {
                "idle": _make_image("#666666"),
                "recording": _make_image("#e63946"),
                "transcribing": _make_image("#f4a261"),
            }
            self.icon = pystray.Icon(
                "voice_dictation",
                self._images["idle"],
                "Whisper Voice Dictation",
                menu=pystray.Menu(
                    pystray.MenuItem("Quit", lambda: self.icon.stop()),
                ),
            )
            threading.Thread(target=self.icon.run, daemon=True).start()
            self._ready = True
        except Exception as e:
            logging.info(f"Tray icon disabled: {e}")

    def set_state(self, state: str):
        if not self._ready or not self.icon:
            return
        img = self._images.get(state)
        if img:
            self.icon.icon = img


# ─── Hotkey-driven main loop ────────────────────────────────────────────────


@dataclass
class State:
    is_recording: bool = False
    is_transcribing: bool = False


def main_loop(cfg: dict):
    from pynput import keyboard

    # Lazy-import чтобы не падать на импортов при ошибке отсутствия пакетов
    try:
        from examples.common import transcribe
    except Exception as e:
        print(f"❌ Не могу загрузить examples.common: {e}", file=sys.stderr)
        print("Запусти из корня whisper-skill: cd whisper-skill && python -m examples.voice_dictation", file=sys.stderr)
        return 1

    state = State()
    state_lock = threading.Lock()
    recorder = AudioRecorder(cfg["sample_rate"], cfg["channels"])

    # macOS low-CPU mode: pystray в фоне и Tk у нас вызывают серьёзный
    # idle-CPU на маке (наблюдалось ~90% на M-чипе). Дефолтно отключаем оба
    # GUI-feedback'а на Mac. Юзер видит CLI-stdout (Recording/Transcribing/✓).
    is_mac_low_cpu = (
        platform.system() == "Darwin"
        and cfg.get("mac_low_cpu_mode", True)
    )
    if is_mac_low_cpu:
        if cfg.get("show_tray") or cfg.get("show_cursor_indicator"):
            logging.info(
                "macOS: tray и cursor_indicator отключены ради экономии CPU. "
                "Чтобы включить — поставь mac_low_cpu_mode: false в конфиге."
            )

    tray = TrayIcon()
    if cfg.get("show_tray") and not is_mac_low_cpu:
        tray.start()

    # Cursor indicator (small blinking dot near the mouse cursor while recording).
    # Optional — silently disables if Tk unavailable. На macOS всегда no-op
    # (см. scripts/cursor_indicator.py — Tk thread-safety issue).
    cursor_ind = None
    if cfg.get("show_cursor_indicator", True) and not is_mac_low_cpu:
        try:
            from scripts.cursor_indicator import CursorIndicator
            cursor_ind = CursorIndicator(color=cfg.get("cursor_indicator_color", "#ef4444"))
            cursor_ind.start()
        except Exception as e:
            logging.error(f"cursor indicator init failed: {e}")
            cursor_ind = None

    def start_recording():
        with state_lock:
            if state.is_recording or state.is_transcribing:
                return
            state.is_recording = True
        tray.set_state("recording")
        if cursor_ind:
            cursor_ind.show()
        if cfg.get("play_sound"):
            threading.Thread(target=play_start_beep, daemon=True).start()
        try:
            recorder.start()
            print("🎙  Recording... (release hotkey to transcribe)")
        except Exception as e:
            print(f"❌ Recording failed: {e}")
            state.is_recording = False
            tray.set_state("idle")
            if cursor_ind:
                cursor_ind.hide()

    def stop_and_transcribe():
        with state_lock:
            if not state.is_recording:
                return
            state.is_recording = False
        tray.set_state("transcribing")
        if cursor_ind:
            cursor_ind.hide()

        # Сначала закрываем микрофон, потом играем бип. Параллельный запуск
        # winsound во время stream.close() PortAudio даёт повторное звучание
        # (наблюдалось 2026-05-01: один вызов PlaySound → два слышимых тона).
        wav_path = recorder.stop()
        if cfg.get("play_sound"):
            threading.Thread(target=play_stop_beep, daemon=True).start()
        if not wav_path:
            tray.set_state("idle")
            return
        duration_ms = recorder.duration_sec * 1000

        if duration_ms < cfg.get("min_duration_ms", 300):
            print(f"⏭  Skipped (too short: {duration_ms:.0f}ms)")
            try: os.unlink(wav_path)
            except: pass
            tray.set_state("idle")
            return

        print(f"⏳ Transcribing {duration_ms:.0f}ms of audio...")
        state.is_transcribing = True

        def work():
            try:
                t0 = time.time()
                result = transcribe(
                    wav_path,
                    language=cfg.get("language"),
                    model_name=cfg.get("model"),
                    word_timestamps=False,
                    verbose=False,
                )
                text = result.text.strip()
                elapsed = time.time() - t0

                if not text:
                    print("⏭  Empty transcription")
                else:
                    print(f"✓ ({elapsed:.1f}s) → {text}")
                    saved_clipboard = save_clipboard()
                    copy_to_clipboard(text)
                    if cfg.get("auto_paste"):
                        time.sleep(0.25)  # дать целевому полю стать активным
                        paste_from_clipboard()
                        # Восстанавливаем содержимое буфера после того как
                        # целевое поле успело захватить вставку.
                        time.sleep(0.5)
                        restore_clipboard(saved_clipboard)
            except Exception as e:
                print(f"❌ Transcription failed: {e}")
            finally:
                state.is_transcribing = False
                tray.set_state("idle")
                try: os.unlink(wav_path)
                except: pass

        threading.Thread(target=work, daemon=True).start()

    def toggle():
        if state.is_recording:
            stop_and_transcribe()
        else:
            start_recording()

    hotkey_str = cfg["hotkey"]
    print(f"🎤 Whisper Voice Dictation активна")
    print(f"   Хоткей: {hotkey_str} ({cfg['mode']})")
    print(f"   Модель: {cfg['model']}")
    print(f"   Язык:   {cfg.get('language') or 'auto'}")
    print(f"\nНажми {hotkey_str} чтобы говорить. Ctrl+C чтобы выйти.\n")

    if cfg["mode"] == "ptt":
        # Push-to-talk: нажал → запись, отпустил → транскрибировать
        # У pynput GlobalHotKeys работает по нажатию. Для PTT отслеживаем сами через Listener.
        keys_needed = _parse_hotkey(hotkey_str)
        currently_pressed = set()

        def on_press(key):
            currently_pressed.add(_canonical_key(key))
            if keys_needed.issubset(currently_pressed):
                start_recording()

        def on_release(key):
            ck = _canonical_key(key)
            if ck in keys_needed and state.is_recording:
                stop_and_transcribe()
            currently_pressed.discard(ck)

        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            try:
                listener.join()
            except KeyboardInterrupt:
                pass
    else:
        # Toggle: нажал → старт, нажал ещё раз → стоп
        with keyboard.GlobalHotKeys({hotkey_str: toggle}) as listener:
            try:
                listener.join()
            except KeyboardInterrupt:
                pass

    print("\n👋 Bye")
    return 0


def _parse_hotkey(s: str) -> set:
    """'<ctrl>+<shift>+<space>' → set of canonical key names"""
    parts = [p.strip().lower() for p in s.replace(" ", "").split("+")]
    keys = set()
    for p in parts:
        if p.startswith("<") and p.endswith(">"):
            keys.add(p[1:-1])
        else:
            keys.add(p)
    return keys


def _canonical_key(key) -> str:
    """Канонизирует key из pynput в строку, совпадающую с _parse_hotkey."""
    from pynput.keyboard import Key, KeyCode
    if isinstance(key, Key):
        # Key.ctrl_l, Key.shift_r → "ctrl", "shift"
        name = key.name
        # Убрать суффиксы _l/_r
        for suffix in ("_l", "_r"):
            if name.endswith(suffix):
                name = name[:-2]
        return name
    if isinstance(key, KeyCode):
        if key.char:
            return key.char.lower()
        return str(key)
    return str(key).lower()


# ─── Entry point ────────────────────────────────────────────────────────────


def main():
    p = argparse.ArgumentParser(description="Push-to-talk голосовая диктовка через Whisper")
    p.add_argument("--config", default=None, help="Путь к JSON-конфигу")
    p.add_argument("--setup", action="store_true", help="Создать дефолтный конфиг и выйти")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.setup:
        setup_wizard()
        return 0

    # Fast mode for dictation: greedy decoding, no temperature fallback
    os.environ.setdefault("WHISPER_BEAM_SIZE", "1")
    os.environ.setdefault("WHISPER_BEST_OF", "1")
    os.environ.setdefault("WHISPER_CONDITION_ON_PREV", "0")

    # Apply backend selection from config (must happen before transcribe is imported)
    cfg_path_for_env = Path(args.config) if args.config else default_config_path()
    cfg_for_env = load_config(cfg_path_for_env)
    if cfg_for_env.get("backend"):
        os.environ["WHISPER_BACKEND"] = cfg_for_env["backend"]
    if cfg_for_env.get("ov_device"):
        os.environ["WHISPER_OV_DEVICE"] = cfg_for_env["ov_device"]

    cfg_path = Path(args.config) if args.config else default_config_path()
    cfg = load_config(cfg_path)

    # Проверка зависимостей
    missing = []
    for mod in ["sounddevice", "soundfile", "pynput", "pyperclip", "numpy"]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    # tkinter нужен только если включён cursor_indicator на не-Mac
    if cfg.get("show_cursor_indicator", True) and platform.system() != "Darwin":
        try:
            __import__("tkinter")
        except ImportError:
            print("⚠ tkinter не найден — cursor_indicator не будет работать.", file=sys.stderr)
            print("  Mac:    brew install python-tk@3.12", file=sys.stderr)
            print("  Linux:  sudo apt install python3-tk", file=sys.stderr)
            print("  Windows: переустанови Python и отметь 'tcl/tk and IDLE'", file=sys.stderr)
            print("  Или просто отключи в конфиге: show_cursor_indicator: false\n", file=sys.stderr)
    if missing:
        print(f"❌ Не установлены пакеты: {missing}", file=sys.stderr)
        print(f"\nПоставь:")
        print(f"  pip install {' '.join(missing)} pystray Pillow")
        return 1

    # macOS Accessibility check — без него глобальный hotkey не сработает,
    # но pynput даёт только WARNING в stderr и не падает. Пользователь
    # думает что всё сломано. Явно проверяем + открываем системные настройки.
    if platform.system() == "Darwin":
        if not _check_macos_accessibility():
            return 1

    return main_loop(cfg)


def _check_macos_accessibility() -> bool:
    """Проверить что процессу выдан Accessibility-permission на macOS.

    Использует CoreFoundation/ApplicationServices через ctypes. Если
    permission не выдан — печатает чёткую инструкцию и автоматически
    открывает соответствующий раздел System Settings. Возвращает False
    если permission не выдан (caller должен exit'нуть с этим кодом).
    """
    try:
        import ctypes
        from ctypes import c_void_p, c_bool

        # AXIsProcessTrustedWithOptions из ApplicationServices framework
        ApplicationServices = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        # Берём без options → не показываем системный prompt (он мигает и пропадает,
        # пользователю всё равно не понятно что делать)
        ApplicationServices.AXIsProcessTrusted.restype = c_bool
        trusted = ApplicationServices.AXIsProcessTrusted()
    except Exception as e:
        # Если не удалось проверить — не блокируем запуск (пусть pynput сам разберётся)
        logging.debug(f"AX trust check failed: {e}")
        return True

    if trusted:
        return True

    print("\n" + "─" * 60, file=sys.stderr)
    print("❌ macOS Accessibility permission не выдан", file=sys.stderr)
    print("─" * 60, file=sys.stderr)
    print(
        f"\nЭтому Python-бинарю нужен Accessibility доступ для глобального hotkey:\n"
        f"  {sys.executable}\n",
        file=sys.stderr,
    )
    print("Что делать:", file=sys.stderr)
    print("  1. Сейчас откроется System Settings → Privacy → Accessibility", file=sys.stderr)
    print("  2. Нажми + → Cmd+Shift+G → вставь путь выше → выбери python3", file=sys.stderr)
    print("  3. Включи галочку напротив добавленного python3", file=sys.stderr)
    print("  4. Запусти voice_dictation заново\n", file=sys.stderr)

    try:
        subprocess.Popen([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ])
    except Exception:
        pass

    return False


if __name__ == "__main__":
    sys.exit(main())
