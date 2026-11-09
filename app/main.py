"""Точка входа: запускает бота, дашборд и фоновые задачи в одном процессе."""
import asyncio
import contextlib
import logging
from zoneinfo import ZoneInfo

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.db import init_db, make_engine, make_sessionmaker
from app.integrations.sheets import SheetsClient
from app.services.approval import ApprovalService
from app.services.sync import SheetSync, StaffSync
from app.web.server import create_app
from app.workflow_config import load_workflow

log = logging.getLogger("app")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    settings = get_settings()
    wf = load_workflow(settings.workflow_file)
    tz = ZoneInfo(settings.timezone)

    engine = make_engine(settings.database_url)
    await init_db(engine)
    sm = make_sessionmaker(engine)
    approval = ApprovalService(wf, open_access=settings.open_access, admins=settings.admin_ids)
    log.info("Режим: %s", "открытый доступ (все роли у каждого пользователя)" if settings.open_access else "рабочий")

    sheets = None
    if settings.sheets_enabled:
        sheets = SheetsClient(settings.google_credentials_path, settings.google_sheet_id, wf.currency)
        try:
            url = await sheets.ensure_worksheets()
            log.info("Google Таблица подключена: %s", url)
        except Exception:
            log.exception("Не удалось подключиться к Google Таблице — проверьте ключ и доступ. "
                          "Бот продолжит работу, журнал допишется позже.")
    else:
        log.info("Google Таблица не настроена — журнал ведётся только в базе")

    sync = SheetSync(sheets, sm, wf, tz)
    staff = StaffSync(sheets, sm, settings.staff_file)
    await staff.run()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(staff.run, "interval", minutes=wf.staff_sync_minutes)
    tasks: list[asyncio.Task] = []

    if sheets:
        tasks.append(asyncio.create_task(sync.run_forever()))
        scheduler.add_job(sync.resync_all, "interval", minutes=30)
        sync.kick()

    dp = bot = None
    if settings.bot_token:
        from app.bot.setup import create_bot, create_dispatcher
        from app.reminders import send_reminders

        bot = create_bot(settings.bot_token)
        dp, notifier = create_dispatcher(bot, sm, approval, wf, tz, sync if sheets else None, settings.open_access)
        scheduler.add_job(send_reminders, "interval", minutes=wf.reminder_check_minutes,
                          args=[sm, approval, notifier])
        me = await bot.get_me()
        log.info("Бот запущен: https://t.me/%s", me.username)
        tasks.append(asyncio.create_task(dp.start_polling(bot, handle_signals=False)))
    else:
        log.warning("BOT_TOKEN не задан — запускаю только дашборд. Добавьте токен в файл .env")

    scheduler.start()

    app = create_app(sm, wf, tz, settings)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.web_host, port=settings.web_port,
                                           log_level="warning"))
    log.info("Дашборд: http://%s:%s", settings.web_host, settings.web_port)
    try:
        await server.serve()  # работает, пока не нажмут Ctrl+C
    finally:
        log.info("Остановка...")
        scheduler.shutdown(wait=False)
        if dp is not None:
            with contextlib.suppress(Exception):
                await dp.stop_polling()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if bot is not None:
            await bot.session.close()
        await engine.dispose()


def run() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
