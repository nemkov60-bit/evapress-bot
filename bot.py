import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

import backup
import handlers_admin
import handlers_trips
import handlers_driver
from config import ADMIN_IDS, BOT_TOKEN, DATA_DIR


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в файле .env")
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS не заданы — админ-функции недоступны. Узнайте свой ID командой /myid.")
    logging.info("Данные бота хранятся в %s", DATA_DIR)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(handlers_trips.router)   # рейсы/точки/ZIP — до общего роутера админа с его «заглушками»
    dp.include_router(handlers_admin.router)
    dp.include_router(handlers_driver.router)
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="myid", description="Мой Telegram ID"),
    ])
    await bot.delete_webhook(drop_pending_updates=False)
    restored = None
    try:
        restored = await backup.startup_restore(bot)
    except Exception as e:  # noqa: BLE001
        logging.exception("Автовосстановление не удалось: %s", e)
    if restored:
        logging.warning("База была пустой — восстановлена из автокопии от %s", restored)
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, f"♻️ <b>Данные бота восстановлены автоматически</b> из автокопии от "
                                            f"{restored}.\nПохоже, хостинг очистил папку с данными при обновлении. "
                                            f"Водители, маршруты, заметки и настройки на месте.")
            except Exception:
                pass
    elif backup.is_empty():
        logging.warning("База данных пустая: %s", DATA_DIR)
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(
                    aid, "⚠️ <b>База данных бота пустая.</b>\n"
                         "Если бот уже работал раньше, значит хостинг не сохранил данные при обновлении "
                         "(проверьте, что DATA_DIR указывает на постоянное хранилище).\n\n"
                         f"Чтобы вернуть данные, отправьте боту последний файл <code>{backup.PREFIX}….zip</code>.")
            except Exception:
                pass
    asyncio.create_task(backup.daily_loop(bot))
    # сразу после запуска — свежая закреплённая автокопия (страховка перед следующей пересборкой)
    asyncio.create_task(backup.autosave(bot, force=True))
    asyncio.create_task(backup.autosave_loop(bot))

    async def on_shutdown():
        # BotHost останавливает бота перед пересборкой — успеваем сохранить последние изменения
        try:
            await asyncio.wait_for(backup.autosave(bot), timeout=20)
        except Exception as e:  # noqa: BLE001
            logging.warning("Автокопия при остановке не удалась: %s", e)

    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
