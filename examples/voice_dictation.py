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
import re
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
    "auto_paste": True,                  # вставить через Cmd+V/Ctrl+V после копирования
    "play_sound": True,                  # бипы на старт/стоп
    "show_tray": True,                   # значок в трее (если установлен pystray)
    "show_cursor_indicator": True,       # мигающая красная точка у курсора во время записи
    "cursor_indicator_color": "#ef4444", # цвет точки (CSS hex)
    "log_file": None,                    # путь к файлу лога или null = stdout
    "trim_silence_ms": 200,              # обрезать тишину в начале/конце записи
    "min_duration_ms": 300,              # игнорировать слишком короткие записи (промахи кнопкой)
    # Словарь специфичных слов/имён для модели. НЕ user dictionary как в Dragon —
    # это whisper initial_prompt: модель видит контекст «эти слова ожидаемы» и
    # с большей вероятностью распознаёт их правильно (особенно имена, бренды,
    # англицизмы при русской дорожке). Лимит ~244 токенов суммарно.
    "vocabulary": [],
    # Post-process find/replace для случаев когда vocabulary-bias не сработал
    # (whisper стабильно выдаёт неправильный вариант). Применяется к итоговому
    # тексту до auto_paste. Match: case-insensitive, word-boundary (\b...\b) —
    # т.е. "cancel" не зацепит "cancellation". Output: ровно как в значении
    # (без сохранения case оригинала — поведение предсказуемо).
    "replacements": {},
    # То же, что replacements, но ключ — это RAW regex (НЕ экранируется).
    # Для случаев когда whisper коверкает слово множеством способов и literal-
    # замена не масштабируется (напр. бренд «YouGile» → VGL/ViewGile/UGL/UGile).
    # Один паттерн ловит всё семейство. Match case-insensitive, замена literal
    # (через lambda, без backref'ов). Битый regex логируется и пропускается.
    # ⚠️ Держи паттерн заякоренным (\b...\b), иначе зацепишь живой текст.
    "replacements_regex": {},
    # Whisper-галлюцинации: на тишине/хвостах записи модель дописывает «титры»
    # из ютуб-обучающих данных («Субтитры сделал DimaTorzok», «Редактор
    # субтитров…»). Эти regex-паттерны (case-insensitive) вырезаются из
    # итогового текста до auto_paste. Битый паттерн пропускается — не роняет
    # диктовку. Держи паттерны узкими: только то, что ты НИКОГДА не диктуешь сам,
    # иначе срежешь живой текст (напр. «Спасибо за просмотр» — рискованно).
    "hallucination_patterns": [],
    # macOS-специфика: pystray/Tk известно жрут CPU в фоне на macOS
    # (NSRunLoop в non-main thread + Tk thread-safety). Этот флаг автоматически
    # отключает show_tray и show_cursor_indicator на macOS, оставляя CLI-вывод
    # как единственный feedback. Если хочешь tray на Mac на свой страх и риск —
    # поставь false (тогда show_tray/show_cursor_indicator будут уважаться).
    "mac_low_cpu_mode": True,
    # macOS: слушать PTT-хоткей опросом состояния клавиш (33×/с) вместо
    # CGEventTap. Tap система отключает под нагрузкой → терялся release,
    # диктовки пропадали. Опрос отключить нельзя — «залипание» невозможно
    # by design. false = старый событийный слушатель (pynput).
    "mac_poll_listener": True,
    # Whisper repetition-loops: модель иногда залипает и гонит один токен/фразу
    # десятки-сотни раз («Greek Greek Greek…»). Профилактика — ниже
    # (condition_on_previous_text), а это страховка-постобработка. Два порога:
    #  • collapse: повтор подряд >= этого числа — мягко схлопнуть в 1 вхождение
    #    (для лёгких повторов/заиканий «я-я-я думаю» → «я думаю», остальной
    #    текст реальный). 4 — выше естественной речи («да-да-да»=3).
    "repetition_loop_threshold": 4,
    #  • discard: если непрерывный повтор занял >= этого числа СЛОВ = пато-петля
    #    whisper = ВСЯ диктовка галлюцинация (Дмитрий этих слов не говорил) → не
    #    вставлять НИЧЕГО. Метрика в СЛОВАХ (а не в «повторах»), чтобы одинаково
    #    ловить и однословные («Greek ×180»=180 слов), и фразовые («спасибо за
    #    внимание ×5»=15 слов) петли. 10 ловит любые реальные петли, но щадит
    #    живые повторы. 0 = выключить выбрасывание (только collapse).
    "repetition_loop_discard_threshold": 10,
    # False гасит repetition-loops на корню (модель не кондишенится на свой
    # повтор). Для коротких диктовок контекст от прошлого сегмента не нужен.
    "condition_on_previous_text": False,
}


def _collapse_repetition_loops(text: str, threshold: int = 4):
    """Схлопывает whisper repetition-loops И сообщает масштаб петли.

    Возвращает (cleaned_text, max_run_words):
      • max_run_words — макс. длина В СЛОВАХ непрерывного повтора n-граммы
        (n≤4) в ИСХОДНОМ тексте (= reps*n). Индикатор пато-петли: вызывающий
        по нему решает, отбросить ли всю диктовку. В словах (а не в повторах),
        чтобы однословные и фразовые петли мерились одной линейкой.
      • cleaned_text — n-граммы, повторённые >= threshold раз подряд, ужаты
        до одного вхождения. Трогаем только соседние повторы.
    Кейс: «… Greek ×180» → max_run_words=180, текст «… Greek»."""
    try:
        threshold = int(threshold)
    except (TypeError, ValueError):
        threshold = 4
    tokens = text.split()
    if len(tokens) <= 1:
        return text, 1

    # 1) измеряем макс. длину соседнего повтора В СЛОВАХ (reps*n), n=1..4,
    #    ДО схлопывания
    max_run = 1
    for n in range(1, 5):
        i, L = 0, len(tokens)
        while i + n <= L:
            gram = [t.lower() for t in tokens[i:i + n]]
            reps, j = 1, i + n
            while j + n <= L and [t.lower() for t in tokens[j:j + n]] == gram:
                reps += 1
                j += n
            if reps >= 2:
                max_run = max(max_run, reps * n)
            i = j if reps > 1 else i + 1

    # 2) мягко схлопываем (для лёгких повторов; пато-петлю вызывающий отбросит)
    if threshold >= 2 and len(tokens) > threshold:
        for n in range(4, 0, -1):
            out: list[str] = []
            i, L = 0, len(tokens)
            while i < L:
                gram = tokens[i:i + n]
                if len(gram) < n:
                    out.extend(tokens[i:])
                    break
                gram_lower = [t.lower() for t in gram]
                reps, j = 1, i + n
                while j + n <= L and [t.lower() for t in tokens[j:j + n]] == gram_lower:
                    reps += 1
                    j += n
                if reps >= threshold:
                    out.extend(gram)   # оставляем одно вхождение n-граммы
                    i = j
                else:
                    out.append(tokens[i])
                    i += 1
            tokens = out

    return " ".join(tokens), max_run


def default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "whisper-skill" / "voice_dictation.json"


def load_config(path: Optional[Path] = None) -> dict:
    path = path or default_config_path()
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    user_cfg = json.loads(path.read_text(encoding="utf-8"))
    # Defense-in-depth: конфиг может содержать кастомные пути / hotkey-настройки,
    # чужим юзерам это видеть незачем. Идемпотентно — `chmod 600` каждый старт.
    try: os.chmod(path, 0o600)
    except OSError: pass
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(user_cfg)
    return cfg


def write_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    try: os.chmod(path, 0o600)
    except OSError: pass


# ─── Available models (OpenVINO cache) ──────────────────────────────────────


_MODEL_QUALITY_ORDER = [
    # Best → worst. Turbo (distilled-decoder) — лучший компромисс скорость/качество,
    # но чуть слабее int8/int8-sym на сложных кейсах (шум, акцент).
    # int4 теряет в качестве заметнее всех.
    "large-v3",
    "large-v3-int8",
    "large-v3-int8-sym",
    "large-v3-turbo",
    "large-v3-int4",
    "medium", "small", "base", "tiny",
]


def list_available_ov_models() -> list:
    """Папки `whisper-*-ov` в ~/.cache/openvino-whisper/ — те, что openvino-
    backend умеет грузить (см. _transcribe_openvino в common.py).
    Возвращает model-name'ы в порядке убывания качества (best первый);
    модели вне whitelist'а уходят в конец alphabetically.
    """
    base = Path.home() / ".cache" / "openvino-whisper"
    if not base.exists():
        return []
    found = set()
    for p in base.iterdir():
        if p.is_dir() and p.name.startswith("whisper-") and p.name.endswith("-ov"):
            found.add(p.name[len("whisper-"):-len("-ov")])
    ordered = [m for m in _MODEL_QUALITY_ORDER if m in found]
    extras = sorted(found - set(ordered))
    return ordered + extras


# ─── Single-instance lock ───────────────────────────────────────────────────


_single_instance_handle = None  # держим ссылку чтобы lock не сборщик мусора убил


def acquire_single_instance_lock(timeout_seconds: float = 2.0) -> bool:
    """Захватить named mutex (Windows) / file lock (Unix). True — захватили.
    False — другая копия уже работает.

    timeout_seconds покрывает self-restart: старая копия только что вызвала
    os._exit, новая стартует, ОС ещё не успела освободить lock — повторяем.
    """
    global _single_instance_handle
    deadline = time.monotonic() + timeout_seconds

    if platform.system() == "Windows":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ERROR_ALREADY_EXISTS = 183
        while True:
            handle = kernel32.CreateMutexW(None, True, "WhisperVoiceDictation_SingleInstance")
            if kernel32.GetLastError() != ERROR_ALREADY_EXISTS:
                _single_instance_handle = handle
                return True
            kernel32.CloseHandle(handle)
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.15)
    else:
        try:
            import fcntl
        except ImportError:
            return True  # нет fcntl — пропускаем lock (Win-вариант покрыт выше)
        lock_path = Path.home() / ".config" / "whisper-skill" / "voice_dictation.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            fh = open(lock_path, "a+")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fh.seek(0); fh.truncate()
                fh.write(str(os.getpid())); fh.flush()
                _single_instance_handle = fh
                return True
            except (BlockingIOError, OSError):
                fh.close()
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.15)


def restart_self() -> None:
    """Завершить текущий процесс и запустить новую копию через VBS launcher.
    Используется при смене модели через tray-меню."""
    repo_root = Path(__file__).resolve().parents[1]
    if platform.system() == "Windows":
        vbs = repo_root / "launcher" / "voice_dictation_silent.vbs"
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        if vbs.exists():
            subprocess.Popen(
                ["wscript.exe", str(vbs)],
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                [sys.executable, "-m", "examples.voice_dictation"],
                cwd=str(repo_root),
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
    else:
        subprocess.Popen(
            [sys.executable, "-m", "examples.voice_dictation"],
            cwd=str(repo_root),
            start_new_session=True,
            close_fds=True,
        )
    # os._exit — мгновенный hard exit без atexit/finally; ОС освободит mutex/lock,
    # новая копия подхватит после retry в acquire_single_instance_lock.
    os._exit(0)


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


def _windows_set_clipboard_text(text: str) -> bool:
    """Надёжная запись CF_UNICODETEXT через Win32. Возвращает True при успехе.

    pyperclip на Windows периодически не выдерживает rapid-fire вызовы и
    может отвалиться без исключения. Эта реализация делает retry на
    OpenClipboard (буфер мог быть занят другим процессом) и явно владеет
    памятью до момента, когда система её забирает.
    """
    import ctypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.EmptyClipboard.restype = ctypes.c_int
    user32.CloseClipboard.restype = ctypes.c_int
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    GMEM_MOVEABLE = 0x0002
    CF_UNICODETEXT = 13

    data = text.encode("utf-16-le") + b"\x00\x00"
    h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h_mem:
        return False
    p_mem = kernel32.GlobalLock(h_mem)
    if not p_mem:
        kernel32.GlobalFree(h_mem)
        return False
    ctypes.memmove(p_mem, data, len(data))
    kernel32.GlobalUnlock(h_mem)

    opened = False
    for _ in range(10):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.01)
    if not opened:
        kernel32.GlobalFree(h_mem)
        return False

    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
            kernel32.GlobalFree(h_mem)
            return False
        return True
    finally:
        user32.CloseClipboard()


def _windows_get_clipboard_text() -> Optional[str]:
    """Чтение CF_UNICODETEXT через Win32. None если буфер пуст / не текст /
    OpenClipboard не удался."""
    import ctypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.CloseClipboard.restype = ctypes.c_int
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int

    CF_UNICODETEXT = 13

    opened = False
    for _ in range(10):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.01)
    if not opened:
        return None

    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def _get_clipboard_text() -> Optional[str]:
    if platform.system() == "Windows":
        return _windows_get_clipboard_text()
    try:
        import pyperclip
        return pyperclip.paste() or None
    except Exception:
        return None


def copy_to_clipboard(text: str) -> None:
    if platform.system() == "Windows":
        if _windows_set_clipboard_text(text):
            return
        logging.warning("win32 clipboard set failed, falling back to pyperclip")
    try:
        import pyperclip
        pyperclip.copy(text)
    except Exception as e:
        logging.error(f"clipboard copy failed: {e}")


def save_clipboard() -> Optional[str]:
    """Снимок текстового содержимого буфера для последующего восстановления.

    None если буфер пуст / содержит не-текст (картинку, файл) / не удалось
    прочитать. В этих случаях restore тоже no-op — мы не пытаемся
    восстановить то, что не сохранили.
    """
    text = _get_clipboard_text()
    if text is None:
        logging.info("clipboard save: empty or non-text, restore will be skipped")
    return text


def restore_clipboard(saved: Optional[str]) -> None:
    """Положить сохранённое содержимое обратно с verify-and-retry.

    После записи читаем буфер и сравниваем; если не совпало — повторяем
    до 3 попыток. Защита от того, что в момент нашей записи буфер был
    занят другим процессом (Win+V history listener, clipboard manager).
    """
    if not saved:
        return
    for attempt in range(3):
        copy_to_clipboard(saved)
        current = _get_clipboard_text()
        if current == saved:
            return
        time.sleep(0.05)
    logging.warning(
        f"clipboard restore not verified after 3 attempts "
        f"(expected len={len(saved)}, got len={len(current) if current else 0})"
    )


def restore_clipboard_deferred(saved: Optional[str], delay_sec: float = 1.0) -> None:
    """Восстановить буфер с задержкой в отдельном потоке.

    SendInput возвращается синхронно, но таргет-приложение обрабатывает
    Ctrl+V асинхронно: сначала помещает WM_PASTE в очередь, обработчик
    читает буфер в свою очередь. На медленных таргетах (Chrome,
    Electron, web-приложения вроде ChatGPT/Claude) между нашим SendInput
    и реальным чтением буфера может пройти 200–800 мс. Если восстановить
    буфер слишком быстро — приложение прочитает уже восстановленный
    оригинал, а не диктованный текст. delay_sec=1.0 покрывает медленные
    таргеты с запасом.
    """
    if not saved:
        return

    def _do():
        time.sleep(delay_sec)
        restore_clipboard(saved)

    threading.Thread(target=_do, daemon=True).start()


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

    На macOS Sequoia (26.x) osascript часто молча проглатывает keystroke
    без ошибки (Python нет в Automation → System Events, attribution от
    родителя VSCode не всегда передаётся). Поэтому на маке предпочитаем
    pynput через Quartz CGEventPost — он управляется через Accessibility
    которая выдаётся напрямую python-бинарю.

    На Linux/Windows используется pynput напрямую.
    """
    # macOS: prefer Quartz CGEventPost (низкий уровень, обходит pynput-цепочку).
    # pynput-путь press Cmd → press V → release V → release Cmd на Sequoia
    # иногда не реализуется как «настоящее» Cmd+V для target-app (проверено
    # 2026-05-23 на M5 macOS 26.5). Quartz одним event'ом с command-flag — ок.
    if platform.system() == "Darwin":
        try:
            from Quartz import (
                CGEventCreateKeyboardEvent, CGEventSetFlags,
                CGEventPost, kCGEventFlagMaskCommand, kCGSessionEventTap,
            )
            # kCGSessionEventTap (а не HID) — событие НЕ видно taps на HID-уровне
            # (= потенциальные сторонние кейлоггеры/мониторы). Целевые приложения
            # всё равно получают Cmd+V нормально (они taps на session уровне).
            V_KEYCODE = 9  # virtual key 'v' (US layout)
            down = CGEventCreateKeyboardEvent(None, V_KEYCODE, True)
            CGEventSetFlags(down, kCGEventFlagMaskCommand)
            CGEventPost(kCGSessionEventTap, down)
            up = CGEventCreateKeyboardEvent(None, V_KEYCODE, False)
            CGEventSetFlags(up, kCGEventFlagMaskCommand)
            CGEventPost(kCGSessionEventTap, up)
            return
        except Exception as e:
            logging.warning(f"Quartz paste failed: {e}, trying AppleScript fallback")
        try:
            ascript = (
                'tell application "System Events"\n'
                '    set frontApp to first application process whose frontmost is true\n'
                '    set appName to name of frontApp\n'
                '    tell process appName\n'
                '        keystroke "v" using command down\n'
                '    end tell\n'
                'end tell'
            )
            subprocess.run(
                ["osascript", "-e", ascript],
                check=True, capture_output=True, timeout=2,
            )
            return
        except Exception as e:
            logging.error(
                f"AppleScript paste also failed: {e}; "
                f"текст в clipboard, вставь вручную через Cmd+V."
            )
        return

    try:
        if platform.system() == "Windows":
            _windows_paste()
        else:
            from pynput.keyboard import Controller, Key
            kb = Controller()
            with kb.pressed(Key.ctrl):
                kb.press("v")
                kb.release("v")
    except Exception as e:
        logging.error(
            f"paste simulation failed: {e}\n"
            f"Текст в clipboard — вставь вручную через Ctrl+V."
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


_MAC_SOUND_START = "/System/Library/Sounds/Tink.aiff"
_MAC_SOUND_STOP = "/System/Library/Sounds/Pop.aiff"
_MAC_SOUND_VOLUME = "0.05"  # 0.0-1.0, очень тихо


def _mac_afplay(path: str) -> None:
    try:
        proc = subprocess.Popen(
            ["afplay", "-v", _MAC_SOUND_VOLUME, path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
        )
        # Reaper-thread ждёт ~100ms пока afplay отыграет и заберёт exit-код.
        # Иначе под KeepAlive=true (launchd, недели работы) копились бы зомби —
        # 2 на каждую диктовку, к концу месяца сотни. proc.wait() в фоне
        # позволяет ОС освободить запись в process table сразу после выхода.
        threading.Thread(target=proc.wait, daemon=True).start()
    except Exception:
        pass


def play_start_beep() -> None:
    if platform.system() == "Darwin":
        _mac_afplay(_MAC_SOUND_START)
        return
    if _BEEP_WAV_START is not None:
        _play_wav_bytes(_BEEP_WAV_START)


def play_stop_beep() -> None:
    if platform.system() == "Darwin":
        _mac_afplay(_MAC_SOUND_STOP)
        return
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

    def start(self, current_model: Optional[str] = None,
              available_models: Optional[list] = None,
              on_select_model=None):
        """current_model / available_models / on_select_model — для подменю
        "Модель". on_select_model(name) вызывается при клике; обычно делает
        write_config + restart_self()."""
        try:
            import pystray
            from PIL import Image, ImageDraw

            self._images = self._build_images()

            menu_entries = []
            if available_models:
                def _make_handler(name):
                    return lambda icon, item: on_select_model and on_select_model(name)

                def _make_check(name):
                    return lambda item: current_model == name

                model_items = [
                    pystray.MenuItem(
                        m, _make_handler(m),
                        checked=_make_check(m), radio=True,
                    )
                    for m in available_models
                ]
                menu_entries.append(
                    pystray.MenuItem("Model", pystray.Menu(*model_items))
                )

            menu_entries.append(pystray.MenuItem("Quit", lambda: self.icon.stop()))

            self.icon = pystray.Icon(
                "voice_dictation",
                self._images["idle"],
                "Whisper Voice Dictation",
                menu=pystray.Menu(*menu_entries),
            )
            threading.Thread(target=self.icon.run, daemon=True).start()
            self._ready = True
        except Exception as e:
            logging.warning(f"Tray icon disabled: {e}", exc_info=True)

    @staticmethod
    def _build_images():
        from PIL import Image, ImageDraw

        size = 64
        # Базовая иконка — assets/icon.png рядом с репо. Если её нет
        # (минимальная установка) — fallback на серый круг.
        repo_root = Path(__file__).resolve().parents[1]
        icon_path = repo_root / "assets" / "icon.png"
        if icon_path.exists():
            base = Image.open(icon_path).convert("RGBA").resize((size, size), Image.LANCZOS)
        else:
            base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            d = ImageDraw.Draw(base)
            d.ellipse((8, 8, 56, 56), fill="#666666")

        def _with_dot(color: Optional[str]):
            img = base.copy()
            if color is not None:
                d = ImageDraw.Draw(img)
                # Точка-индикатор поверх встроенной красной точки логотипа
                # (правый нижний угол) — повторяет позицию dot'а возле курсора.
                d.ellipse((size - 26, size - 26, size - 4, size - 4),
                          fill=color, outline="white", width=2)
            return img

        return {
            "idle": _with_dot(None),
            "recording": _with_dot("#e63946"),
            "transcribing": _with_dot("#f4a261"),
        }

    def set_state(self, state: str):
        if not self._ready or not self.icon:
            return
        img = self._images.get(state)
        if img:
            self.icon.icon = img


# ─── Hotkey-driven main loop ────────────────────────────────────────────────


def _warmup(transcribe_fn, cfg: dict, tray) -> None:
    """Прогрев модели в фоне — компилирует OV-граф / прогружает веса.

    Без warmup'а первый Ctrl+Alt тратит 5–30 сек на cold start (особенно
    на OpenVINO + iGPU при первом compile=True). Запись на короткий буфер
    тишины, результат игнорируем.
    """
    try:
        import wave
        # 0.5 сек тишины 16k mono int16 — минимум, который не отлетает по VAD
        sample_rate = 16000
        silence = b"\x00\x00" * (sample_rate // 2)
        # NamedTemporaryFile — unpredictable name + mode 0600 + user-private
        # /var/folders/.../T/. Раньше был /tmp/whisper_skill_warmup.wav —
        # предсказуемый путь, потенциальный symlink-attack vector на
        # многопользовательских системах (single-user mac — низкий риск,
        # но defense-in-depth).
        _tmpfd = tempfile.NamedTemporaryFile(
            prefix="whisper_warmup_", suffix=".wav", delete=False
        )
        _tmpfd.close()
        tmp = Path(_tmpfd.name)
        with wave.open(str(tmp), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(silence)

        t0 = time.time()
        try:
            transcribe_fn(
                str(tmp),
                language=cfg.get("language"),
                model_name=cfg.get("model"),
                backend=cfg.get("backend"),
                word_timestamps=False,
                verbose=False,
            )
            logging.info(f"warmup done in {time.time() - t0:.1f}s")
        finally:
            # Чистим warmup-файл независимо от исхода — иначе тишинный wav
            # копится в /var/folders/.../T/ при каждом запуске.
            try: tmp.unlink()
            except: pass
    except Exception as e:
        # Прогрев — best-effort. Если упал, обычная диктовка продолжит работать
        # как раньше: просто первый запрос будет холодным.
        logging.info(f"warmup failed (non-fatal): {e}")


@dataclass
class State:
    is_recording: bool = False
    is_transcribing: bool = False
    last_dictation_at: float = 0.0  # time.time() последней успешной вставки
    record_started_at: float = 0.0  # time.time() начала текущей записи (для watchdog)


# Maximum duration of a single PTT recording. After this, watchdog force-stops
# (releases the lost-release-event deadlock — pynput on macOS occasionally
# drops on_release events, leaving is_recording=True forever).
_MAX_RECORDING_SECONDS = 90.0
_WATCHDOG_TICK_SECONDS = 3.0
_TAP_REVIVAL_TICK_SECONDS = 0.5
# Если CGEventTapEnable(True) подряд N раз не оживляет tap (каждый следующий
# тик IsEnabled опять False) — reference stale, re-enable бесполезен. Тогда
# self-exit; launchd по KeepAlive=true поднимет новый процесс с новым tap'ом.
# 8 × 0.5s = 4 секунды агонии до авто-рестарта (было 20 = 10с).
# Снижено 2026-06-15 на основе 3 недель логов: из 32 серий, перешагнувших
# #5 revival, 29 (91%) всё равно доходили до zombie и рестартовали — ожидание
# полных 10с почти всегда бессмысленно. Порог 8 > типичного мерцания #1-5
# (оно лечится revival без рестарта), но вдвое+ сокращает мёртвую зону хоткея.
_TAP_STALE_RESTART_THRESHOLD = 8

# ── Release-поллер: физическое состояние клавиш (macOS) ─────────────────────
# pynput на macOS периодически теряет on_release (34 инцидента в dictation.log
# за июнь–июль 2026: запись висла 90с, watchdog выбрасывал всё надиктованное).
# Поллер раз в 0.2с сверяет is_recording с ФИЗИЧЕСКИМ состоянием клавиш через
# CGEventSourceFlagsState/KeyState — этот опрос не зависит от CGEventTap и
# работает даже когда tap мёртв. Запись идёт, а хоткей отпущен → это
# потерянный release → штатный stop_and_transcribe (текст сохраняется).
_RELEASE_POLL_TICK_SECONDS = 0.2
# Грейс от старта записи: HID-state обновляется раньше доставки событий, но
# зазор снимает любые старт-гонки. Записи короче min_duration_ms (800ms у
# текущего конфига) всё равно отбрасываются — потерь грейс не создаёт.
_RELEASE_POLL_GRACE_SECONDS = 0.3

_MAC_MODIFIER_FLAG_MASKS = {
    # kCGEventFlagMask* (Quartz CGEventTypes.h); ключи — канон _canonical_key
    "ctrl": 0x00040000,   # kCGEventFlagMaskControl
    "shift": 0x00020000,  # kCGEventFlagMaskShift
    "alt": 0x00080000,    # kCGEventFlagMaskAlternate
    "cmd": 0x00100000,    # kCGEventFlagMaskCommand
}
# kVK_* (Carbon Events.h) для не-модификаторов, которые могут быть в хоткее.
# Буквы/цифры не мапим (layout-зависимы) — с ними поллер честно молчит.
_MAC_KEYCODES = {
    "space": 49, "tab": 48, "esc": 53, "caps_lock": 57,
    "f13": 105, "f14": 107, "f15": 113, "f16": 106,
    "f17": 64, "f18": 79, "f19": 80, "f20": 90,
}


def _mac_hotkey_physically_down(keys: set) -> Optional[bool]:
    """Физически ли зажат весь хоткей прямо сейчас.

    False — хотя бы одна ПРОВЕРЯЕМАЯ клавиша отпущена (можно смело стопить).
    True — все клавиши известны и зажаты.
    None — достоверно проверить нельзя (не macOS, Quartz недоступен, или все
    зажаты, но в хоткее есть незнакомая клавиша) → вызывающий молчит,
    поведение деградирует к событийному (как до фикса)."""
    if platform.system() != "Darwin":
        return None
    try:
        from Quartz import (
            CGEventSourceFlagsState,
            CGEventSourceKeyState,
            kCGEventSourceStateHIDSystemState,
        )
    except Exception:
        return None
    try:
        flags = CGEventSourceFlagsState(kCGEventSourceStateHIDSystemState)
        unknown = False
        for k in keys:
            mask = _MAC_MODIFIER_FLAG_MASKS.get(k)
            if mask is not None:
                if not (flags & mask):
                    return False
                continue
            code = _MAC_KEYCODES.get(k)
            if code is None:
                unknown = True
                continue
            if not CGEventSourceKeyState(kCGEventSourceStateHIDSystemState, code):
                return False
        return None if unknown else True
    except Exception:
        return None


# ── Poll-слушатель (macOS): PTT вообще без event tap ────────────────────────
# Корневая причина всех «залипаний» — событийная архитектура: CGEventTap
# macOS отключает под нагрузкой (kCGEventTapDisabledByTimeout), события
# теряются, и никакие revival/watchdog это до конца не лечат — они лишь
# уменьшают урон. Poll-слушатель убирает сам класс проблемы: никакого
# перехвата, просто опрос «зажат ли хоткей» 33 раза в секунду через
# CGEventSourceFlagsState/KeyState + edge detection. Опрос нельзя
# «отключить», он не зависит от load и не теряет события — событий нет.
# Поведение 1:1 со старым PTT (старт при любом нажатии Ctrl, короткие
# записи режет min_duration_ms). Подход портируем (Windows:
# GetAsyncKeyState), но включаем только на Darwin — там и болело.
#
# Адаптивный тик: в простое опрашиваем редко (фоновая цена ~втрое ниже,
# замер 2026-07-17: 33×/с ≈ 1.4% одного ядра, 10×/с ≈ 0.5%), при зажатом
# хоткее — часто, чтобы отпускание ловилось мгновенно. Старт-лаг до 100мс
# безвреден: юзер начинает говорить после старт-бипа, а бип играет после
# фактического старта записи.
_POLL_LISTENER_TICK_SECONDS = 0.03        # хоткей зажат: отзывчивый стоп
_POLL_LISTENER_IDLE_TICK_SECONDS = 0.1    # простой: 10×/с хватает для старта


def _poll_ptt_loop(probe, on_down, on_up, tick: float = _POLL_LISTENER_TICK_SECONDS,
                   idle_tick: Optional[float] = None):
    """Цикл PTT-слушателя: probe() → True (хоткей зажат) / False / None
    (тик пропустить). Фронт вверх → on_down(), фронт вниз → on_up().
    Однопоточно (старт/стоп решает один поток — гонки исключены).
    Блокирует навсегда, как listener.join(); KeyboardInterrupt наружу.
    Ошибка тика логируется и не убивает слушатель.
    idle_tick — интервал, пока хоткей не зажат (None = как tick)."""
    prev_down = False
    error_streak = 0
    while True:
        time.sleep(tick if prev_down else (idle_tick or tick))
        try:
            down = probe()
            error_streak = 0
            if down is None:
                continue
            if down and not prev_down:
                on_down()
            elif prev_down and not down:
                on_up()
            prev_down = down
        except KeyboardInterrupt:
            raise
        except Exception as e:
            error_streak += 1
            logging.error(f"poll listener tick failed (#{error_streak}): {e!r}")
            # Перманентно сломанный опрос = глухой демон, висящий «живым» —
            # ровно тот класс тихих смертей, ради которого poll-слушатель и
            # писался. Не терпим: после серии подряд — self-exit, launchd
            # (KeepAlive) поднимет свежий процесс, а его probe-проверка на
            # старте сама уведёт на событийный fallback, если опрос мёртв.
            if error_streak >= 30:
                logging.error(
                    "poll listener: probe permanently failing — self-restart "
                    "via launchd KeepAlive"
                )
                sys.stdout.flush(); sys.stderr.flush()
                os._exit(75)  # EX_TEMPFAIL, как у tap revival
            time.sleep(1.0)  # backoff — не спамим лог, edge повторится


def _rescue_wav(wav_path: str) -> Optional[str]:
    """Спасти WAV зависшей записи вместо удаления.

    Watchdog раньше делал unlink — минута речи пропадала безвозвратно.
    Теперь кладём в ~/.config/whisper-skill/rescued/ (0600, как остальные
    приватные файлы) и держим последние 20 штук. Возвращает новый путь
    или None (тогда файл удалён как раньше)."""
    try:
        from datetime import datetime
        rescue_dir = Path.home() / ".config" / "whisper-skill" / "rescued"
        rescue_dir.mkdir(parents=True, exist_ok=True)
        dst = rescue_dir / f"rescued-{datetime.now():%Y%m%d-%H%M%S}.wav"
        os.replace(wav_path, dst)
        try: os.chmod(dst, 0o600)
        except OSError: pass
        for old in sorted(rescue_dir.glob("rescued-*.wav"))[:-20]:
            try: old.unlink()
            except OSError: pass
        return str(dst)
    except Exception as e:
        logging.error(f"rescue wav failed: {e}")
        try: os.unlink(wav_path)
        except Exception: pass
        return None


def main_loop(cfg: dict, cfg_path: Path):
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

    def _on_select_model(new_model: str):
        if new_model == cfg.get("model"):
            return
        try:
            cur = load_config(cfg_path)
            cur["model"] = new_model
            write_config(cfg_path, cur)
        except Exception as e:
            logging.error(f"failed to write new model to config: {e}")
            return
        print(f"🔁 Переключаю модель → {new_model}, перезапуск...")
        # restart_self спавнит новую копию через VBS launcher и os._exit'ит текущую.
        # Новая копия дождётся освобождения mutex (retry в acquire_single_instance_lock).
        restart_self()

    tray = TrayIcon()
    if cfg.get("show_tray") and not is_mac_low_cpu:
        tray.start(
            current_model=cfg.get("model"),
            available_models=list_available_ov_models(),
            on_select_model=_on_select_model,
        )

    # Прогрев модели в фоне: первый hotkey-press не должен ждать
    # компиляцию OpenVINO-графа / загрузку весов faster-whisper.
    # Event сигнализирует завершение warmup'а — work() ждёт его перед
    # первым transcribe. Это решает две проблемы одним механизмом:
    #   1) Cold start первой диктовки: без ожидания первый hotkey ловил
    #      холодную модель + компиляцию OV-графа (5–30с задержка).
    #   2) Race на module import: warmup-thread и work-thread параллельно
    #      делают `from optimum.intel import OVModelForSpeechSeq2Seq`.
    #      transformers._LazyModule под Python 3.12 даёт partially-initialized
    #      module второму thread'у → AttributeError → Python преобразует в
    #      ImportError на первой диктовке.
    warmup_done = threading.Event()
    if cfg.get("warmup", True):
        def _warmup_then_signal():
            try:
                _warmup(transcribe, cfg, tray)
            finally:
                # set даже на failure — иначе work() заблокируется навсегда.
                # Если warmup упал, первый transcribe сам потерпит cold start
                # — это лучше чем deadlock.
                warmup_done.set()

        threading.Thread(target=_warmup_then_signal, daemon=True).start()
    else:
        warmup_done.set()

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
            state.record_started_at = time.time()
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
        # Точка → катушка ровно в той же позиции возле курсора. Скрываем
        # индикатор только когда текст уже вставлен (в work() finally) или
        # на ранних выходах ниже.
        if cursor_ind:
            cursor_ind.show_transcribing()

        # Сначала закрываем микрофон, потом играем бип. Параллельный запуск
        # winsound во время stream.close() PortAudio даёт повторное звучание
        # (наблюдалось 2026-05-01: один вызов PlaySound → два слышимых тона).
        wav_path = recorder.stop()
        if cfg.get("play_sound"):
            threading.Thread(target=play_stop_beep, daemon=True).start()
        if not wav_path:
            tray.set_state("idle")
            if cursor_ind:
                cursor_ind.hide()
            return
        duration_ms = recorder.duration_sec * 1000

        if duration_ms < cfg.get("min_duration_ms", 300):
            print(f"⏭  Skipped (too short: {duration_ms:.0f}ms)")
            try: os.unlink(wav_path)
            except: pass
            tray.set_state("idle")
            if cursor_ind:
                cursor_ind.hide()
            return

        print(f"⏳ Transcribing {duration_ms:.0f}ms of audio...")
        state.is_transcribing = True

        def work():
            try:
                if not warmup_done.is_set():
                    print("⏳ Waiting for model warmup to finish...")
                    warmup_done.wait()
                t0 = time.time()
                vocab_raw = cfg.get("vocabulary") or []
                # Defense: vocabulary должен быть list[str]. Защита от опечаток
                # (строка вместо массива, числа внутри списка и т.п.).
                if isinstance(vocab_raw, list):
                    vocab = [w for w in vocab_raw if isinstance(w, str) and w.strip()]
                else:
                    vocab = []
                # Soft warning ОДИН раз на процесс при риске упереться в
                # whisper-лимит ~244 токенов. Грубая эвристика: средне 2 токена
                # на слово + наша обёртка → safe-зона ~100 слов.
                if vocab and len(vocab) > 100 and not getattr(state, "_vocab_warned", False):
                    logging.warning(
                        f"vocabulary has {len(vocab)} entries — рискуем упереться "
                        f"в whisper initial_prompt limit (~244 tokens)"
                    )
                    state._vocab_warned = True
                initial_prompt = (
                    "В разговоре могут упоминаться: " + ", ".join(vocab) + "."
                    if vocab else None
                )
                result = transcribe(
                    wav_path,
                    language=cfg.get("language"),
                    model_name=cfg.get("model"),
                    word_timestamps=False,
                    verbose=False,
                    initial_prompt=initial_prompt,
                    # гасим whisper repetition-loops («Greek Greek Greek…»):
                    # для коротких диктовок контекст от прошлого сегмента не нужен
                    condition_on_previous_text=cfg.get("condition_on_previous_text", False),
                )
                text = result.text.strip()
                # Страховка от repetition-loops (даже с condition_on_previous_text
                # =False иногда проскакивает). Меряем масштаб петли и чистим:
                text, loop_run = _collapse_repetition_loops(
                    text, threshold=cfg.get("repetition_loop_threshold", 4)
                )
                # Патологическая петля = ВСЯ диктовка галлюцинация (этих слов
                # пользователь не говорил) → не вставляем ничего, пусть переговорит.
                discard_at = cfg.get("repetition_loop_discard_threshold", 10)
                if discard_at and loop_run >= discard_at:
                    logging.warning(
                        f"repetition loop ({loop_run} слов повтора) — диктовка отброшена целиком как галлюцинация"
                    )
                    print(f"⏭  Repetition loop ({loop_run} слов) — галлюцинация, ничего не вставляю")
                    text = ""
                replacements = cfg.get("replacements") or {}
                if isinstance(replacements, dict):
                    for src, dst in replacements.items():
                        if not src or not isinstance(src, str) or not isinstance(dst, str):
                            continue
                        # lambda для dst — иначе re.sub интерпретирует backref'ы
                        # (\1, \g<name>) в значении замены. Lambda даёт literal-replacement.
                        text = re.sub(
                            rf"\b{re.escape(src)}\b",
                            lambda _m, _d=dst: _d,
                            text,
                            flags=re.IGNORECASE,
                        )
                # То же, но ключ — raw regex (для брендов, что коверкаются
                # множеством способов). Битый regex логируем и пропускаем.
                replacements_regex = cfg.get("replacements_regex") or {}
                if isinstance(replacements_regex, dict):
                    for pat, dst in replacements_regex.items():
                        if not pat or not isinstance(pat, str) or not isinstance(dst, str):
                            continue
                        try:
                            text = re.sub(
                                pat,
                                lambda _m, _d=dst: _d,
                                text,
                                flags=re.IGNORECASE,
                            )
                        except re.error as e:
                            logging.warning(f"bad replacements_regex {pat!r}: {e}")
                # Вырезаем известные whisper-галлюцинации (титры/кредиты).
                # Битый паттерн логируем и пропускаем — диктовка важнее.
                hall_patterns = cfg.get("hallucination_patterns") or []
                if isinstance(hall_patterns, list) and hall_patterns:
                    for pat in hall_patterns:
                        if not pat or not isinstance(pat, str):
                            continue
                        try:
                            text = re.sub(pat, "", text, flags=re.IGNORECASE)
                        except re.error as e:
                            logging.warning(f"bad hallucination_pattern {pat!r}: {e}")
                    # схлопнуть двойные пробелы (но не трогать \n) и снять
                    # повисшие пробелы по краям после вырезания
                    text = re.sub(r"[ \t]{2,}", " ", text).strip()
                    # если после вычистки остались только пробелы/пунктуация —
                    # это была чистая галлюцинация на тишине, отдаём пусто
                    if text and not re.search(r"\w", text):
                        text = ""
                elapsed = time.time() - t0

                if not text:
                    print("⏭  Empty transcription")
                else:
                    # Опция privacy: если log_transcribed_text=false, не палим текст
                    # в лог — только длину и время. Текст всё равно ушёл в clipboard
                    # и вставился, просто не оседает на диске. Для случаев когда
                    # диктуешь пароли/ключи/чувствительное.
                    if cfg.get("log_transcribed_text", True):
                        print(f"✓ ({elapsed:.1f}s) → {text}")
                    else:
                        print(f"✓ ({elapsed:.1f}s) → [{len(text)} chars, hidden]")
                    # Если предыдущая диктовка была недавно — начинаем
                    # новую с переноса строки. Порог 30s — «продолжаем
                    # в то же место»; после большой паузы вставка чистая.
                    newline_threshold_s = cfg.get("newline_after_dictation_within_sec", 30.0)
                    now = time.time()
                    if state.last_dictation_at and (now - state.last_dictation_at) < newline_threshold_s:
                        text_to_paste = "\n" + text
                    else:
                        text_to_paste = text

                    saved_clipboard = save_clipboard() if cfg.get("auto_paste") else None
                    copy_to_clipboard(text_to_paste)
                    if cfg.get("auto_paste"):
                        time.sleep(0.25)  # дать целевому полю стать активным
                        paste_from_clipboard()
                        # Восстанавливаем буфер асинхронно через 1с —
                        # таргет должен успеть обработать WM_PASTE и
                        # прочитать диктованный текст до того, как мы
                        # вернём оригинал.
                        restore_clipboard_deferred(saved_clipboard, delay_sec=1.0)
                    state.last_dictation_at = now
            except Exception as e:
                print(f"❌ Transcription failed: {e}")
            finally:
                state.is_transcribing = False
                tray.set_state("idle")
                if cursor_ind:
                    cursor_ind.hide()
                try: os.unlink(wav_path)
                except: pass

        threading.Thread(target=work, daemon=True).start()

    def toggle():
        if state.is_recording:
            stop_and_transcribe()
        else:
            start_recording()

    def _watchdog():
        # Если pynput потерял on_release event, is_recording=True зависает
        # навсегда. Watchdog отбрасывает аудио и сбрасывает state — НЕ пытается
        # транскрибировать и вставить (иначе случайный 90-сек залип = минута
        # звука вставлена куда попало в случайное активное поле, плохой UX).
        #
        # Race-safety: state-мутация атомарно внутри state_lock, чтобы listener
        # thread (stop_and_transcribe) не вошёл в свою обработку одновременно.
        # recorder.stop() вынесен наружу lock'а — он сам потокобезопасен через
        # внутренний _recording-флаг (двойной вызов вернёт None).
        while True:
            time.sleep(_WATCHDOG_TICK_SECONDS)

            # Атомарно: проверь + захвати ответственность за cleanup
            with state_lock:
                if not state.is_recording or state.record_started_at <= 0:
                    continue
                elapsed = time.time() - state.record_started_at
                if elapsed <= _MAX_RECORDING_SECONDS:
                    continue
                # Мы первые — listener thread теперь не войдёт в stop_and_transcribe
                state.is_recording = False
                state.record_started_at = 0.0

            logging.warning(
                f"watchdog: stuck recording {elapsed:.0f}s — rescuing audio, "
                f"state reset (pynput on_release event likely lost)"
            )
            print(
                f"⚠️  Watchdog: запись зависла на {elapsed:.0f}с — "
                f"стейт сброшен, готов к новому хоткею."
            )

            try:
                wav_path = recorder.stop()
                if wav_path:
                    # Не вставляем никуда (см. комментарий выше), но и не
                    # удаляем: текст достаётся вручную через
                    # `python -m examples.transcribe_one <файл>`.
                    rescued = _rescue_wav(wav_path)
                    if rescued:
                        print(f"💾 Аудио спасено: {rescued}")
            except Exception as e:
                logging.error(f"watchdog recorder.stop failed: {e}")

            tray.set_state("idle")
            if cursor_ind:
                try: cursor_ind.hide()
                except: pass
            if cfg.get("play_sound"):
                threading.Thread(target=play_stop_beep, daemon=True).start()

    threading.Thread(target=_watchdog, daemon=True).start()

    # ── Tap-revival watchdog (macOS CGEventTap auto-revive) ────────────────
    # pynput на macOS не обрабатывает kCGEventTapDisabledByTimeout /
    # kCGEventTapDisabledByUserInput — когда система отключает event tap
    # (callback задержался, sleep/wake, и т.п.), pynput не переподнимает его
    # и хоткей перестаёт работать до restart процесса. Чиним поллингом
    # CGEventTapIsEnabled из отдельного потока: если disabled →
    # CGEventTapEnable(True). Tap оживает мгновенно, без перезапуска демона.
    # Заодно: если запись была активна в момент смерти tap'а (on_release
    # потерялся → микрофон висит), сбрасываем state и закрываем стрим
    # сразу, не дожидаясь _MAX_RECORDING_SECONDS обычного watchdog'а.
    def _patch_pynput_for_tap_capture():
        """Монкипатч pynput чтобы созданный CGEventTap сохранялся на listener._cg_tap.
        КРИТИЧНО: должен применяться ДО создания Listener — иначе Listener.start()
        в своём потоке вызовет ORIGINAL _create_event_tap, и tap-reference никогда
        не появится. Идемпотентно: повторные вызовы — no-op."""
        if platform.system() != "Darwin":
            return False
        try:
            import pynput._util.darwin as _pd_util
        except Exception as e:
            logging.warning(f"tap revival: pynput unavailable ({e}), skipping")
            return False
        if getattr(_pd_util.ListenerMixin, "_cg_tap_exposed", False):
            return True
        _orig_create = _pd_util.ListenerMixin._create_event_tap

        def _create_with_capture(self):
            tap = _orig_create(self)
            try:
                self._cg_tap = tap
            except Exception:
                pass
            return tap

        _pd_util.ListenerMixin._create_event_tap = _create_with_capture
        _pd_util.ListenerMixin._cg_tap_exposed = True
        return True

    def _install_tap_revival(listener, currently_pressed_ref=None, keys_needed_ref=None):
        if platform.system() != "Darwin":
            return
        try:
            from Quartz import CGEventTapIsEnabled, CGEventTapEnable
        except Exception as e:
            logging.warning(f"tap revival: Quartz unavailable ({e}), skipping")
            return

        def _emergency_cleanup_if_recording():
            # Раньше здесь запись выбрасывалась БЕЗУСЛОВНО — и мигание tap'а
            # посреди длинной диктовки стоило всего надиктованного, хотя юзер
            # ещё держал хоткей и продолжал говорить. Теперь сверяемся с
            # физическим состоянием клавиш (не зависит от tap'а):
            if keys_needed_ref:
                phys = _mac_hotkey_physically_down(keys_needed_ref)
                if phys is True:
                    # Хоткей реально зажат → юзер диктует. Tap только что
                    # реанимирован (release придёт), а потеряется — release-
                    # поллер закроет запись штатно. Не трогаем.
                    logging.warning(
                        "tap revival: recording active at tap-death, hotkey "
                        "still physically held — keeping recording alive"
                    )
                    return
                if phys is False:
                    # Уже отпустил → release-поллер остановит и ТРАНСКРИБИРУЕТ
                    # (тик 0.2с). Не дублируем стоп и не выбрасываем аудио.
                    logging.warning(
                        "tap revival: recording active at tap-death, hotkey "
                        "released — deferring to release poller"
                    )
                    return
                # phys is None — физическая проверка недоступна: старое
                # поведение ниже (сброс с отбросом аудио) как fallback.
            with state_lock:
                if not state.is_recording:
                    return
                elapsed = (
                    time.time() - state.record_started_at
                    if state.record_started_at > 0 else 0.0
                )
                state.is_recording = False
                state.record_started_at = 0.0
            logging.warning(
                f"tap revival: recording active ({elapsed:.1f}s) at tap-death — "
                f"on_release lost, discarding audio"
            )
            print(
                f"⚠️  Запись сброшена ({elapsed:.1f}с) — release потерялся при "
                f"отвале tap'а."
            )
            if currently_pressed_ref is not None:
                currently_pressed_ref.clear()
            try:
                wav_path = recorder.stop()
                if wav_path:
                    try: os.unlink(wav_path)
                    except Exception: pass
            except Exception as e:
                logging.error(f"emergency recorder.stop failed: {e}")
            try: tray.set_state("idle")
            except Exception: pass
            if cursor_ind:
                try: cursor_ind.hide()
                except Exception: pass
            if cfg.get("play_sound"):
                threading.Thread(target=play_stop_beep, daemon=True).start()

        def _revival_loop():
            deadline = time.time() + 10.0
            tap = None
            while time.time() < deadline:
                tap = getattr(listener, "_cg_tap", None)
                if tap is not None:
                    break
                time.sleep(0.1)
            if tap is None:
                logging.warning(
                    "tap revival: tap not exposed within 10s, giving up"
                )
                print("⚠️  Tap revival watchdog НЕ поднялся (monkeypatch race?)")
                return

            # WARNING-уровень (не INFO), иначе невидимо в launchd-запуске
            # без --verbose. Один раз за процесс — шума ноль.
            logging.warning(
                f"tap revival: armed (poll every {_TAP_REVIVAL_TICK_SECONDS}s)"
            )
            print(f"🛡  Tap revival watchdog armed (poll {_TAP_REVIVAL_TICK_SECONDS}s)")

            revivals = 0
            consecutive_revivals = 0  # подряд без healthy tick → триггер self-restart
            error_streak = 0
            last_log_at = 0.0  # rate-limit чтобы не спамить если tap "не лечится"

            while getattr(listener, "running", True):
                time.sleep(_TAP_REVIVAL_TICK_SECONDS)
                try:
                    if CGEventTapIsEnabled(tap):
                        error_streak = 0
                        consecutive_revivals = 0
                        continue
                    CGEventTapEnable(tap, True)
                    revivals += 1
                    consecutive_revivals += 1
                    _emergency_cleanup_if_recording()
                    now = time.time()
                    # Первые 5 revival'ов — лог как обычно. Дальше — раз в 30с,
                    # с подсказкой про restart.sh: если tap "лечится" десятки
                    # раз — значит ссылка stale (CGEventTapEnable silent no-op).
                    if revivals <= 5:
                        logging.warning(
                            f"tap revival: CGEventTap was disabled — "
                            f"re-enabled (#{revivals}) "
                            f"[load1m={os.getloadavg()[0]:.1f}]"
                        )
                        print(
                            f"♻️  Tap revived (#{revivals}) — hotkey работает дальше."
                        )
                        last_log_at = now
                    elif (now - last_log_at) >= 30.0:
                        logging.error(
                            f"tap revival: tap keeps dying ({revivals}× total, "
                            f"{consecutive_revivals} consecutive) — "
                            f"tap reference likely stale"
                        )
                        print(
                            f"⚠️  Tap revived {revivals}× за сессию "
                            f"({consecutive_revivals} подряд)"
                        )
                        last_log_at = now

                    # Stale-tap self-restart: re-enable не помогает, ссылка мёртвая.
                    # Умираем — launchd KeepAlive поднимет новый процесс с новым tap.
                    if consecutive_revivals >= _TAP_STALE_RESTART_THRESHOLD:
                        logging.error(
                            f"tap revival: {consecutive_revivals} consecutive "
                            f"failed revivals — self-restarting via launchd KeepAlive "
                            f"[load1m={os.getloadavg()[0]:.1f}]"
                        )
                        print(
                            f"🔄 Tap zombie ({consecutive_revivals} попыток "
                            f"подряд) — рестартую процесс через launchd…"
                        )
                        # Flush чтобы лог не потерялся при hard exit
                        sys.stdout.flush()
                        sys.stderr.flush()
                        for _h in logging.root.handlers:
                            try: _h.flush()
                            except Exception: pass
                        # launchd по KeepAlive=true в com.whisper.dictation.plist
                        # перезапустит нас автоматически за ~1 секунду
                        os._exit(75)  # EX_TEMPFAIL
                except Exception as e:
                    error_streak += 1
                    now = time.time()
                    # Exponential backoff: 2,4,8,16,32,60s.
                    # Лог первые 3 ошибки, дальше раз в 60с — против log-storm
                    # если CGEventTap*-вызовы кидают перманентно.
                    if error_streak <= 3 or (now - last_log_at) >= 60.0:
                        logging.error(
                            f"tap revival loop error #{error_streak}: {e}"
                        )
                        last_log_at = now
                    time.sleep(min(60.0, 2.0 ** min(error_streak, 6)))

        threading.Thread(target=_revival_loop, daemon=True).start()

    hotkey_str = cfg["hotkey"]
    print(f"🎤 Whisper Voice Dictation активна")
    print(f"   Хоткей: {hotkey_str} ({cfg['mode']})")
    print(f"   Модель: {cfg['model']}")
    print(f"   Язык:   {cfg.get('language') or 'auto'}")
    print(f"\nНажми {hotkey_str} чтобы говорить. Ctrl+C чтобы выйти.\n")

    if cfg["mode"] == "ptt":
        # Push-to-talk: нажал → запись, отпустил → транскрибировать
        keys_needed = _parse_hotkey(hotkey_str)
        currently_pressed = set()

        # Дефолтный путь на macOS — poll-слушатель (см. _poll_ptt_loop:
        # никакого event tap → нечему отваливаться и терять release).
        # mac_poll_listener: false в конфиге вернёт событийный путь ниже —
        # откат одной строкой, без правки кода. Probe-проверка: если хоткей
        # не опрашиваем (незнакомая клавиша), честно падаем в события.
        use_poll_listener = (
            platform.system() == "Darwin"
            and cfg.get("mac_poll_listener", True)
            and _mac_hotkey_physically_down(keys_needed) is not None
        )
        if use_poll_listener:
            logging.warning(
                f"poll listener: armed (active {_POLL_LISTENER_TICK_SECONDS * 1000:.0f}ms / "
                f"idle {_POLL_LISTENER_IDLE_TICK_SECONDS * 1000:.0f}ms) "
                f"— event tap не используется, revival/monkeypatch не нужны"
            )
            print(
                f"🛡  Poll-слушатель: опрос {1 / _POLL_LISTENER_IDLE_TICK_SECONDS:.0f}×/с в простое, "
                f"{1 / _POLL_LISTENER_TICK_SECONDS:.0f}×/с при зажатом хоткее, без event tap"
            )
            try:
                _poll_ptt_loop(
                    probe=lambda: _mac_hotkey_physically_down(keys_needed),
                    on_down=start_recording,
                    on_up=stop_and_transcribe,
                    idle_tick=_POLL_LISTENER_IDLE_TICK_SECONDS,
                )
            except KeyboardInterrupt:
                pass
            print("\n👋 Bye")
            return 0

        # ── Событийный путь (fallback: не-Mac / mac_poll_listener=false) ──
        # ВАЖНО: монкипатч pynput надо применить ДО `with keyboard.Listener(...)`,
        # иначе Listener.start() успеет вызвать original _create_event_tap в
        # своём потоке и tap-reference никогда не сохранится.
        _patch_pynput_for_tap_capture()

        def on_press(key):
            currently_pressed.add(_canonical_key(key))
            if keys_needed.issubset(currently_pressed):
                start_recording()

        def on_release(key):
            ck = _canonical_key(key)
            if ck in keys_needed and state.is_recording:
                stop_and_transcribe()
            currently_pressed.discard(ck)

        # ── Release-поллер: страховка от потерянного on_release ────────
        # Независимый от CGEventTap источник правды: опрос физического
        # состояния клавиш. Запись активна, хоткей физически отпущен →
        # release потерялся → останавливаем и транскрибируем как обычно.
        # Это главный фикс «залипшего Ctrl»: раньше запись висела 90с и
        # watchdog выбрасывал весь надиктованный текст.
        def _release_poller():
            # Smoke-probe: если проверка недоступна (нет Quartz / незнакомые
            # клавиши в хоткее) — честно выключаемся, работаем как раньше.
            # Хоткей сейчас не зажат → probe вернёт False; None = недоступно.
            if _mac_hotkey_physically_down(keys_needed) is None:
                logging.warning(
                    "release poller: physical key-state check unavailable "
                    "for this hotkey — poller disabled (events only)"
                )
                return
            logging.warning(
                f"release poller: armed (poll every {_RELEASE_POLL_TICK_SECONDS}s)"
            )
            print(f"🛡  Release poller armed (poll {_RELEASE_POLL_TICK_SECONDS}s)")
            while True:
                time.sleep(_RELEASE_POLL_TICK_SECONDS)
                with state_lock:
                    recording = state.is_recording
                    started_at = state.record_started_at
                if not recording or started_at <= 0:
                    continue
                if (time.time() - started_at) < _RELEASE_POLL_GRACE_SECONDS:
                    continue
                if _mac_hotkey_physically_down(keys_needed) is not False:
                    continue  # зажат (True) или неопределимо (None) — молчим
                elapsed = time.time() - started_at
                logging.warning(
                    f"release poller: hotkey physically released but recording "
                    f"still active ({elapsed:.1f}s) — on_release lost, "
                    f"stopping + transcribing"
                )
                print(
                    f"♻️  Release-поллер: хоткей отпущен ({elapsed:.1f}с) — "
                    f"останавливаю и транскрибирую."
                )
                # Фантомные «зажатые» клавиши мешали бы следующему on_press
                currently_pressed.clear()
                # Идемпотентно: если штатный on_release успел первым,
                # внутренний guard выйдет по not is_recording.
                stop_and_transcribe()

        threading.Thread(target=_release_poller, daemon=True).start()

        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            _install_tap_revival(listener, currently_pressed, keys_needed)
            try:
                listener.join()
            except KeyboardInterrupt:
                pass
    else:
        # Toggle: нажал → старт, нажал ещё раз → стоп
        # Монкипатч до создания listener'а — см. комментарий в ptt-ветке.
        _patch_pynput_for_tap_capture()
        with keyboard.GlobalHotKeys({hotkey_str: toggle}) as listener:
            _install_tap_revival(listener, None)
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


_LOG_ROTATE_BYTES = 5 * 1024 * 1024


def _attach_log_file(log_path: str, verbose: bool) -> None:
    """Перенаправить stdout/stderr/logging в файл.

    Нужен и для отладки (видно что транскрибируется), и чтобы под pythonw.exe
    (autostart) print() не падал молча — там sys.stdout/sys.stderr = None.
    """
    expanded = os.path.expandvars(os.path.expanduser(log_path))
    Path(expanded).parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.path.getsize(expanded) > _LOG_ROTATE_BYTES:
            backup = expanded + ".old"
            try:
                os.replace(expanded, backup)
                # Rotated backup тоже tightened — иначе ./dictation.log.old
                # копит транскрипции на 644 пока не будет вручную удалён.
                try: os.chmod(backup, 0o600)
                except OSError: pass
            except OSError: pass
    except OSError:
        pass
    fh = open(expanded, "a", buffering=1, encoding="utf-8", errors="replace")
    # Privacy: лог содержит plaintext транскрипции голоса. Закрываем чтение
    # для group/other (default 644 → 600). Идемпотентно при каждом старте.
    try: os.chmod(expanded, 0o600)
    except OSError: pass
    sys.stdout = fh
    sys.stderr = fh
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(fh)],
        force=True,
    )
    from datetime import datetime as _dt
    fh.write(f"\n--- voice_dictation started {_dt.now().isoformat(timespec='seconds')} ---\n")


def main():
    p = argparse.ArgumentParser(description="Push-to-talk голосовая диктовка через Whisper")
    p.add_argument("--config", default=None, help="Путь к JSON-конфигу")
    p.add_argument("--setup", action="store_true", help="Создать дефолтный конфиг и выйти")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    if args.setup:
        logging.basicConfig(
            level=logging.INFO if args.verbose else logging.WARNING,
            format="%(asctime)s %(levelname)s %(message)s",
        )
        setup_wizard()
        return 0

    cfg_path = Path(args.config) if args.config else default_config_path()
    cfg = load_config(cfg_path)

    if cfg.get("log_file"):
        _attach_log_file(cfg["log_file"], args.verbose)
    else:
        logging.basicConfig(
            level=logging.INFO if args.verbose else logging.WARNING,
            format="%(asctime)s %(levelname)s %(message)s",
        )

    # Single-instance: вторая копия (autostart + ярлык, или ручной запуск
    # поверх работающей) выходит тихо. Retry — на случай self-restart при
    # переключении модели через tray-меню.
    if not acquire_single_instance_lock(timeout_seconds=2.0):
        logging.info("Another voice_dictation instance is already running — exiting silently.")
        return 0

    # Fast mode for dictation: greedy decoding, no temperature fallback
    os.environ.setdefault("WHISPER_BEAM_SIZE", "1")
    os.environ.setdefault("WHISPER_BEST_OF", "1")
    os.environ.setdefault("WHISPER_CONDITION_ON_PREV", "0")

    # Apply backend selection from config (must happen before transcribe is imported)
    if cfg.get("backend"):
        os.environ["WHISPER_BACKEND"] = cfg["backend"]
    if cfg.get("ov_device"):
        os.environ["WHISPER_OV_DEVICE"] = cfg["ov_device"]

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

    return main_loop(cfg, cfg_path)


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
