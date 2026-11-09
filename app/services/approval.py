"""Ядро проекта: маршрут заявки и правила переходов между статусами.

Здесь нет ничего про Telegram или Google — только бизнес-логика.
Поэтому её легко тестировать и можно подключить к любому другому интерфейсу.

Жизненный цикл заявки:

    создана ─► этап 1 ─► этап 2 ─► ... ─► ИСПОЛНЕНА
                  │         │
                  └─────────┴──► ОТКЛОНЕНА (с причиной)
    на любом этапе автор может ОТОЗВАТЬ заявку

Исправление ошибок (администратор, причина обязательна):
    любая заявка ──► АННУЛИРОВАНА (ушла из статистики, но осталась в истории)
    любая заявка ──► ВОЗВРАЩЕНА на выбранный этап (например, если одобрили по ошибке)
Заявки не удаляются: история согласования — это аудит.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Action, Assignment, Request, RequestEvent, Role, Status, User, utcnow
from app.workflow_config import APPROVE, Stage, WorkflowConfig

MAX_AMOUNT = Decimal("10000000000")  # 10 млрд — защита от опечаток
CURRENCY_MARKS = ("тенге", "тг", "₸", "руб.", "руб", "р.", "₽")
MIN_REASON_LEN = 3


class WorkflowError(Exception):
    """Ошибка, текст которой можно показать пользователю."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(WorkflowError):
    pass


class AlreadyProcessed(WorkflowError):
    pass


class NotAllowed(WorkflowError):
    pass


class InvalidInput(WorkflowError):
    pass


@dataclass
class Outcome:
    """Что произошло — чтобы бот знал, кого уведомить."""
    request: Request
    action: Action
    actor: User | None
    stage: Stage | None = None  # этап, на котором было действие
    new_assignees: list[User] = field(default_factory=list)  # кому заявка пришла теперь
    previous_assignees: list[User] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return not self.request.is_open


def parse_amount(text: str) -> Decimal:
    """«15 000», «15000,50», «1 200 ₸», «5000 тг» → Decimal."""
    cleaned = "".join(text.lower().split())  # убираем все пробелы, в том числе неразрывные
    for mark in CURRENCY_MARKS:
        cleaned = cleaned.replace(mark, "")
    try:
        value = Decimal(cleaned.replace(",", "."))
    except InvalidOperation:
        raise InvalidInput("Не понял сумму. Напишите число, например: 15000 или 1 250,50") from None
    if not value.is_finite() or value <= 0:
        raise InvalidInput("Сумма должна быть больше нуля")
    if value > MAX_AMOUNT:
        raise InvalidInput("Слишком большая сумма — проверьте, нет ли опечатки")
    return value.quantize(Decimal("0.01"))


def _check_reason(reason: str | None, what: str) -> str:
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON_LEN:
        raise InvalidInput(f"Причина {what} обязательна — напишите хотя бы пару слов")
    return reason


class ApprovalService:
    def __init__(self, workflow: WorkflowConfig, open_access: bool = False,
                 admins: frozenset[str] = frozenset()):
        self.wf = workflow
        self.open_access = open_access
        self.admins = admins  # Telegram ID и ники из настроек

    # ---------- создание ----------

    async def create(
        self,
        session: AsyncSession,
        author: User,
        type_code: str,
        amount: Decimal,
        description: str,
        attachment_file_id: str | None = None,
        attachment_kind: str | None = None,
        now: datetime | None = None,
    ) -> Outcome:
        now = now or utcnow()
        if not self.wf.has_type(type_code):
            raise InvalidInput("Неизвестный тип заявки")
        if amount <= 0 or amount > MAX_AMOUNT:
            raise InvalidInput("Некорректная сумма")
        description = (description or "").strip()
        if len(description) < 3:
            raise InvalidInput("Опишите заявку хотя бы парой слов")

        req = Request(
            author=author,
            type_code=type_code,
            amount_kop=int(amount * 100),
            description=description,
            attachment_file_id=attachment_file_id,
            attachment_kind=attachment_kind,
            status=Status.IN_PROGRESS,
            created_at=now,
            updated_at=now,
            events=[],
            assignments=[],
        )
        session.add(req)
        self._log(req, Action.CREATED, actor=author, now=now)
        assignees = await self._advance(session, req, start=0, now=now)
        await session.flush()
        return Outcome(req, Action.CREATED, author, new_assignees=assignees)

    # ---------- решения ----------

    async def get(self, session: AsyncSession, request_id: int) -> Request:
        req = await session.get(Request, request_id)
        if req is None:
            raise NotFound("Заявка не найдена")
        return req

    def can_act(self, req: Request, user: User) -> bool:
        if not req.is_open:
            return False
        if self.open_access:
            return True
        return any(u.id == user.id for u in req.current_assignees())

    async def check_can_act(self, session: AsyncSession, request_id: int, stage_index: int, user: User) -> Request:
        req = await self.get(session, request_id)
        if not req.is_open or req.stage_index != stage_index:
            raise AlreadyProcessed(f"Заявка №{req.id} уже обработана")
        if not self.can_act(req, user):
            raise NotAllowed("Решение по этой заявке принимаете не вы")
        return req

    async def approve(
        self, session: AsyncSession, request_id: int, stage_index: int, actor: User,
        now: datetime | None = None,
    ) -> Outcome:
        now = now or utcnow()
        req = await self.check_can_act(session, request_id, stage_index, actor)
        stage = self.wf.stages[stage_index]
        previous = req.current_assignees()
        action = Action.APPROVED if stage.kind == APPROVE else Action.EXECUTED
        self._log(req, action, actor=actor, now=now, stage_index=stage_index)
        assignees = await self._advance(session, req, start=stage_index + 1, now=now)
        await session.flush()
        return Outcome(req, action, actor, stage=stage, new_assignees=assignees,
                       previous_assignees=previous)

    async def reject(
        self, session: AsyncSession, request_id: int, stage_index: int, actor: User, reason: str,
        now: datetime | None = None,
    ) -> Outcome:
        now = now or utcnow()
        reason = _check_reason(reason, "отказа")
        req = await self.check_can_act(session, request_id, stage_index, actor)
        stage = self.wf.stages[stage_index]
        previous = req.current_assignees()
        self._log(req, Action.REJECTED, actor=actor, now=now, stage_index=stage_index, comment=reason)
        req.status = Status.REJECTED
        req.reject_reason = reason
        req.closed_at = now
        req.stage_index = None  # stage_code оставляем — видно, на каком этапе отказали
        await session.flush()
        return Outcome(req, Action.REJECTED, actor, stage=stage, previous_assignees=previous)

    async def cancel(
        self, session: AsyncSession, request_id: int, actor: User, now: datetime | None = None,
    ) -> Outcome:
        now = now or utcnow()
        req = await self.get(session, request_id)
        if req.author_id != actor.id:
            raise NotAllowed("Отозвать заявку может только её автор")
        if not req.is_open:
            raise AlreadyProcessed(f"Заявка №{req.id} уже закрыта")
        previous = req.current_assignees()
        stage = self.wf.stages[req.stage_index] if req.stage_index is not None else None
        self._log(req, Action.CANCELLED, actor=actor, now=now, stage_index=req.stage_index)
        req.status = Status.CANCELLED
        req.closed_at = now
        req.stage_index = None
        await session.flush()
        return Outcome(req, Action.CANCELLED, actor, stage=stage, previous_assignees=previous)

    # ---------- исправление ошибок (администратор) ----------

    def is_admin(self, user: User) -> bool:
        if user.role == Role.ADMIN:
            return True
        return str(user.tg_id) in self.admins or (user.username or "").lower() in self.admins

    def can_admin(self, req: Request, user: User) -> bool:
        """Администратор — любую заявку; в режиме открытого доступа — каждый свою (чтобы не трогали чужие)."""
        return self.is_admin(user) or (self.open_access and req.author_id == user.id)

    def returnable_stages(self, req: Request) -> list[Stage]:
        """Этапы, на которые можно вернуть заявку: нужные по сумме и не тот, на котором она сейчас."""
        return [
            stage for i, stage in enumerate(self.wf.stages)
            if (stage.min_amount is None or req.amount >= stage.min_amount)
            and not (req.is_open and req.stage_index == i)
        ]

    async def _get_for_admin(self, session: AsyncSession, request_id: int, actor: User) -> Request:
        req = await self.get(session, request_id)
        if not self.can_admin(req, actor):
            raise NotAllowed("Это может сделать только администратор")
        if req.status == Status.ANNULLED:
            raise AlreadyProcessed(f"Заявка №{req.id} уже аннулирована")
        return req

    async def annul(
        self, session: AsyncSession, request_id: int, actor: User, reason: str,
        now: datetime | None = None,
    ) -> Outcome:
        now = now or utcnow()
        reason = _check_reason(reason, "аннулирования")
        req = await self._get_for_admin(session, request_id, actor)
        previous = req.current_assignees()
        stage = self.wf.stages[req.stage_index] if req.stage_index is not None else None
        self._log(req, Action.ANNULLED, actor=actor, now=now, stage_index=req.stage_index, comment=reason)
        req.status = Status.ANNULLED
        req.closed_at = now
        req.stage_index = None
        await session.flush()
        return Outcome(req, Action.ANNULLED, actor, stage=stage, previous_assignees=previous)

    async def return_to_stage(
        self, session: AsyncSession, request_id: int, stage_code: str, actor: User, reason: str,
        now: datetime | None = None,
    ) -> Outcome:
        """Возвращает заявку на этап — даже исполненную или отклонённую. Ответственный получит её заново."""
        now = now or utcnow()
        reason = _check_reason(reason, "возврата")
        req = await self._get_for_admin(session, request_id, actor)
        index = next((i for i, s in enumerate(self.wf.stages) if s.code == stage_code), None)
        if index is None or self.wf.stages[index] not in self.returnable_stages(req):
            raise InvalidInput("На этот этап заявку вернуть нельзя")
        previous = req.current_assignees()
        self._log(req, Action.RETURNED, actor=actor, now=now, stage_index=index, comment=reason)
        req.status = Status.IN_PROGRESS
        req.closed_at = None
        req.reject_reason = None
        assignees = await self._advance(session, req, start=index, now=now)
        await session.flush()
        return Outcome(req, Action.RETURNED, actor, stage=self.wf.stages[index],
                       new_assignees=assignees, previous_assignees=previous)

    # ---------- напоминания ----------

    async def overdue(self, session: AsyncSession, now: datetime | None = None) -> list[Request]:
        """Заявки, которые висят на этапе дольше N часов и о которых давно не напоминали."""
        now = now or utcnow()
        limit = now - timedelta(hours=self.wf.reminder_after_hours)
        result = await session.execute(
            select(Request).where(
                Request.status == Status.IN_PROGRESS,
                Request.stage_entered_at <= limit,
                or_(Request.last_reminded_at.is_(None), Request.last_reminded_at <= limit),
            ).order_by(Request.stage_entered_at)
        )
        return list(result.scalars())

    def mark_reminded(self, req: Request, now: datetime | None = None) -> None:
        now = now or utcnow()
        waited_h = (now - req.stage_entered_at).total_seconds() / 3600
        req.last_reminded_at = now
        self._log(req, Action.REMINDED, actor=None, now=now, stage_index=req.stage_index,
                  comment=f"Без решения {waited_h:.0f} ч")

    # ---------- выборки для бота ----------

    async def inbox(self, session: AsyncSession, user: User, limit: int = 20) -> list[Request]:
        """Заявки, которые сейчас ждут решения этого человека."""
        result = await session.execute(
            select(Request)
            .join(Assignment, and_(Assignment.request_id == Request.id,
                                   Assignment.stage_index == Request.stage_index))
            .where(Request.status == Status.IN_PROGRESS, Assignment.user_id == user.id)
            .order_by(Request.stage_entered_at)
            .limit(limit)
        )
        return list(result.scalars().unique())

    async def by_author(self, session: AsyncSession, user: User, limit: int = 10) -> list[Request]:
        result = await session.execute(
            select(Request).where(Request.author_id == user.id)
            .order_by(Request.id.desc()).limit(limit)
        )
        return list(result.scalars())

    def can_view(self, req: Request, user: User) -> bool:
        if self.open_access or req.author_id == user.id or user.role == Role.DIRECTOR or self.is_admin(user):
            return True
        return any(a.user_id == user.id for a in req.assignments)

    # ---------- внутреннее ----------

    async def _advance(self, session: AsyncSession, req: Request, start: int, now: datetime) -> list[User]:
        """Переводит заявку на следующий подходящий этап. Возвращает новых ответственных."""
        for index in range(start, len(self.wf.stages)):
            stage = self.wf.stages[index]
            if stage.min_amount is not None and req.amount < stage.min_amount:
                self._log(req, Action.SKIPPED, actor=None, now=now, stage_index=index,
                          comment=f"Сумма ниже порога {stage.min_amount:,.0f}".replace(",", " "))
                continue
            users = await self._resolve(session, stage.assignee, req)
            if not users and stage.fallback:
                users = await self._resolve(session, stage.fallback, req)
            if not users:
                self._log(req, Action.SKIPPED, actor=None, now=now, stage_index=index,
                          comment="Не найден ответственный")
                continue

            req.stage_index = index
            req.stage_code = stage.code
            req.stage_entered_at = now
            req.last_reminded_at = None
            already = {a.user.id for a in req.assignments if a.stage_index == index}  # при возврате на этап
            for user in users:
                if user.id not in already:
                    req.assignments.append(Assignment(stage_index=index, user=user))
            self._log(req, Action.ENTERED, actor=None, now=now, stage_index=index)
            return users

        # Все этапы пройдены
        req.status = Status.DONE
        req.stage_index = None
        req.closed_at = now
        return []

    async def _resolve(self, session: AsyncSession, spec: str, req: Request) -> list[User]:
        """Кто отвечает за этап. Автор заявки никогда не согласует сам себя (кроме режима открытого доступа)."""
        author = req.author
        if self.open_access:
            return [author]

        query = select(User).where(User.is_active.is_(True), User.id != author.id)
        if spec == "department_manager":
            if not author.department:
                return []
            query = query.where(User.role == Role.MANAGER, User.department == author.department)
        elif spec.startswith("role:"):
            query = query.where(User.role == spec.removeprefix("role:"))
        elif spec.startswith("user:"):
            query = query.where(User.username == spec.removeprefix("user:").lstrip("@").lower())
        else:
            return []
        result = await session.execute(query.order_by(User.id))
        return list(result.scalars())

    def _log(
        self, req: Request, action: Action, actor: User | None, now: datetime,
        stage_index: int | None = None, comment: str | None = None,
    ) -> None:
        stage_code = self.wf.stages[stage_index].code if stage_index is not None else None
        req.events.append(RequestEvent(
            action=action, actor=actor, stage_index=stage_index, stage_code=stage_code,
            comment=comment, created_at=now,
        ))
        req.updated_at = now
        req.sheet_dirty = True
