"""Форматирование сумм, дат и длительностей — общее для бота, таблицы и дашборда."""
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo


def money(value: Decimal, currency: str = "₸") -> str:
    text = f"{value:,.2f}".replace(",", " ")
    if text.endswith(".00"):
        text = text[:-3]
    return f"{text.replace('.', ',')} {currency}"


def local_dt(dt: datetime | None, tz: ZoneInfo) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC).astimezone(tz)


def fmt_dt(dt: datetime | None, tz: ZoneInfo, with_year: bool = True) -> str:
    local = local_dt(dt, tz)
    if local is None:
        return ""
    return local.strftime("%d.%m.%Y %H:%M" if with_year else "%d.%m %H:%M")


def fmt_hours(hours: float | None) -> str:
    """3.5 → «3 ч 30 мин», 50 → «2 дн 2 ч»."""
    if hours is None:
        return "—"
    minutes = round(hours * 60)
    if minutes < 60:
        return f"{minutes} мин"
    h, m = divmod(minutes, 60)
    if h < 48:
        return f"{h} ч {m} мин" if m and h < 10 else f"{h} ч"
    d, h = divmod(h, 24)
    return f"{d} дн {h} ч" if h else f"{d} дн"
