# -*- coding: utf-8 -*-
"""
all_chats_download — плагин для AyuGram/Telegrah (плагины на Python, API `ctx`).

Что делает:
  Добавляет пункт "💾 Скачать чат" в меню «три точки» внутри чата/группы.
  По нажатию выгружает всю доступную историю переписки в .txt и сохраняет
  файл в папку «Загрузки» на устройстве.

Формат файла:
  Чат: Название
  Экспортировано: 2026-10-09 15:30
  Всего сообщений: 12453
  ─────────────────────────────
  [2024-01-01 10:15] Иван: Привет
  [2024-01-01 10:16] Мария: Привет, как дела?
  [2024-01-01 10:17] Иван: [фото]

Для больших чатов: во время выгрузки показывается прогресс
"Загружено X / Y" с возможностью отменить (кнопка "Отменить").

Совместимость: плагин рассчитан на Telegrah-совместимый хост (AyuGram).
Если хост не предоставляет menu-колбэки или ctx.client, плагин регистрирует
команду /download_chat (или !export) как запасной способ запуска экспорта.
"""

import asyncio
import os
import re
import sys
from datetime import datetime

# Метаданные плагина (совпадают с plugin.toml внутри архива .plugin)
PLUGIN_ID = "all_chats_download"
PLUGIN_NAME = "Скачать чат"
PLUGIN_VERSION = "1.0.0"
PLUGIN_DESCRIPTION = ("Экспорт всей истории открытого чата/группы в .txt "
                      "в папку «Загрузки» (пункт 💾 в меню «три точки»).")

IS_ANDROID = sys.platform == "android"

if IS_ANDROID:
    # На Android нет единой «папки Загрузки»: используем стандартный путь
    # общего хранилища (content://downloads или /storage/emulated/0/Download).
    try:
        from android.storage import primary_external_storage_path  # type: ignore
        _EXT = primary_external_storage_path() or "/storage/emulated/0"
    except Exception:
        _EXT = "/storage/emulated/0"
    DOWNLOADS_DIR = os.path.join(_EXT, "Download")
else:
    home = os.path.expanduser("~")
    ru_name = "\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0438"  # «Загрузки»
    ru_dir = os.path.join(home, ru_name)
    xdg = os.environ.get("XDG_DOWNLOAD_DIR") or ""
    if (os.path.isdir(ru_dir) or not os.path.isdir(home) or
            not os.path.isdir(os.path.join(home, "Downloads"))) and xdg != ru_dir:
        # Русская локаль или нет стандартной папки Downloads — создаём «Загрузки»
        DOWNLOADS_DIR = ru_dir
    else:
        DOWNLOADS_DIR = xdg or os.path.join(home, "Downloads")
    try:
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    except Exception:
        DOWNLOADS_DIR = ru_dir
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

BATCH_SIZE = 100          # размер пачки при запросе истории (~100 сообщений)
PROGRESS_EVERY = 500      # как часто обновлять счётчик прогресса
BAR_WIDTH = 12            # ширина текстового прогресс-бара

# Единственное хранилище состояния плагина — модульный словарь `state`.
#   state["menu"]    -> id пункта меню «три точки»;
#   state["chat_id"] -> чат, открытый в момент добавления пункта;
#   state["cancel"]  -> asyncio.Event отмены текущей выгрузки.
# Хост (Telegrah API) передаёт в init_plugin свой state_dict для персистентного
# хранения данных плагина — мы копируем в него наши значения при инициализации,
# но вся рабочая логика использует только `state`, чтобы не держать две
# синхронизируемые структуры.
state = {"menu": None, "chat_id": None, "cancel": None}


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    """Имя файла для чата: недопустимые символы (и пробелы) заменяются на '_'.

    Итог — 'chat_НазваниеЧата_2026-10-09.txt' как в концепции: дефисы в дате
    остаются (они допустимы в именах файлов), а пробелы и всякая нечисть
    ('/', '\\', ':', '*', '?', кавычки, control-символы) превращаются в '_'.
    """
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    name = name.replace(" ", "_")
    name = re.sub(r"_+", "_", name).strip("_")
    name = name[:80] if name else ""
    return name or "chat"


def get_media_label(message) -> str:
    """Пометка для нетекстовых сообщений: [фото], [видео], [голосовое] и т.д."""
    m = message
    if getattr(m, "sticker", None):
        return "[стикер]"
    if getattr(m, "voice", None):
        return "[голосовое]"
    if getattr(m, "audio", None):
        return "[аудио]"
    if getattr(m, "video_note", None):
        return "[видеосообщение]"
    if getattr(m, "video", None):
        return "[видео]"
    if getattr(m, "round_message", None) or getattr(m, "round_video", None):
        return "[видеосообщение]"
    if getattr(m, "photo", None):
        return "[фото]"
    if getattr(m, "document", None):
        return "[документ]"
    if getattr(m, "contact", None):
        return "[контакт]"
    if getattr(m, "location", None) or getattr(m, "geo", None):
        return "[геопозиция]"
    if getattr(m, "poll", None):
        return "[опрос]"
    if getattr(m, "invoice", None):
        return "[счёт]"
    if getattr(m, "game", None):
        return "[игра]"
    if getattr(m, "gif", None):
        return "[gif]"
    if getattr(m, "venue", None):
        return "[место]"
    action = getattr(m, "action", None)
    if action:
        return "[%s]" % str(action).lower()
    return "[не текст]"


def format_sender(sender, chat_id) -> str:
    """Имя отправителя: 'Вы' для себя, иначе первое/последнее имя или @username."""
    if sender is None:
        return "Неизвестный"
    if getattr(sender, "self", False):
        return "Вы"
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    name = (first + " " + last).strip()
    if not name:
        name = getattr(sender, "title", "") or ""  # канал/бот без имени
    if not name:
        uname = getattr(sender, "username", "") or ""
        name = "@" + uname if uname else str(getattr(sender, "id", chat_id))
    return name


def format_line(message, sender, chat_id):
    """Запись вида (timestamp, '[2024-01-01 10:15] Иван: Привет').

    timestamp нужен для итоговой сортировки строк по дате — так порядок в
    файле корректен независимо от того, в каком порядке и какими пачками
    пришли сообщения. Для сообщений без даты используем id (или 0), чтобы
    они не падали в начало/конец файла случайно.
    """
    dt = message.date
    if dt is not None and getattr(dt, "tzinfo", None) is not None:
        try:
            dt = dt.astimezone()  # к локальному часовому поясу устройства
        except Exception:
            pass
    stamp = dt.strftime("%Y-%m-%d %H:%M") if dt else "????-??-?? --:--"
    # timestamp для сортировки; datetime.timestamp может не существовать
    # (или бросить исключение) у нестандартных объектов даты — подстраховываемся.
    ts = None
    try:
        ts = float(dt.timestamp())
    except (AttributeError, TypeError, ValueError, OSError):
        ts = None
    if ts is None:
        ts = float(getattr(message, "id", 0) or 0)
    who = format_sender(sender, chat_id)
    text = (message.message or "").strip()
    body = text if text else get_media_label(message)
    return ts, "[%s] %s: %s" % (stamp, who, body)


def progress_bar(done: int, total: int) -> str:
    if total <= 0:
        return "?" * BAR_WIDTH
    filled = min(BAR_WIDTH, done * BAR_WIDTH // total)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


async def get_dialog_total(client, entity):
    """Общее число сообщений в чате — для счётчика прогресса «X / Y».

    У Telethon нет client.get_messages_count(); количество хранится в
    самом диалоге (PeerDialog.total_messages), куда его кладёт сервер при
    get_dialogs(). Поэтому сначала пробуем достать готовое значение из
    entity (если хост передал диалог/сущность с этим полем), затем —
    запросить список диалогов и найти нужный. Если ничего не найдено,
    возвращаем 0: экспорт продолжится, прогресс будет показан как
    «Загружено X сообщений…» без общего числа.
    """
    # 1. Уже известное значение (диалог из кэша хоста / PeerDialog).
    for src in (entity, getattr(entity, "_dialog", None)):
        total = getattr(src, "total_messages", None)
        if isinstance(total, int) and total > 0:
            return total

    # 2. Запрос диалогов (тяжёлая операция — только один раз, перед выгрузкой).
    chat_id = getattr(entity, "id", None)
    try:
        async for dialog in client.iter_dialogs():
            d_id = getattr(getattr(dialog, "entity", None), "id", None)
            if d_id is not None and d_id == chat_id:
                t = getattr(dialog, "total_messages", None)
                if isinstance(t, int) and t >= 0:
                    return t
                break
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# Экспорт
# ---------------------------------------------------------------------------

class ExportCancelled(Exception):
    """Пользователь отменил выгрузку."""


async def export_chat(client, entity, cancel_event, on_progress=None):
    """Собирает историю пачками по ~100 сообщений и пишет .txt в «Загрузки».

    Возвращает кортеж (путь_к_файлу, количество сообщений) либо бросает
    ExportCancelled, если был установлен флаг отмены.
    """
    title = getattr(entity, "title", None) or \
        getattr(entity, "username", None) or "Чат"

    total = await get_dialog_total(client, entity)

    records = []            # [(timestamp, line), ...] — порядок итерации не важен
    cancelled = False
    last_progress_ts = 0.0

    # iter_messages отдаёт сообщения от новых к старым; Telethon внутри
    # запрашивает их пачками (размер пачки ~100 сообщений).
    # Собираем в чанки по BATCH_SIZE — так проще контролировать отмену.
    # Каждая строка хранится вместе с timestamp, поэтому итоговый файл
    # сортируется по дате независимо от порядка/размера пачек.
    chunk = []

    async def flush_chunk():
        nonlocal last_progress_ts
        for message in chunk:
            # get_sender() — это метод Telethon; если у объекта сообщения его
            # нет (или это не coroutine-версия), берём готовое свойство .sender,
            # а при неудаче — None ("Неизвестный").
            sender = None
            try:
                getter = getattr(message, "get_sender", None)
                if callable(getter):
                    res = getter()
                    if asyncio.iscoroutine(res):
                        sender = await res
                    else:
                        sender = res
                else:
                    sender = getattr(message, "sender", None)
            except Exception:
                sender = None
            chat_id = getattr(getattr(message, "chat", None), "id", None)
            records.append(format_line(message, sender, chat_id))
        chunk.clear()
        now = asyncio.get_event_loop().time()
        if on_progress and now - last_progress_ts >= 0.8:
            last_progress_ts = now
            if total > 0:
                txt = ("⏬ Экспорт чата: %s\nЗагружено %d / %d %s" % (
                    title, len(records), total,
                    progress_bar(len(records), total)))
            else:
                txt = "⏬ Экспорт чата: %s\nЗагружено %d сообщений…" % (
                    title, len(records))
            on_progress(txt)

    async for message in client.iter_messages(entity, reverse=False,
                                              wait_time=0):
        if cancel_event.is_set():
            cancelled = True
            break
        chunk.append(message)
        if len(chunk) >= BATCH_SIZE:
            await flush_chunk()
        if cancel_event.is_set():
            cancelled = True
            break
    # Финальную пачку тоже не пишем, если пользователь нажал «Отменить»,
    # и проверяем флаг ещё раз перед формированием файла — чтобы отменённый
    # экспорт гарантированно ничего не сохранял в «Загрузки».
    if cancel_event.is_set():
        cancelled = True
    if cancelled:
        raise ExportCancelled()
    if chunk:
        await flush_chunk()

    # Сортировка по timestamp даёт строго хронологический порядок строк
    # независимо от порядка итерации и размера пачек (устойчивая сортировка
    # сохраняет относительный порядок сообщений с одинаковой датой).
    records.sort(key=lambda r: r[0])
    lines = [ln for _ts, ln in records]

    fname = "chat_%s_%s.txt" % (sanitize_filename(title),
                                 datetime.now().strftime("%Y-%m-%d"))
    path = os.path.join(DOWNLOADS_DIR, fname)

    header = [
        "Чат: %s" % title,
        "Экспортировано: %s" % datetime.now().strftime("%Y-%m-%d %H:%M"),
        "Всего сообщений: %d" % len(lines),
        "─" * 29,
    ]
    # Собираем через list+join вместо инкрементальной конкатенации строк
    # (O(n^2) на больших чатах).
    body = "\n".join(header + lines) + "\n"

    # Запись — в отдельном потоке, чтобы не блокировать UI при больших объёмах.
    def _write():
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)

    await asyncio.to_thread(_write)

    return path, len(lines)


# ---------------------------------------------------------------------------
# UI: запуск экспорта, диалог результата, пункт меню
# ---------------------------------------------------------------------------

async def ask_dialog(ctx, title, message, buttons):
    """Показать диалог и дождаться выбора пользователя.

    Хосты бывают разные: ctx.show_dialog может быть синхронным (вернул выбор
    сразу), возвращать coroutine/Future (выбор придёт позже) или не существовать
    вовсе. Здесь вся эта тройственность нормализуется к одному await-результату.

    Возвращает id нажатой кнопки ("acd_cancel"/"acd_open"/...) или "" — если
    хост не умеет диалоги либо ничего не было выбрано.
    """
    show = getattr(ctx, "show_dialog", None)
    if not callable(show):
        return ""
    res = show(title=title, message=message,
               buttons=[{"text": t, "id": i} for t, i in buttons])
    # coroutine / Future / Awaitable -> ждём реальный ответ
    if asyncio.iscoroutine(res) or isinstance(res, asyncio.Future):
        try:
            res = await res
        except asyncio.CancelledError:
            raise
        except Exception:
            return ""
    # синхронный хост уже вернул dict/str/None
    if isinstance(res, dict):
        return str(res.get("button", "") or res.get("id", ""))
    if isinstance(res, str):
        return res
    return ""


def ctx_dialog_shown(ctx) -> bool:
    """Открыт ли у хоста хотя бы один диалог (для ожидания старта прогресса).

    У реального Telegrah-хоста такого метода может не быть — тогда считаем,
    что диалог уже виден, и не ждём.
    """
    for name in ("is_dialog_shown", "dialog_shown", "has_dialog"):
        fn = getattr(ctx, name, None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                return True
    return True


async def run_export(ctx, chat_id):
    client = ctx.client
    cancel = asyncio.Event()
    state["cancel"] = cancel

    tg = getattr(ctx, "tg", None)

    def toast(text):
        if tg is not None:
            try:
                tg.showToast(text)
            except Exception:
                pass

    toast("Начинаю экспорт чата…")

    dialog_open = True  # пока открыт прогресс-диалог — обновляем его текстом

    def _update_progress(text):
        # Пытаемся обновить открытый диалог; если недоступно — toast.
        updated = False
        upd = getattr(ctx, "update_dialog", None)
        if callable(upd) and dialog_open:
            try:
                res = upd(message=text)
                if asyncio.iscoroutine(res):
                    asyncio.ensure_future(res)
                updated = True
            except Exception:
                updated = False
        if not updated:
            toast(text.replace("\n", " "))

    async def _progress_dialog():
        # ask_dialog вызывается ровно один раз и всегда через await —
        # вся работа с «синхронный/асинхронный хост» спрятана внутри ask_dialog.
        choice = await ask_dialog(
            ctx,
            "Экспорт чата 💾",
            "Подготовка к выгрузке…",
            [("Отменить", "acd_cancel")],
        )
        if choice == "acd_cancel":
            cancel.set()

    # Прогресс-диалог запускаем отдельной задачей. Первым делом ставим
    # yield-паузу: за этот тик цикл успевает запустить _progress_dialog,
    # который откроет диалог (и найдёт уже зарегистрированный state["cancel"]),
    # а экспорт стартует только когда диалог точно открыт. Иначе на полностью
    # синхронном/быстром моке диалог мог бы «ответить» раньше, чем мы начнём
    # читать сообщения, и отмена не сработала бы.
    dlg_task = asyncio.ensure_future(_progress_dialog())

    def _on_dialog_done(fut):
        nonlocal dialog_open
        dialog_open = False
        try:
            fut.result()
        except (asyncio.CancelledError, Exception):
            pass

    dlg_task.add_done_callback(_on_dialog_done)

    # Ждём, пока прогресс-диалог реально открыт (или задача завершилась).
    for _ in range(50):
        await asyncio.sleep(0)
        if ctx_dialog_shown(ctx) or dlg_task.done():
            break

    async def _close_progress_dialog():
        """Спокойно закрыть прогресс-диалог перед показом финального."""
        nonlocal dialog_open
        dialog_open = False
        close = getattr(ctx, "close_dialog", None)
        if callable(close):
            try:
                res = close()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass
        if dlg_task is not None and not dlg_task.done():
            dlg_task.cancel()
            try:
                await dlg_task
            except (asyncio.CancelledError, Exception):
                pass

    try:
        entity = await client.get_entity(chat_id)
        path, count = await export_chat(client, entity, cancel,
                                        on_progress=_update_progress)

        await _close_progress_dialog()

        # Уведомление: «Чат экспортирован, файл в Загрузках» + кнопки.
        # Тот же самый ask_dialog, что и для прогресса — единая логика.
        choice = await ask_dialog(
            ctx,
            "Готово 💾",
            ("Чат экспортирован (%d сообщений).\n"
             "Файл в папке «Загрузки»:\n%s" % (count, path)),
            [("📂 Открыть папку", "acd_open"), ("ОК", "acd_close")],
        )
        if choice == "acd_open":
            await open_folder(ctx, path)
    except ExportCancelled:
        await _close_progress_dialog()
        toast("Экспорт отменён")
    except asyncio.CancelledError:
        await _close_progress_dialog()
        toast("Экспорт отменён")
        raise
    except Exception as e:
        await _close_progress_dialog()
        try:
            await ctx.show_alert("Ошибка экспорта: %r" % (e,))
        except Exception:
            print("[all_chats_download] error:", repr(e))
    finally:
        state["cancel"] = None


async def open_folder(ctx, path):
    """Открыть папку с файлом (Android: показать файл; desktop: файловый менеджер)."""
    folder = os.path.dirname(path)
    opened = False

    tg = getattr(ctx, "tg", None)
    if IS_ANDROID and tg is not None:
        # Telegrah API: открыть файл через внешнее приложение.
        for fn in ("openFile", "shareFile"):
            f = getattr(tg, fn, None)
            if callable(f):
                try:
                    res = f(path)
                    if asyncio.iscoroutine(res):
                        await res
                    opened = True
                    break
                except Exception:
                    continue

    if not opened:
        import subprocess
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
            opened = True
        except Exception:
            opened = False

    if not opened:
        try:
            await ctx.copy_to_clipboard(folder)
            if tg is not None:
                tg.showToast("Путь скопирован: %s" % folder)
        except Exception:
            pass


async def add_button(ctx):
    """Добавить пункт 💾 в меню «три точки» текущего чата."""
    removed = False
    old = state.get("menu")
    if old is not None:
        try:
            await ctx.remove_menu(old)
            removed = True
        except Exception:
            pass
    if removed:
        state["menu"] = None

    chat_id = None
    try:
        chat_id = await ctx.get_current_chat_id()
    except Exception:
        pass
    if chat_id is None:
        try:
            c = await ctx.get_current_chat()
            chat_id = c.id
        except Exception:
            chat_id = None
    state["chat_id"] = chat_id

    if chat_id is None:
        mid = await ctx.add_menu_item(
            "💾 Сначала откройте чат", lambda: _noop())
        state["menu"] = mid
        return

    async def on_click():
        cid = state.get("chat_id")
        if cid is None:
            try:
                await ctx.show_alert("Не удалось определить открытый чат.")
            except Exception:
                pass
            return
        # Выгрузка — в фоне, чтобы не тормозить интерфейс.
        asyncio.ensure_future(run_export(ctx, cid))

    mid = await ctx.add_menu_item("💾 Скачать чат", on_click)
    state["menu"] = mid


async def _noop():
    pass


async def handle_command(ctx, args):
    """Запасной способ запуска: /download_chat (или !export)."""
    chat_id = None
    try:
        chat_id = await ctx.get_current_chat_id()
    except Exception:
        pass
    if chat_id is None:
        try:
            chat_id = ctx.event.chat_id
        except Exception:
            chat_id = None
    if chat_id is None:
        try:
            await ctx.show_alert("Откройте чат, который нужно экспортировать.")
        except Exception:
            pass
        return
    asyncio.ensure_future(run_export(ctx, chat_id))


async def handle_callback(ctx, data):
    if data == "acd_cancel":
        ev = state.get("cancel")
        if ev is not None:
            ev.set()
    # acd_open обработан сразу в show_dialog, здесь ничего не делаем.


# ---------------------------------------------------------------------------
# Точка входа плагина
# ---------------------------------------------------------------------------

async def init_plugin(ctx, state_dict):
    """Точка входа. state_dict — персистентное хранилище плагина от хоста;
    мы сохраняем в него идентификатор пункта меню (чтобы при перезапуске
    плагина хост мог его подхватить), но вся рабочая логика живёт в
    модульном словаре `state` — вторая копия состояния не заводится."""
    st = state_dict if isinstance(state_dict, dict) else {}
    # Восстановление из персистентного хранилища не требуется: id пункта меню
    # привязан к сессии UI, а чат/отмена — к текущему запуску экспорта.
    st["menu"] = None          # id пункта «три точки» (обновляется в add_button)
    st["add_button"] = add_button
    st["handle_command"] = handle_command
    st["handle_callback"] = handle_callback

    # Пункт в меню «три точки»
    try:
        await add_button(ctx)
    except Exception:
        pass
    st["menu"] = state["menu"]  # синхронизируем персистентную копию
    try:
        ctx.on("update_chat_focus", lambda *_a, **_k: asyncio.ensure_future(add_button(ctx)))
    except Exception:
        pass
    try:
        ctx.on("enter_chat", lambda *_a, **_k: asyncio.ensure_future(add_button(ctx)))
    except Exception:
        pass

    # Команда как запасной вариант
    try:
        ctx.add_command("download_chat", handle_command,
                        description="Выгрузить историю чата в .txt в папку «Загрузки»")
    except Exception:
        try:
            ctx.add_command("!export", handle_command)
        except Exception:
            pass

    # Колбэки диалога
    try:
        ctx.on("callback", handle_callback)
    except Exception:
        pass

    # Метаданные плагина для хоста (Telegrah/AyuGram): читаются из
    # plugin.toml, но не помешает объявить их и здесь. Иконка — icon.png
    # из архива .plugin (значок загрузки с эмодзи сообщения поверх).
    try:
        st["manifest"] = {
            "id": PLUGIN_ID,
            "name": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "description": PLUGIN_DESCRIPTION,
            "icon": "icon.png",
        }
    except Exception:
        pass
