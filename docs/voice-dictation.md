# Voice Dictation — диктовка вместо клавиатуры

Push-to-talk голосовой ввод во **любое** активное поле (Slack, iMessage, Telegram, VS Code, Obsidian, любой текстовое поле в браузере). Работает локально через Whisper, не отправляет ничего наружу.

> 💰 Заменяет **Superwhisper ($8.49/мес)**, **Wispr Flow ($12/мес)**, **Aqua Voice**, **Whispering** — за **$0**.

## Что нужно поставить

```bash
# В venv где уже стоит твой Whisper-бэкенд (mlx / faster / whisperx):
pip install sounddevice soundfile pynput pyperclip pystray Pillow numpy
```

## Как работает

```
Жмёшь Ctrl+Shift+Space →  🎙 запись
Говоришь...
Отпускаешь          →  ⏳ Whisper
                    →  📋 в clipboard
                    →  ⌨️  Cmd+V в активное поле
```

**Скорость:** на Mac M2 Pro для фразы 5 секунд — транскрибация ~0.3-0.5 сек. Realtime-feel.

## Запуск

```bash
# Создать конфиг (одноразово)
python -m examples.voice_dictation --setup

# Запустить (висит в трее, слушает хоткей)
python -m examples.voice_dictation
```

## Конфиг

Лежит в `~/.config/whisper-skill/voice_dictation.json` (Mac/Linux) или `%APPDATA%\whisper-skill\` (Windows).

```json
{
  "hotkey": "<ctrl>+<shift>+<space>",
  "mode": "ptt",
  "language": "ru",
  "model": "large-v3-turbo",
  "sample_rate": 16000,
  "channels": 1,
  "max_duration_sec": 60,
  "auto_paste": true,
  "play_sound": true,
  "show_tray": true,
  "trim_silence_ms": 200,
  "min_duration_ms": 300
}
```

### Параметры

| Поле | Значения | Описание |
|---|---|---|
| `hotkey` | строка | Глобальный хоткей в формате pynput. Примеры: `<ctrl>+<shift>+<space>`, `<f9>`, `<cmd>+<shift>+v`, `<alt>+<space>` |
| `mode` | `ptt` / `toggle` | **ptt** = держи и говори (push-to-talk). **toggle** = жми чтобы начать, жми ещё чтобы остановить |
| `language` | `null` / `ru` / `en` / ... | `null` — auto-detect (медленнее). Лучше указать |
| `model` | имя | `large-v3-turbo` (дефолт), `large-v3`, `medium`, `small`, `tiny` |
| `auto_paste` | bool | После транскрибации эмулировать Cmd+V/Ctrl+V. Если `false` — только в clipboard |
| `play_sound` | bool | Тихие бипы на старт/стоп для обратной связи |
| `show_tray` | bool | Иконка в трее (меняет цвет: серый/красный/оранжевый) |
| `min_duration_ms` | int | Игнорировать короткие записи (промахи кнопкой) |

### Популярные пресеты хоткеев

```jsonc
// Mac — родной для системы
"hotkey": "<f5>"           // удобно на Mac, обычно ничем не занято

// Универсальный
"hotkey": "<ctrl>+<shift>+<space>"

// Под клавишу на правой стороне (для leftie)
"hotkey": "<right_alt>+<space>"

// Однокнопочный (требует доступности)
"hotkey": "<f9>"
```

## Permissions

### macOS

Нужны два разрешения:

1. **Microphone**:
   `System Settings → Privacy & Security → Microphone → добавить Terminal/iTerm/тот шелл откуда запускаешь`

2. **Accessibility** (для глобального хоткея и эмуляции вставки):
   `System Settings → Privacy & Security → Accessibility → добавить Terminal/iTerm`

После добавления нужно **перезапустить терминал**. Если не работает — кликни ⊖ Remove → ⊕ Add заново.

### Linux

- **X11** — работает out-of-box.
- **Wayland** — глобальные хоткеи частично сломаны на уровне платформы. Workaround:
  - Использовать `evdev` напрямую (нужен root или добавить юзера в группу `input`):
    ```bash
    sudo usermod -aG input $USER
    # перелогиниться
    ```
  - Или отказаться от Wayland (`/etc/gdm3/custom.conf` → `WaylandEnable=false` → перезагрузка)

### Windows

Обычно работает без настройки. Если хоткей перехватывается другим приложением — поменяй в конфиге.

## Сценарии использования

### 1. Кодеру — диктовать комментарии и docstrings

```jsonc
{
  "hotkey": "<f5>",
  "mode": "ptt",
  "language": "en",
  "model": "large-v3-turbo"
}
```

В VS Code/PyCharm кликаешь куда вставить → жмёшь F5 → говоришь → отпускаешь. Текст вставляется.

### 2. В Slack/Telegram быстро ответить

```jsonc
{
  "hotkey": "<ctrl>+<shift>+<space>",
  "mode": "ptt",
  "language": "ru",
  "model": "large-v3-turbo"
}
```

Кликаешь в поле ввода Slack → жмёшь хоткей → говоришь → отпускаешь → Enter.

### 3. Длинная диктовка статьи (toggle mode)

```jsonc
{
  "hotkey": "<ctrl>+<shift>+d",
  "mode": "toggle",
  "language": "ru",
  "model": "large-v3"
}
```

Жмёшь — начало записи, говоришь сколько надо, жмёшь ещё раз — стоп. Удобно для длинных текстов где не хочется держать кнопку 2 минуты.

### 4. Многоязычка (RU + EN термины)

```jsonc
{
  "hotkey": "<f5>",
  "language": null,
  "model": "large-v3"
}
```

Auto-detect языка. Whisper сам понимает RU/EN. Чуть медленнее (auto-detect добавляет 5-10%).

## Запуск в фоне (автостарт)

### macOS — через launchd

Создай `~/Library/LaunchAgents/com.whisper.dictation.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
        "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.whisper.dictation</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/.venv/bin/python</string>
        <string>-m</string>
        <string>examples.voice_dictation</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/whisper-skill</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/whisper_dictation.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/whisper_dictation.err</string>
</dict>
</plist>
```

Загрузи:
```bash
launchctl load ~/Library/LaunchAgents/com.whisper.dictation.plist
```

### Linux — через systemd user service

`~/.config/systemd/user/whisper-dictation.service`:

```ini
[Unit]
Description=Whisper Voice Dictation
After=graphical-session.target

[Service]
Type=simple
WorkingDirectory=%h/whisper-skill
ExecStart=%h/whisper-skill/.venv/bin/python -m examples.voice_dictation
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now whisper-dictation
```

### Windows — Task Scheduler

`Win+R` → `taskschd.msc` → Create Task:
- General: запуск при логине пользователя
- Triggers: At log on
- Actions: Start a program → `python.exe`, аргументы `-m examples.voice_dictation`, путь рабочей папки = whisper-skill

## Известные грабли

### Хоткей не срабатывает на Mac

→ Не дано Accessibility-разрешение. Зайди в `System Settings → Privacy & Security → Accessibility` и добавь Terminal (откуда запущено). После — **перезапусти терминал**.

### "Microphone permission denied"

→ `System Settings → Privacy & Security → Microphone` → добавь Terminal.

### Текст вставился, но без пробела

→ Whisper иногда не ставит пробел в начале. Открой конфиг и поправь `auto_paste: false`, тогда текст только в clipboard, ты вставишь руками с правильным пробелом.

### Транскрибация очень медленная (>3 сек)

→ Используешь не оптимальный бэкенд / модель. Запусти `python scripts/detect_env.py` и проверь.

### Hotkey не работает в специфичном приложении

→ Некоторые приложения (особенно игры в полный экран) перехватывают input до глобального уровня. Workaround — использовать другой хоткей через конфиг.

### Запись пустая

→ Проверь что выбран правильный микрофон в системе. Скрипт использует **системный default input device**.

### "Жжёт" батарею в фоне

→ В idle (без хоткея) скрипт не пишет ничего и расход CPU = ~0.5%. Жжёт только во время самой транскрибации.

## Сравнение с платными аналогами

| Функция | Local Whisper | Superwhisper $8.49 | Wispr Flow $12 | Aqua Voice $9.99 |
|---|---|---|---|---|
| Цена/мес | **$0** | $8.49 | $12 | $9.99 |
| Локально | ✅ | ⚠ | ✅ | ⚠ |
| Скорость на M-чипе | ~0.3-0.5s | ~0.3s | ~0.5s | ~0.5s |
| Языки | 99 | 99 | EN focus | EN focus |
| Кастом хоткей | ✅ | ✅ | ⚠ | ⚠ |
| Open source | ✅ | ❌ | ❌ | ❌ |
| AI пост-обработка | ⚠ (через LLM) | ✅ | ✅ | ✅ |

> 💡 **Что у платных лучше:** AI пост-обработка (исправление пунктуации, форматирование под контекст). У нас её нет в дефолте, но можно добавить через `examples/voice_dictation_with_llm.py` который пускает результат через Claude/GPT перед вставкой.

## Расширения

### Пост-обработка через LLM (исправить пунктуацию + форматирование)

Скопируй `voice_dictation.py` → `voice_dictation_llm.py`, в функции `work()` добавь:

```python
import anthropic
client = anthropic.Anthropic()

raw_text = result.text.strip()
fixed = client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=512,
    system="Расставь пунктуацию и заглавные буквы. Не меняй смысл и слова. Не добавляй ничего своего.",
    messages=[{"role": "user", "content": raw_text}],
).content[0].text

copy_to_clipboard(fixed)
```

Цена за обработку 100 диктовок в день через Haiku ≈ $0.30/мес. Дешевле чем Superwhisper.

### Команды-триггеры

Можно добавить парсинг команд: «новая строка», «точка», «открой Notion», «закрой окно». Обработка через `if/elif` в `work()`.

### Per-app промпты

Через AppleScript определять активное приложение и подставлять разный `initial_prompt`:
- В Slack — initial_prompt с глоссарием команды
- В IDE — с глоссарием технических терминов
