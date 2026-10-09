# -*- coding: utf-8 -*-
"""Тест плагина all_chats_download с мок-окружением (без реального Telegram).

Запуск:
    python tests/test_plugin.py            # успешный сценарий
    ACD_CANCEL_AT=1 python tests/test_plugin.py   # сценарий отмены
"""
import asyncio
import datetime as dt
import os
import sys
import types

# Тестовый HOME, чтобы «Загрузки» падали во временную папку
import shutil

TEST_HOME = "/tmp/acd_test_home"
shutil.rmtree(TEST_HOME, ignore_errors=True)
os.makedirs(TEST_HOME)
os.environ["HOME"] = TEST_HOME
# Тестируем локализацию на русском (эмуляция системного языка устройства);
# без этой переменной язык берётся из окружения/локали ОС.
os.environ.setdefault("ACD_FORCE_LANG", "ru")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib
import plugin
importlib.reload(plugin)  # перечитать DOWNLOADS_DIR после подмены HOME

assert plugin.DOWNLOADS_DIR == os.path.join(TEST_HOME, "Загрузки"), \
    plugin.DOWNLOADS_DIR


class FakeMessage:
    def __init__(self, id, date, text=None, sender_id=1, **media):
        self.id = id
        self.date = date
        self.message = text
        self._sender_id = sender_id
        for k in ("photo", "video", "voice", "sticker", "document", "audio",
                  "video_note", "poll", "contact", "location", "game",
                  "gif", "invoice", "venue", "round_message", "action"):
            setattr(self, k, media.get(k))
        self.chat = types.SimpleNamespace(id=-100123)

    async def get_sender(self):
        return SENDERS[self._sender_id]


SENDERS = {
    1: types.SimpleNamespace(self=True, first_name="", last_name="",
                             username="me", id=1, title=""),
    2: types.SimpleNamespace(self=False, first_name="Иван", last_name="",
                             username="ivan", id=2, title=""),
    3: types.SimpleNamespace(self=False, first_name="Мария", last_name="",
                             username="maria", id=3, title=""),
    4: types.SimpleNamespace(self=False, first_name="", last_name="",
                             username="", id=4, title="Канал Новости"),
}

MESSAGES = [
    FakeMessage(3, dt.datetime(2024, 1, 1, 10, 17), None, 2, photo=[1]),
    FakeMessage(2, dt.datetime(2024, 1, 1, 10, 16), "Привет, как дела?", 3),
    FakeMessage(1, dt.datetime(2024, 1, 1, 10, 15), "Привет", 2),
    FakeMessage(4, dt.datetime(2024, 1, 1, 10, 18), None, 3, voice=object()),
    FakeMessage(5, dt.datetime(2024, 1, 1, 10, 19), None, 4, sticker=object()),
    FakeMessage(6, dt.datetime(2024, 1, 1, 10, 20), None, 2, document=object()),
]


class FakeClient:
    async def get_entity(self, chat_id):
        return types.SimpleNamespace(id=chat_id,
                                     title="Рабочий чат <тест>/group",
                                     username=None)

    async def iter_dialogs(self):
        # как в Telethon: PeerDialog c total_messages
        yield types.SimpleNamespace(
            entity=types.SimpleNamespace(id=-100123),
            total_messages=len(MESSAGES))

    async def iter_messages(self, entity, reverse=False, wait_time=0):
        for m in MESSAGES:  # от новых к старым, как в Telethon
            yield m


CANCEL_AT = int(os.environ.get("ACD_CANCEL_AT", "0"))  # 0 = не отменять


class FakeCtx:
    client = FakeClient()

    def __init__(self):
        self.dialogs = []
        self.toasts = []
        self.alerts = []
        self.menu_items = {}
        self._mid = 0

    async def get_current_chat_id(self):
        return -100123

    async def add_menu_item(self, text, callback):
        self._mid += 1
        self.menu_items[self._mid] = (text, callback)
        return self._mid

    async def remove_menu(self, mid):
        del self.menu_items[mid]

    async def show_dialog(self, title="", message="", buttons=None):
        self.dialogs.append({"title": title, "message": message,
                             "buttons": buttons})
        texts = [b["text"] for b in (buttons or [])]
        if CANCEL_AT and ("\u041e\u0442\u043c\u0435\u043d\u0438\u0442\u044c" in texts
                          or "Cancel" in texts):
            # имитируем нажатие «Отменить»: ставим флаг отмены,
            # когда экспорт его зарегистрирует
            async def _click():
                for _ in range(500):
                    ev = plugin.state.get("cancel")
                    if ev is not None:
                        ev.set()
                        return
                    await asyncio.sleep(0.01)
            asyncio.ensure_future(_click())
            return {"button": "acd_cancel"}
        return {"button": "acd_open" if any(
            x.startswith("\U0001F4C2 ") for x in texts) else "acd_close"}

    async def show_alert(self, text):
        self.alerts.append(text)

    def update_dialog(self, message=""):
        if self.dialogs:
            self.dialogs[-1]["message"] = message


async def main():
    ctx = FakeCtx()
    st = {}
    await plugin.init_plugin(ctx, st)

    # 1. Пункт меню добавлен
    assert any("💾 Скачать чат" in t for t, _ in ctx.menu_items.values()), \
        ctx.menu_items
    print("OK: кнопка 💾 «Скачать чат» в меню «три точки» зарегистрирована")

    # 2. Клик по кнопке -> фоновый экспорт
    _, cb = [(t, c) for t, c in ctx.menu_items.values()
             if "\U0001F4BE" in t][0]
    await cb()

    # ждём завершения/отмены
    for _ in range(300):
        await asyncio.sleep(0.02)
        files = [f for f in os.listdir(plugin.DOWNLOADS_DIR)
                 if f.endswith(".txt")]
        if files or ctx.alerts or plugin.state.get("cancel") is None and \
                (files or CANCEL_AT):
            if files or ctx.alerts:
                break
        if CANCEL_AT and not plugin.state.get("cancel"):
            break

    files = [f for f in os.listdir(plugin.DOWNLOADS_DIR) if f.endswith(".txt")]

    if CANCEL_AT:
        assert not files, "файл не должен быть создан при отмене: %r" % files
        assert not ctx.alerts, ctx.alerts
        print("OK: отмена работает — экспорт прерван, файл не сохранён")
        print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ (сценарий отмены)")
        return

    assert len(files) == 1, files
    fname = files[0]
    assert fname.startswith("chat_Рабочий_чат_тест_group_"), fname
    assert dt.date.today().strftime("%Y-%m-%d") in fname, fname
    print("OK: имя файла:", fname)

    content = open(os.path.join(plugin.DOWNLOADS_DIR, fname),
                   encoding="utf-8").read()
    print("---- содержимое ----")
    print(content)
    print("--------------------")
    lines = content.splitlines()
    assert lines[0].startswith("Чат: Рабочий чат"), lines[0]
    assert lines[1].startswith("Экспортировано: "), lines[1]
    assert lines[2] == "Всего сообщений: 6", lines[2]
    assert lines[3] == "─" * 29, repr(lines[3])
    assert lines[4] == "[2024-01-01 10:15] Иван: Привет", lines[4]
    assert "[2024-01-01 10:16] Мария: Привет, как дела?" in content
    assert "[2024-01-01 10:17] Иван: [фото]" in content
    assert "[2024-01-01 10:18] Мария: [голосовое]" in content
    assert "[2024-01-01 10:19] Канал Новости: [стикер]" in content
    assert "[2024-01-01 10:20] Иван: [документ]" in content
    print("OK: формат файла соответствует концепции")

    # 3. Диалог результата
    done_dlgs = [d for d in ctx.dialogs
                 if "\u044d\u043a\u0441\u043f\u043e\u0440\u0442\u0438\u0440\u043e\u0432\u0430\u043d"
                 in d["message"].lower() or "exported" in d["message"].lower()]
    assert done_dlgs, ctx.dialogs
    d = done_dlgs[-1]
    btns = [b["text"] for b in d["buttons"]]
    assert any(b.startswith("\U0001F4C2 ") for b in btns), btns
    assert any(b in ("\u041e\u041a", "OK") for b in btns), btns
    print("OK: диалог «Чат экспортирован…» с кнопками", btns)

    # 4. Ошибок не было
    assert not ctx.alerts, ctx.alerts
    print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ (успешный сценарий)")


asyncio.run(main())
