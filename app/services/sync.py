"""Синхронизация базы → Google Таблица и списка сотрудников → база.

Надёжность: заявки и события помечаются флагом «нужно записать в таблицу».
Если Google временно недоступен, ничего не теряется — фоновая задача
допишет всё при следующей попытке.
"""
import asyncio
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.formatting import fmt_dt
from app.integrations.sheets import SheetsClient
from app.models import ACTION_TITLES, STATUS_TITLES, Request, RequestEvent
from app.services.staff import apply_staff, load_staff_yaml, parse_staff_rows
from app.workflow_config import WorkflowConfig

log = logging.getLogger(__name__)


def journal_row(req: Request, wf: WorkflowConfig, tz: ZoneInfo) -> list:
    assignees = ", ".join(u.full_name for u in req.current_assignees())
    return [
        req.id,
        fmt_dt(req.created_at, tz),
        req.author.full_name,
        req.author.department or "",
        wf.type_title(req.type_code),
        float(req.amount),
        req.description,
        STATUS_TITLES.get(req.status, req.status),
        wf.stage_title(req.stage_code) if req.is_open else "",
        assignees,
        fmt_dt(req.stage_entered_at, tz) if req.is_open else "",
        req.close_reason or "",
        fmt_dt(req.closed_at, tz),
        fmt_dt(req.updated_at, tz),
    ]


def history_row(ev: RequestEvent, wf: WorkflowConfig, tz: ZoneInfo) -> list:
    return [
        fmt_dt(ev.created_at, tz),
        ev.request_id,
        ACTION_TITLES.get(ev.action, ev.action),
        wf.stage_title(ev.stage_code) if ev.stage_code else "",
        ev.actor.full_name if ev.actor else "бот",
        ev.comment or "",
    ]


class SheetSync:
    def __init__(self, client: SheetsClient | None, sessionmaker: async_sessionmaker,
                 wf: WorkflowConfig, tz: ZoneInfo):
        self.client = client
        self.sm = sessionmaker
        self.wf = wf
        self.tz = tz
        self._wake = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def kick(self) -> None:
        """Попросить записать изменения в таблицу прямо сейчас."""
        self._wake.set()

    async def run_forever(self, interval: float = 60) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except TimeoutError:
                pass
            self._wake.clear()
            await asyncio.sleep(1)  # собираем несколько быстрых изменений в один запрос
            try:
                await self.flush()
            except Exception:
                log.exception("Не удалось записать изменения в Google Таблицу — повторю позже")

    async def flush(self) -> None:
        if not self.client:
            return
        async with self.sm() as session:
            requests = list((await session.execute(
                select(Request).where(Request.sheet_dirty.is_(True)).order_by(Request.id)
            )).scalars())
            events = list((await session.execute(
                select(RequestEvent).where(RequestEvent.sheet_synced.is_(False)).order_by(RequestEvent.id)
            )).scalars())
            if not requests and not events:
                return

            if requests:
                await self.client.upsert_journal_rows([journal_row(r, self.wf, self.tz) for r in requests])
                for r in requests:
                    # Снимаем флаг, только если заявка не изменилась, пока мы писали в таблицу.
                    # Прямой UPDATE не увеличивает версию — не мешаем пользователям.
                    await session.execute(
                        update(Request).where(Request.id == r.id, Request.version == r.version)
                        .values(sheet_dirty=False)
                        .execution_options(synchronize_session=False)
                    )
            if events:
                await self.client.prepend_history([history_row(e, self.wf, self.tz) for e in events])
                await session.execute(
                    update(RequestEvent).where(RequestEvent.id.in_([e.id for e in events]))
                    .values(sheet_synced=True)
                    .execution_options(synchronize_session=False)
                )
            await session.commit()
            log.info("Google Таблица: обновлено заявок %s, событий %s", len(requests), len(events))

    async def resync_all(self) -> None:
        """Полностью перезаписать «Журнал» и «Историю» — исправляет случайные ручные правки.

        Свежие записи — сверху, как и при обычной синхронизации.
        """
        if not self.client:
            return
        async with self.sm() as session:
            requests = list((await session.execute(select(Request).order_by(Request.id.desc()))).scalars())
            events = list((await session.execute(
                select(RequestEvent).order_by(RequestEvent.id.desc())
            )).scalars())
            await self.client.rewrite_journal([journal_row(r, self.wf, self.tz) for r in requests])
            await self.client.rewrite_history([history_row(e, self.wf, self.tz) for e in events])
            if events:  # уже на листе — обычная синхронизация не должна добавить их второй раз
                await session.execute(
                    update(RequestEvent).where(RequestEvent.id <= events[0].id, RequestEvent.sheet_synced.is_(False))
                    .values(sheet_synced=True)
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
        log.info("Google Таблица: перезаписаны журнал (%s заявок) и история (%s событий)",
                 len(requests), len(events))


class StaffSync:
    def __init__(self, client: SheetsClient | None, sessionmaker: async_sessionmaker, staff_file: str):
        self.client = client
        self.sm = sessionmaker
        self.staff_file = staff_file

    async def run(self) -> None:
        try:
            if self.client:
                rows, source = await self.client.read_staff(), "лист «Сотрудники»"
            else:
                rows, source = load_staff_yaml(self.staff_file), self.staff_file
            records, errors = parse_staff_rows(rows)
            for err in errors:
                log.warning("Список сотрудников: %s", err)
            async with self.sm() as session:
                stats = await apply_staff(session, records)
                await session.commit()
            if any(stats.values()):
                log.info("Сотрудники (%s): %s", source, stats)
        except Exception:
            log.exception("Не удалось обновить список сотрудников")
