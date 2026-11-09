"""Работа с Google Таблицей через Google Sheets API (библиотека gspread).

Три листа:
  «Журнал»     — одна строка на заявку, бот обновляет её при каждом изменении
  «История»    — каждое действие отдельной строкой (кто, что, когда)
  «Сотрудники» — люди и роли; заполняют люди, бот только читает
"""
import asyncio
import logging
import threading
from pathlib import Path

import gspread

from app.integrations.sheet_style import (
    HISTORY_STYLE,
    JOURNAL_STYLE,
    STAFF_STYLE,
    cleanup_requests,
    style_requests,
)

log = logging.getLogger(__name__)

DEFAULT_SHEET_TITLES = ("Лист1", "Sheet1")

JOURNAL = "Журнал"
HISTORY = "История"
STAFF = "Сотрудники"

JOURNAL_HEADERS = [
    "№", "Создана", "Автор", "Отдел", "Тип", "Сумма", "Описание", "Статус",
    "Текущий этап", "Ответственные", "На этапе с", "Причина отказа / аннулирования", "Закрыта", "Обновлено",
]
HISTORY_HEADERS = ["Время", "№ заявки", "Действие", "Этап", "Кто", "Комментарий"]
STAFF_HEADERS = ["Telegram", "ФИО", "Отдел", "Роль", "Активен"]


def _col_letter(n: int) -> str:
    result = ""
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


class SheetsClient:
    def __init__(self, credentials_file: Path, sheet_id: str, currency: str = "₸"):
        self._credentials_file = credentials_file
        self._currency = currency
        self._sheet_id = sheet_id
        self._spreadsheet = None
        self._worksheets: dict[str, gspread.Worksheet] = {}
        self._lock = threading.Lock()  # gspread не рассчитан на параллельные вызовы

    # Все вызовы gspread блокирующие — выполняем их в отдельном потоке,
    # чтобы бот не «замирал», пока Google отвечает.
    async def _run(self, fn, *args):
        def locked():
            with self._lock:
                return fn(*args)
        return await asyncio.to_thread(locked)

    def _ws(self, title: str, headers: list[str]) -> gspread.Worksheet:
        if title in self._worksheets:
            return self._worksheets[title]
        if self._spreadsheet is None:
            client = gspread.service_account(filename=str(self._credentials_file))
            self._spreadsheet = client.open_by_key(self._sheet_id)
        try:
            ws = self._spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self._spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
        if not ws.row_values(1):
            ws.update(values=[headers], range_name="A1")
            ws.freeze(rows=1)
        self._worksheets[title] = ws
        return ws

    # ---------- синхронные реализации ----------

    def _ensure_all(self) -> str:
        self._ws(JOURNAL, JOURNAL_HEADERS)
        self._ws(HISTORY, HISTORY_HEADERS)
        self._ws(STAFF, STAFF_HEADERS)
        try:
            self._apply_style()
        except Exception:
            log.exception("Не удалось оформить листы таблицы — данные пишутся без оформления")
        return self._spreadsheet.url

    def _apply_style(self) -> None:
        sh = self._spreadsheet
        meta = sh.fetch_sheet_metadata(params={
            "fields": "sheets(properties(sheetId,title),bandedRanges(bandedRangeId),conditionalFormats)",
        })
        by_title = {s["properties"]["title"]: s for s in meta["sheets"]}
        managed = [(JOURNAL, JOURNAL_HEADERS, JOURNAL_STYLE),
                   (HISTORY, HISTORY_HEADERS, HISTORY_STYLE),
                   (STAFF, STAFF_HEADERS, STAFF_STYLE)]
        requests: list[dict] = []
        for index, (title, headers, style) in enumerate(managed):
            sheet = by_title[title]
            requests += cleanup_requests(sheet)
            requests += style_requests(sheet["properties"]["sheetId"], len(headers), style, index, self._currency)
        # Пустой лист, который Google создаёт вместе с таблицей, только мешает
        for title in DEFAULT_SHEET_TITLES:
            if title in by_title and not any(any(row) for row in sh.worksheet(title).get_all_values()):
                requests.append({"deleteSheet": {"sheetId": by_title[title]["properties"]["sheetId"]}})
        sh.batch_update({"requests": requests})

    def _upsert_journal_rows(self, rows: list[list]) -> None:
        ws = self._ws(JOURNAL, JOURNAL_HEADERS)
        ids = ws.col_values(1)  # один запрос вместо поиска по каждой заявке
        row_by_id = {value: i + 1 for i, value in enumerate(ids) if i > 0}
        last_col = _col_letter(len(JOURNAL_HEADERS))
        updates, appends = [], []
        for row in rows:
            n = row_by_id.get(str(row[0]))
            if n:
                updates.append({"range": f"A{n}:{last_col}{n}", "values": [row]})
            else:
                appends.append(row)
        if updates:
            ws.batch_update(updates, value_input_option="RAW")
        if appends:
            # Новые — сразу под шапкой, самые свежие сверху.
            # RAW — чтобы текст вида «=СУММ(...)» из описания не стал формулой.
            ws.insert_rows(appends[::-1], row=2, value_input_option="RAW")

    def _prepend_history(self, rows: list[list]) -> None:
        if rows:
            self._ws(HISTORY, HISTORY_HEADERS).insert_rows(rows[::-1], row=2, value_input_option="RAW")

    def _rewrite(self, title: str, headers: list[str], rows: list[list]) -> None:
        ws = self._ws(title, headers)
        ws.clear()
        ws.update(values=[headers] + rows, range_name="A1", value_input_option="RAW")

    def _read_staff(self) -> list[dict]:
        return self._ws(STAFF, STAFF_HEADERS).get_all_records(expected_headers=STAFF_HEADERS[:4])

    # ---------- асинхронный интерфейс ----------

    async def ensure_worksheets(self) -> str:
        return await self._run(self._ensure_all)

    async def upsert_journal_rows(self, rows: list[list]) -> None:
        await self._run(self._upsert_journal_rows, rows)

    async def prepend_history(self, rows: list[list]) -> None:
        """rows — в хронологическом порядке; на лист попадут свежими сверху."""
        await self._run(self._prepend_history, rows)

    async def rewrite_journal(self, rows: list[list]) -> None:
        await self._run(self._rewrite, JOURNAL, JOURNAL_HEADERS, rows)

    async def rewrite_history(self, rows: list[list]) -> None:
        await self._run(self._rewrite, HISTORY, HISTORY_HEADERS, rows)

    async def read_staff(self) -> list[dict]:
        return await self._run(self._read_staff)
