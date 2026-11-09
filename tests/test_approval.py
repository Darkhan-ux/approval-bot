"""Тесты маршрута согласования."""
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.models import Action, Status, utcnow
from app.services.approval import (
    AlreadyProcessed,
    ApprovalService,
    InvalidInput,
    NotAllowed,
    parse_amount,
)
from app.services.staff import apply_staff, find_or_register, parse_staff_rows
from app.services.stats import compute_stats

pytestmark = pytest.mark.asyncio


async def create(service, session, author, amount="15000"):
    return await service.create(session, author, "purchase", Decimal(amount), "Принтер в офис")


async def test_full_route_small_amount(service, session, staff):
    out = await create(service, session, staff["employee"])
    req = out.request
    assert req.status == Status.IN_PROGRESS
    assert req.stage_code == "manager"
    assert [u.username for u in out.new_assignees] == ["man"]  # руководитель своего отдела, не чужого

    out = await service.approve(session, req.id, req.stage_index, staff["manager"])
    # Директор пропущен — сумма меньше порога 500 000
    assert req.stage_code == "accountant"
    assert [u.username for u in out.new_assignees] == ["acc"]
    assert any(e.action == Action.SKIPPED and e.stage_code == "director" for e in req.events)

    out = await service.approve(session, req.id, req.stage_index, staff["accountant"])
    assert req.status == Status.DONE
    assert out.finished
    assert req.closed_at is not None


async def test_big_amount_goes_to_director(service, session, staff):
    req = (await create(service, session, staff["employee"], "750000")).request
    await service.approve(session, req.id, req.stage_index, staff["manager"])
    assert req.stage_code == "director"
    await service.approve(session, req.id, req.stage_index, staff["director"])
    assert req.stage_code == "accountant"


async def test_reject_requires_reason(service, session, staff):
    req = (await create(service, session, staff["employee"])).request
    with pytest.raises(InvalidInput):
        await service.reject(session, req.id, req.stage_index, staff["manager"], "  ")
    out = await service.reject(session, req.id, req.stage_index, staff["manager"], "Нет бюджета")
    assert req.status == Status.REJECTED
    assert req.reject_reason == "Нет бюджета"
    assert req.stage_code == "manager"  # видно, на каком этапе отказали
    assert out.finished


async def test_double_click_is_safe(service, session, staff):
    req = (await create(service, session, staff["employee"])).request
    stage = req.stage_index
    await service.approve(session, req.id, stage, staff["manager"])
    with pytest.raises(AlreadyProcessed):
        await service.approve(session, req.id, stage, staff["manager"])


async def test_stranger_cannot_decide(service, session, staff):
    req = (await create(service, session, staff["employee"])).request
    with pytest.raises(NotAllowed):
        await service.approve(session, req.id, req.stage_index, staff["other_manager"])
    with pytest.raises(NotAllowed):
        await service.approve(session, req.id, req.stage_index, staff["employee"])  # сам себе


async def test_manager_request_goes_to_director(service, session, staff):
    """Руководитель не согласует сам себе — заявка уходит директору (fallback)."""
    out = await create(service, session, staff["manager"])
    assert out.request.stage_code == "manager"
    assert [u.username for u in out.new_assignees] == ["dir"]


async def test_cancel_only_by_author(service, session, staff):
    req = (await create(service, session, staff["employee"])).request
    with pytest.raises(NotAllowed):
        await service.cancel(session, req.id, staff["manager"])
    out = await service.cancel(session, req.id, staff["employee"])
    assert req.status == Status.CANCELLED
    assert [u.username for u in out.previous_assignees] == ["man"]


async def test_inbox(service, session, staff):
    req = (await create(service, session, staff["employee"])).request
    await session.flush()
    assert [r.id for r in await service.inbox(session, staff["manager"])] == [req.id]
    assert await service.inbox(session, staff["accountant"]) == []


async def test_overdue_and_reminder(service, session, staff):
    past = utcnow() - timedelta(hours=30)
    req = (await service.create(session, staff["employee"], "purchase", Decimal(100), "Ручки", now=past)).request
    await session.flush()
    overdue = await service.overdue(session)
    assert [r.id for r in overdue] == [req.id]
    service.mark_reminded(req)
    await session.flush()
    assert await service.overdue(session) == []  # повторно не напоминаем сразу


async def test_open_access_one_person_plays_all_roles(wf, session, staff):
    svc = ApprovalService(wf, open_access=True)
    me = staff["employee"]
    req = (await svc.create(session, me, "payment", Decimal(500000), "Проверка маршрута")).request
    while req.is_open:
        await svc.approve(session, req.id, req.stage_index, me)
    assert req.status == Status.DONE


async def test_stats(service, session, staff, wf):
    t0 = utcnow() - timedelta(hours=10)
    req = (await service.create(session, staff["employee"], "purchase", Decimal(1000), "Бумага", now=t0)).request
    await service.approve(session, req.id, req.stage_index, staff["manager"], now=t0 + timedelta(hours=2))
    await service.approve(session, req.id, req.stage_index, staff["accountant"], now=t0 + timedelta(hours=5))
    await session.flush()
    s = await compute_stats(session, wf, ZoneInfo("Asia/Almaty"), days=30)
    assert s.total == 1 and s.by_status["done"] == 1
    assert s.avg_cycle_h == pytest.approx(5)
    by_code = {st.code: st for st in s.stages}
    assert by_code["manager"].avg_h == pytest.approx(2)
    assert by_code["accountant"].avg_h == pytest.approx(3)
    assert s.bottleneck_stage.code == "accountant"


@pytest.mark.parametrize("text,expected", [
    ("15000", "15000.00"), ("15 000", "15000.00"), ("1 250,50", "1250.50"),
    ("3000 ₽", "3000.00"), ("75 000 ₸", "75000.00"), ("5000 тг", "5000.00"), ("120 000 тенге", "120000.00"),
])
async def test_parse_amount(text, expected):
    assert parse_amount(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["abc", "0", "-5", "", "1e100"])
async def test_parse_amount_invalid(text):
    with pytest.raises(InvalidInput):
        parse_amount(text)


async def test_register(session, staff):
    # Открытый доступ: новый человек сразу получает доступ с первого сообщения
    u = await find_or_register(session, 555, "newbie", "Новичок", open_access=True)
    assert u is not None and u.is_active
    # Рабочий режим: чужим доступа нет, а человек из списка привязывается по нику
    assert await find_or_register(session, 777, "stranger", "Чужой", open_access=False) is None
    emp = await find_or_register(session, 888, "Emp", "Сотрудник", open_access=False)
    assert emp is not None and emp.id == staff["employee"].id and emp.tg_id == 888


async def test_staff_sync(session):
    rows = [
        {"Telegram": "@Ivan", "ФИО": "Ерлан", "Отдел": "Продажи", "Роль": "Руководитель"},
        {"Telegram": "123", "ФИО": "Айгерим", "Отдел": "Бухгалтерия", "Роль": "бухгалтер"},
        {"Telegram": "@bad", "ФИО": "Кто-то", "Отдел": "", "Роль": "космонавт"},
    ]
    records, errors = parse_staff_rows(rows)
    assert len(records) == 2 and len(errors) == 1
    assert records[0].username == "ivan" and records[1].tg_id == 123
    stats = await apply_staff(session, records)
    assert stats["created"] == 2
    # Убрали Анну из списка — она отключается
    stats = await apply_staff(session, records[:1])
    assert stats["deactivated"] == 1
