"""Исправление ошибок администратором: аннулирование и возврат на этап."""
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import Action, Role, Status, User, utcnow
from app.services.approval import AlreadyProcessed, ApprovalService, InvalidInput, NotAllowed
from app.services.staff import apply_staff, parse_staff_rows
from app.services.stats import compute_stats


async def create(service, session, author, amount="15000"):
    out = await service.create(session, author, "purchase", Decimal(amount), "Принтер в офис")
    return out.request


async def test_annul_requires_admin_and_reason(service, session, staff):
    req = await create(service, session, staff["employee"])
    with pytest.raises(NotAllowed):
        await service.annul(session, req.id, staff["manager"], "Ошибка")
    with pytest.raises(InvalidInput):
        await service.annul(session, req.id, staff["admin"], " ")

    out = await service.annul(session, req.id, staff["admin"], "Дубль заявки №5")
    assert req.status == Status.ANNULLED
    assert req.close_reason == "Дубль заявки №5"
    assert [u.username for u in out.previous_assignees] == ["man"]  # руководителю сообщим, что решение не нужно
    assert req.current_assignees() == []
    with pytest.raises(AlreadyProcessed):
        await service.annul(session, req.id, staff["admin"], "Ещё раз")


async def test_return_done_request_to_stage(service, session, staff):
    """Бухгалтер по ошибке отметил «исполнено» — администратор возвращает заявку бухгалтеру."""
    req = await create(service, session, staff["employee"])
    await service.approve(session, req.id, req.stage_index, staff["manager"])
    await service.approve(session, req.id, req.stage_index, staff["accountant"])
    assert req.status == Status.DONE

    out = await service.return_to_stage(session, req.id, "accountant", staff["admin"], "Оплата не прошла")
    assert req.status == Status.IN_PROGRESS
    assert req.stage_code == "accountant"
    assert req.closed_at is None
    assert [u.username for u in out.new_assignees] == ["acc"]
    assert [u.username for u in req.current_assignees()] == ["acc"]  # без дублей после повторного входа
    assert any(e.action == Action.RETURNED and e.comment == "Оплата не прошла" for e in req.events)

    # Дальше заявка идёт по маршруту как обычно
    await service.approve(session, req.id, req.stage_index, staff["accountant"])
    assert req.status == Status.DONE


async def test_return_rejected_request(service, session, staff):
    req = await create(service, session, staff["employee"])
    await service.reject(session, req.id, req.stage_index, staff["manager"], "Нет бюджета")
    await service.return_to_stage(session, req.id, "manager", staff["admin"], "Бюджет нашли")
    assert req.status == Status.IN_PROGRESS
    assert req.reject_reason is None
    assert req.close_reason is None


async def test_return_stage_rules(service, session, staff):
    req = await create(service, session, staff["employee"])  # 15 000 — директор не нужен
    codes = [s.code for s in service.returnable_stages(req)]
    assert codes == ["accountant"]  # не текущий этап и не директор (сумма ниже порога)
    with pytest.raises(InvalidInput):
        await service.return_to_stage(session, req.id, "director", staff["admin"], "Проверить")
    with pytest.raises(InvalidInput):
        await service.return_to_stage(session, req.id, "manager", staff["admin"], "Проверить")


async def test_admin_from_settings(wf, session, staff):
    """Администратора можно назначить по нику или ID в настройках, без роли в списке."""
    svc = ApprovalService(wf, admins=frozenset({"man2"}))
    req = await create(svc, session, staff["employee"])
    assert svc.is_admin(staff["other_manager"])
    await svc.annul(session, req.id, staff["other_manager"], "Тест")
    assert req.status == Status.ANNULLED


async def test_open_access_own_requests_only(wf, session, staff):
    """Посетитель демо-бота исправляет только свои заявки."""
    svc = ApprovalService(wf, open_access=True)
    own = await create(svc, session, staff["employee"])
    foreign = await create(svc, session, staff["accountant"])
    await svc.annul(session, own.id, staff["employee"], "Ошибся")
    with pytest.raises(NotAllowed):
        await svc.annul(session, foreign.id, staff["employee"], "Чужая")


async def test_annulled_excluded_from_stats(service, session, staff, wf):
    t0 = utcnow() - timedelta(hours=5)
    ok = (await service.create(session, staff["employee"], "purchase", Decimal(1000), "Бумага", now=t0)).request
    bad = await create(service, session, staff["employee"])
    await service.annul(session, bad.id, staff["admin"], "Ошибка")
    await session.flush()
    s = await compute_stats(session, wf, ZoneInfo("Asia/Almaty"), days=30)
    assert s.total == 1
    assert s.open_now == 1 and s.stuck[0].id == ok.id


async def test_admin_role_from_staff_list(session):
    records, errors = parse_staff_rows([{"Telegram": "@boss", "ФИО": "Админ", "Отдел": "", "Роль": "администратор"}])
    assert not errors
    await apply_staff(session, records)
    user = (await session.execute(select(User).where(User.username == "boss"))).scalar_one()
    assert user.role == Role.ADMIN
