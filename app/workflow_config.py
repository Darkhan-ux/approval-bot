"""Загрузка настроек процесса (config/workflow.yaml)."""
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

APPROVE = "approve"
EXECUTE = "execute"


@dataclass(frozen=True)
class RequestType:
    code: str
    title: str


@dataclass(frozen=True)
class Stage:
    code: str
    title: str
    kind: str  # approve | execute
    assignee: str
    fallback: str | None = None
    min_amount: Decimal | None = None
    done_label: str | None = None

    @property
    def ok_label(self) -> str:
        if self.done_label:
            return self.done_label
        return "✅ Одобрить" if self.kind == APPROVE else "✅ Исполнено"


@dataclass(frozen=True)
class WorkflowConfig:
    request_types: list[RequestType]
    stages: list[Stage]
    currency: str = "₸"
    reminder_after_hours: float = 24
    reminder_check_minutes: int = 15
    staff_sync_minutes: int = 10

    def type_title(self, code: str) -> str:
        for t in self.request_types:
            if t.code == code:
                return t.title
        return code

    def has_type(self, code: str) -> bool:
        return any(t.code == code for t in self.request_types)

    def stage_title(self, code: str | None) -> str:
        for s in self.stages:
            if s.code == code:
                return s.title
        return code or "—"


class WorkflowConfigError(ValueError):
    pass


def _parse_assignee(value: str, field: str, stage_code: str) -> str:
    value = str(value).strip()
    if value == "department_manager" or value.startswith(("role:", "user:")):
        return value
    raise WorkflowConfigError(
        f"Этап «{stage_code}»: непонятное значение {field}: {value!r}. "
        "Допустимо: department_manager, role:<роль>, user:<ник>"
    )


def parse_workflow(data: dict) -> WorkflowConfig:
    types = [RequestType(str(t["code"]), str(t["title"])) for t in data.get("request_types") or []]
    if not types:
        raise WorkflowConfigError("Не задан ни один тип заявки (request_types)")

    stages: list[Stage] = []
    seen: set[str] = set()
    for raw in data.get("stages") or []:
        code = str(raw["code"])
        if code in seen:
            raise WorkflowConfigError(f"Этап «{code}» указан дважды")
        seen.add(code)
        kind = raw.get("kind", APPROVE)
        if kind not in (APPROVE, EXECUTE):
            raise WorkflowConfigError(f"Этап «{code}»: kind должен быть approve или execute")
        min_amount = raw.get("min_amount")
        stages.append(
            Stage(
                code=code,
                title=str(raw.get("title", code)),
                kind=kind,
                assignee=_parse_assignee(raw["assignee"], "assignee", code),
                fallback=_parse_assignee(raw["fallback"], "fallback", code) if raw.get("fallback") else None,
                min_amount=Decimal(str(min_amount)) if min_amount is not None else None,
                done_label=raw.get("done_label"),
            )
        )
    if not stages:
        raise WorkflowConfigError("Маршрут пустой: не задан ни один этап (stages)")

    return WorkflowConfig(
        request_types=types,
        stages=stages,
        currency=str(data.get("currency", "₸")),
        reminder_after_hours=float(data.get("reminder_after_hours", 24)),
        reminder_check_minutes=int(data.get("reminder_check_minutes", 15)),
        staff_sync_minutes=int(data.get("staff_sync_minutes", 10)),
    )


def load_workflow(path: str | Path) -> WorkflowConfig:
    with open(path, encoding="utf-8") as f:
        return parse_workflow(yaml.safe_load(f) or {})
