"""Таблицы базы данных."""
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Текущее время в UTC (без tzinfo — так его хранит база)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Role(StrEnum):
    EMPLOYEE = "employee"
    MANAGER = "manager"
    ACCOUNTANT = "accountant"
    DIRECTOR = "director"
    ADMIN = "admin"  # исправляет ошибки: аннулирует заявки и возвращает их на этап


ROLE_TITLES = {
    Role.EMPLOYEE: "Сотрудник",
    Role.MANAGER: "Руководитель",
    Role.ACCOUNTANT: "Бухгалтер",
    Role.DIRECTOR: "Директор",
    Role.ADMIN: "Администратор",
}


class Status(StrEnum):
    IN_PROGRESS = "in_progress"
    DONE = "done"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    ANNULLED = "annulled"  # создана по ошибке; не учитывается в статистике, но остаётся в истории


STATUS_TITLES = {
    Status.IN_PROGRESS: "На согласовании",
    Status.DONE: "Исполнена",
    Status.REJECTED: "Отклонена",
    Status.CANCELLED: "Отозвана",
    Status.ANNULLED: "Аннулирована",
}


class Action(StrEnum):
    CREATED = "created"
    ENTERED = "entered"  # заявка пришла на этап
    APPROVED = "approved"
    EXECUTED = "executed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    REMINDED = "reminded"
    SKIPPED = "skipped"  # этап пропущен (сумма ниже порога / нет ответственного)
    ANNULLED = "annulled"  # администратор аннулировал заявку
    RETURNED = "returned"  # администратор вернул заявку на этап


ACTION_TITLES = {
    Action.CREATED: "Создана",
    Action.ENTERED: "Передана на этап",
    Action.APPROVED: "Одобрена",
    Action.EXECUTED: "Исполнена",
    Action.REJECTED: "Отклонена",
    Action.CANCELLED: "Отозвана автором",
    Action.REMINDED: "Напоминание",
    Action.SKIPPED: "Этап пропущен",
    Action.ANNULLED: "Аннулирована",
    Action.RETURNED: "Возвращена на этап",
}

# Действия, которыми завершается пребывание заявки на этапе
DECISION_ACTIONS = (Action.APPROVED, Action.EXECUTED, Action.REJECTED)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    department: Mapped[str | None] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), default=Role.EMPLOYEE)
    is_active: Mapped[bool] = mapped_column(default=True)
    # staff — из списка сотрудников; self — зарегистрировался сам (открытый доступ)
    source: Mapped[str] = mapped_column(String(10), default="staff")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    @property
    def role_title(self) -> str:
        return ROLE_TITLES.get(self.role, self.role)

    @property
    def display(self) -> str:
        return f"{self.full_name} ({self.department})" if self.department else self.full_name


class Request(Base):
    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    type_code: Mapped[str] = mapped_column(String(50))
    amount_kop: Mapped[int] = mapped_column(BigInteger)  # сумма в копейках — без ошибок округления
    description: Mapped[str] = mapped_column(Text)
    attachment_file_id: Mapped[str | None] = mapped_column(String(255))
    attachment_kind: Mapped[str | None] = mapped_column(String(10))  # photo | document

    status: Mapped[str] = mapped_column(String(20), default=Status.IN_PROGRESS, index=True)
    stage_index: Mapped[int | None] = mapped_column(Integer)
    stage_code: Mapped[str | None] = mapped_column(String(50))
    stage_entered_at: Mapped[datetime | None]
    last_reminded_at: Mapped[datetime | None]
    reject_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    closed_at: Mapped[datetime | None]

    # Заявку нужно (пере)записать в Google Таблицу
    sheet_dirty: Mapped[bool] = mapped_column(default=True)
    # Защита от двойного нажатия: если два человека одновременно меняют заявку,
    # второй получит ошибку, а не перезапишет решение первого.
    version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {"version_id_col": version}

    author: Mapped[User] = relationship(lazy="selectin")
    events: Mapped[list["RequestEvent"]] = relationship(
        back_populates="request", lazy="selectin", order_by="RequestEvent.id",
        cascade="all, delete-orphan",
    )
    assignments: Mapped[list["Assignment"]] = relationship(
        back_populates="request", lazy="selectin", cascade="all, delete-orphan",
    )

    @property
    def amount(self) -> Decimal:
        return Decimal(self.amount_kop) / 100

    @property
    def is_open(self) -> bool:
        return self.status == Status.IN_PROGRESS

    @property
    def close_reason(self) -> str | None:
        """Причина отказа или аннулирования — то, что увидит автор."""
        if self.status == Status.REJECTED:
            return self.reject_reason
        if self.status == Status.ANNULLED:
            return next((e.comment for e in reversed(self.events) if e.action == Action.ANNULLED), None)
        return None

    @property
    def unassigned(self) -> bool:
        """На текущем этапе не нашлось ответственного — решают администраторы (или пока никто)."""
        if not self.is_open:
            return False
        entered = next((e for e in reversed(self.events)
                        if e.action == Action.ENTERED and e.stage_index == self.stage_index), None)
        return bool(entered and entered.comment and entered.comment.startswith("⚠️"))

    def current_assignees(self) -> list[User]:
        if not self.is_open:
            return []
        users = {a.user.id: a.user for a in self.assignments if a.stage_index == self.stage_index}
        return list(users.values())


class Assignment(Base):
    """Кто отвечает за заявку на конкретном этапе."""
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    stage_index: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    request: Mapped[Request] = relationship(back_populates="assignments")
    user: Mapped[User] = relationship(lazy="selectin")


class RequestEvent(Base):
    """Журнал: каждое действие с заявкой."""
    __tablename__ = "request_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    action: Mapped[str] = mapped_column(String(20))
    stage_index: Mapped[int | None] = mapped_column(Integer)
    stage_code: Mapped[str | None] = mapped_column(String(50))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Событие уже записано на лист «История» в Google Таблице
    sheet_synced: Mapped[bool] = mapped_column(default=False)

    request: Mapped[Request] = relationship(back_populates="events")
    actor: Mapped[User | None] = relationship(lazy="selectin")
