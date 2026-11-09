"""Оформление листов Google Таблицы.

Применяется при каждом запуске и полностью заменяет прежнее оформление,
поэтому его можно безопасно менять: старые правила удаляются, новые ставятся заново.
"""
from dataclasses import dataclass, field

HEADER_BG = "#1F3A5F"
HEADER_FG = "#FFFFFF"
BAND_1 = "#FFFFFF"
BAND_2 = "#F4F6F9"

# Цвета статусов: (фон, текст)
GREEN = ("#E6F4EA", "#137333")
RED = ("#FCE8E6", "#B3261E")
AMBER = ("#FEF7E0", "#8A5A00")
BLUE = ("#E8F0FE", "#1A56C4")
GREY = ("#F1F3F4", "#5F6368")


@dataclass
class SheetStyle:
    widths: list[int]                                   # ширина колонок в пикселях
    center: list[int] = field(default_factory=list)     # колонки с выравниванием по центру
    money: list[int] = field(default_factory=list)      # колонки с суммой
    highlight: dict[int, dict[str, tuple[str, str]]] = field(default_factory=dict)  # колонка → {текст: цвет}
    dropdowns: dict[int, list[str]] = field(default_factory=dict)  # колонка → варианты
    tab_color: str = HEADER_BG


JOURNAL_STYLE = SheetStyle(
    widths=[55, 125, 165, 120, 130, 115, 280, 140, 140, 170, 125, 220, 125, 125],
    center=[0, 1, 7, 10, 12, 13],
    money=[5],
    highlight={7: {
        "Исполнена": GREEN, "Отклонена": RED, "На согласовании": AMBER, "Отозвана": GREY,
        "Аннулирована": GREY,
    }},
    tab_color="#2A78D6",
)

HISTORY_STYLE = SheetStyle(
    widths=[125, 85, 160, 140, 170, 320],
    center=[0, 1],
    highlight={2: {
        "Создана": BLUE, "Одобрена": GREEN, "Исполнена": GREEN, "Отклонена": RED,
        "Напоминание": AMBER, "Отозвана автором": GREY, "Этап пропущен": GREY,
        "Аннулирована": GREY, "Возвращена на этап": BLUE,
    }},
    tab_color="#5F6368",
)

STAFF_STYLE = SheetStyle(
    widths=[160, 210, 160, 150, 95],
    center=[4],
    highlight={4: {"нет": GREY}},
    dropdowns={
        3: ["сотрудник", "руководитель", "директор", "бухгалтер", "администратор"],
        4: ["да", "нет"],
    },
    tab_color="#137333",
)


def _rgb(hex_color: str) -> dict:
    h = hex_color.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def _cols(sheet_id: int, start: int, end: int | None = None, first_row: int = 1) -> dict:
    rng = {"sheetId": sheet_id, "startRowIndex": first_row, "startColumnIndex": start}
    rng["endColumnIndex"] = end if end is not None else start + 1
    return rng


def cleanup_requests(sheet_meta: dict) -> list[dict]:
    """Удаляет прежние полосы и правила подсветки, чтобы они не копились."""
    sheet_id = sheet_meta["properties"]["sheetId"]
    reqs = [{"deleteBanding": {"bandedRangeId": b["bandedRangeId"]}}
            for b in sheet_meta.get("bandedRanges", [])]
    reqs += [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}}
             for _ in sheet_meta.get("conditionalFormats", [])]
    return reqs


def style_requests(sheet_id: int, n_cols: int, style: SheetStyle, index: int,
                   currency: str = "₸") -> list[dict]:
    reqs: list[dict] = [
        {"updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id, "index": index, "tabColor": _rgb(style.tab_color),
                "gridProperties": {"frozenRowCount": 1, "hideGridlines": True},
            },
            "fields": "index,tabColor,gridProperties.frozenRowCount,gridProperties.hideGridlines",
        }},
        # Шапка
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": n_cols},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _rgb(HEADER_BG),
                "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP",
                "textFormat": {"bold": True, "fontSize": 10, "foregroundColor": _rgb(HEADER_FG)},
            }},
            "fields": "userEnteredFormat("
                      "backgroundColor,horizontalAlignment,verticalAlignment,wrapStrategy,textFormat)",
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 36}, "fields": "pixelSize",
        }},
        # Тело таблицы
        {"repeatCell": {
            "range": _cols(sheet_id, 0, n_cols),
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE", "wrapStrategy": "CLIP",
                "textFormat": {"fontSize": 10},
            }},
            "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment,wrapStrategy,textFormat)",
        }},
        # Полосы через строку
        {"addBanding": {"bandedRange": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": n_cols},
            "rowProperties": {
                "headerColor": _rgb(HEADER_BG),
                "firstBandColor": _rgb(BAND_1), "secondBandColor": _rgb(BAND_2),
            },
        }}},
        # Кнопки фильтра и сортировки в шапке
        {"setBasicFilter": {"filter": {"range": {
            "sheetId": sheet_id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": n_cols,
        }}}},
    ]
    for i, width in enumerate(style.widths):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": width}, "fields": "pixelSize",
        }})
    for col in style.center:
        reqs.append({"repeatCell": {
            "range": _cols(sheet_id, col),
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
            "fields": "userEnteredFormat.horizontalAlignment",
        }})
    for col in style.money:
        reqs.append({"repeatCell": {
            "range": _cols(sheet_id, col),
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "RIGHT",
                "numberFormat": {"type": "NUMBER", "pattern": f'#,##0 "{currency}"'},
            }},
            "fields": "userEnteredFormat(horizontalAlignment,numberFormat)",
        }})
    for col, rules in style.highlight.items():
        for text, (bg, fg) in rules.items():
            reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
                "ranges": [_cols(sheet_id, col)],
                "booleanRule": {
                    "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": text}]},
                    "format": {"backgroundColor": _rgb(bg),
                               "textFormat": {"bold": True, "foregroundColor": _rgb(fg)}},
                },
            }}})
    for col, options in style.dropdowns.items():
        reqs.append({"setDataValidation": {
            "range": _cols(sheet_id, col),
            "rule": {
                "condition": {"type": "ONE_OF_LIST",
                              "values": [{"userEnteredValue": o} for o in options]},
                "showCustomUi": True, "strict": False,
            },
        }})
    return reqs
