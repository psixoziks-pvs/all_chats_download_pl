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

# state["menu"] -> id пункта меню; state["chat_id"] -> чат, открытый в момент
# добавления пункта; state["cancel"] -> Event отмены текущей выгрузки
state = {"menu": None, "chat_id": None, "cancel": None}


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    """Имя файла для чата: недопустимые символы (и пробелы) заменяются на '_'.

    'chat_НазваниеЧата_2026-10-09.txt' — как в концепции, т.е. без пробелов.
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


def format_line(message, sender, chat_id) -> str:
    """Строка вида '[2024-01-01 10:15] Иван: Привет' или '[...] Иван: [фото]'."""
    dt = message.date
    if dt is not None and getattr(dt, "tzinfo", None) is not None:
        try:
            dt = dt.astimezone()  # к локальному часовому поясу устройства
        except Exception:
            pass
    stamp = dt.strftime("%Y-%m-%d %H:%M") if dt else "????-??-?? --:--"
    who = format_sender(sender, chat_id)
    text = (message.message or "").strip()
    body = text if text else get_media_label(message)
    return "[%s] %s: %s" % (stamp, who, body)


def progress_bar(done: int, total: int) -> str:
    if total <= 0:
        return "?" * BAR_WIDTH
    filled = min(BAR_WIDTH, done * BAR_WIDTH // total)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


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

    total = 0
    try:
        total = await client.get_messages_count(entity)
    except Exception:
        try:
            total = await entity.get_messages_count()
        except Exception:
            total = 0

    lines = []
    cancelled = False
    last_progress_ts = 0.0

    # iter_messages отдаёт сообщения от новых к старым; Telethon внутри
    # запрашивает их пачками (размер пачки ~100 сообщений).
    # Собираем в чанки по BATCH_SIZE — так проще контролировать отмену.
    chunk = []

    async def flush_chunk():
        nonlocal last_progress_ts
        for message in chunk:
            try:
                sender = await message.get_sender()
            except Exception:
                sender = None
            chat_id = getattr(getattr(message, "chat", None), "id", None)
            lines.append(format_line(message, sender, chat_id))
        chunk.clear()
        now = asyncio.get_event_loop().time()
        if on_progress and now - last_progress_ts >= 0.8:
            last_progress_ts = now
            if total > 0:
                txt = ("⏬ Экспорт чата: %s\nЗагружено %d / %d %s" % (
                    title, len(lines), total,
                    progress_bar(len(lines), total)))
            else:
                txt = "⏬ Экспорт чата: %s\nЗагружено %d сообщений…" % (
                    title, len(lines))
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
    if not cancelled and chunk:
        await flush_chunk()

    if cancelled:
        raise ExportCancelled()

    lines.reverse()  # хронологический порядок: от старых к новым

    fname = "chat_%s_%s.txt" % (sanitize_filename(title),
                                 datetime.now().strftime("%Y-%m-%d"))
    path = os.path.join(DOWNLOADS_DIR, fname)

    header = [
        "Чат: %s" % title,
        "Экспортировано: %s" % datetime.now().strftime("%Y-%m-%d %H:%M"),
        "Всего сообщений: %d" % len(lines),
        "─" * 29,
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(header) + "\n")
        for ln in lines:
            f.write(ln + "\n")

    return path, len(lines)


# ---------------------------------------------------------------------------
# UI: запуск экспорта, диалог результата, пункт меню
# ---------------------------------------------------------------------------

async def run_export(ctx, chat_id):
    client = ctx.client
    cancel = asyncio.Event()
    state["cancel"] = cancel

    tg = getattr(ctx, "tg", None)
    if tg is not None:
        try:
            tg.showToast("Начинаю экспорт чата…")
        except Exception:
            pass

    # Прогресс показываем без засорения чата: сначала — системный диалог
    # с кнопкой «Отменить» (если хост его поддерживает), затем — toast'ы.
    dialog_open = True
    dlg_task = None

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
        tg_ = getattr(ctx, "tg", None)
        if not updated and tg_ is not None:
            try:
                tg_.showToast(text.replace("\n", " "))
            except Exception:
                pass

    async def _on_dialog_done(fut):
        # Диалог закрыт пользователем (нажата «Отменить»).
        nonlocal dialog_open
        dialog_open = False
        try:
            result = fut.result()
        except asyncio.CancelledError:
            return
        except Exception:
            return
        choice = ""
        if isinstance(result, dict):
            choice = result.get("button", "") or result.get("id", "")
        elif isinstance(result, str):
            choice = result
        if choice == "acd_cancel":
            cancel.set()

    try:
        fut = ctx.show_dialog(
            title="Экспорт чата 💾",
            message="Подготовка к выгрузке…",
            buttons=[{"text": "Отменить", "id": "acd_cancel"}],
        )
        if asyncio.iscoroutine(fut) or isinstance(fut, asyncio.Future):
            dlg_task = asyncio.ensure_future(fut)
            dlg_task.add_done_callback(
                lambda f: asyncio.ensure_future(_on_dialog_done(f)))
        else:
            # Синхронный хост: диалог вернул выбор сразу или None
            dialog_open = False
            dlg_task = None
            res = fut
            if isinstance(res, dict):
                res = res.get("button", "") or res.get("id", "")
            if res == "acd_cancel":
                cancel.set()
    except Exception:
        dialog_open = False
        dlg_task = None

    try:
        entity = await client.get_entity(chat_id)
        path, count = await export_chat(client, entity, cancel,
                                        on_progress=_update_progress)

        if dlg_task is not None and not dlg_task.done():
            # закрываем прогресс-диалог, если хост это умеет
            try:
                close = getattr(ctx, "close_dialog", None)
                if callable(close):
                    res = close()
                    if asyncio.iscoroutine(res):
                        await res
            except Exception:
                pass
            dlg_task.cancel()

        # Уведомление: «Чат экспортирован, файл в Загрузках» + кнопки.
        buttons = [{"text": "📂 Открыть папку", "id": "acd_open"},
                   {"text": "ОК", "id": "acd_close"}]
        result = await ctx.show_dialog(
            title="Готово 💾",
            message=("Чат экспортирован (%d сообщений).\n"
                     "Файл в папке «Загрузки»:\n%s" % (count, path)),
            buttons=buttons,
        )
        choice = ""
        if isinstance(result, dict):
            choice = result.get("button", "") or result.get("id", "")
        elif isinstance(result, str):
            choice = result
        if choice == "acd_open":
            await open_folder(ctx, path)
    except ExportCancelled:
        if dlg_task is not None and not dlg_task.done():
            dlg_task.cancel()
        if tg is not None:
            try:
                tg.showToast("Экспорт отменён")
            except Exception:
                pass
    except asyncio.CancelledError:
        if tg is not None:
            try:
                tg.showToast("Экспорт отменён")
            except Exception:
                pass
    except Exception as e:
        if dlg_task is not None and not dlg_task.done():
            dlg_task.cancel()
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
    st = state_dict
    st.setdefault("menu", None)
    st.setdefault("chat_id", None)
    st.setdefault("cancel", None)
    st["add_button"] = add_button
    st["handle_command"] = handle_command
    st["handle_callback"] = handle_callback

    # Пункт в меню «три точки»
    try:
        await add_button(ctx)
    except Exception:
        pass
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
