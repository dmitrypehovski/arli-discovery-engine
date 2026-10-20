"""Единый перечень методов поверхности: одна точка правды для всех привязок.

Смысл файла в том, чтобы адаптеры не разъезжались. Поверхность движка одна, а привязок
к ней несколько: наш A2A по HTTP и MCP для тех, кто живёт в мире инструментов модели.
Если каждая привязка будет вести свой список, они разойдутся на первой же правке и
разойдутся молча. Поэтому перечень живёт здесь, а привязки обязаны с ним совпадать и
проверяются на совпадение в наборе проверок.

Правило имён: имя инструмента MCP есть имя метода с косой чертой, заменённой на
подчёркивание. Правило обратимо, поэтому соответствие один к одному проверяемо.
"""

from __future__ import annotations

# Методы, которые агент обязан уметь по спецификации. Порядок фиксирован, чтобы
# документ возможностей был побайтово устойчив между прогонами.
METHOD_NAMES = (
    "agent/card",
    "arli/capabilities",
    "arli/commitments",
    "arli/match",
    "arli/psi/keys",
    "arli/psi/commit",
    "arli/psi/sign",
    "arli/psi/finish",
    "arli/psi/record",
    "arli/registry/status",
    "arli/registry/register",
    "arli/registry/rotate",
    "arli/registry/revoke",
    "arli/registry/confirm",
    "arli/registry/check",
    "arli/retention/status",
    "arli/retention/record",
    "arli/retention/archive",
    "arli/retention/delete",
    "arli/retention/purge",
    "arli/claims/status",
    "arli/claims/issuers",
    "arli/claims/declare",
    "arli/negotiation/open",
    "arli/negotiation/turn",
    "arli/opportunity/decide",
)

# Методы, которые меняют состояние агента. Нужны, чтобы описание инструмента MCP было
# честным: вызывающий видит, что действие оставляет след.
MUTATING_METHODS = (
    "arli/psi/commit",
    "arli/psi/sign",
    "arli/psi/finish",
    "arli/psi/record",
    "arli/registry/register",
    "arli/registry/rotate",
    "arli/registry/revoke",
    "arli/registry/confirm",
    "arli/retention/record",
    "arli/retention/archive",
    "arli/retention/delete",
    "arli/retention/purge",
    "arli/claims/issuers",
    "arli/claims/declare",
    "arli/negotiation/open",
    "arli/negotiation/turn",
    "arli/opportunity/decide",
)

# Обязательные поля у методов, где пустой вызов бессмысленен. Перечень неполный
# намеренно: он честно перечисляет то, что действительно обязательно, и не выдумывает
# ограничений там, где движок их не ставит.
REQUIRED_FIELDS = {
    "arli/match": ("commitment",),
    "arli/psi/keys": (),
    "arli/psi/commit": ("features",),
    "arli/psi/sign": ("blinded",),
    "arli/psi/finish": ("signed", "floor_basis_points"),
    "arli/psi/record": ("matched", "floor_basis_points"),
    "arli/registry/register": ("kid", "principal_id", "public_pem"),
    "arli/registry/rotate": ("kid", "new_kid", "new_public_pem"),
    "arli/registry/revoke": ("reason",),
    "arli/registry/confirm": ("principal_id",),
    "arli/registry/check": ("kid",),
    "arli/retention/record": ("evidence_id", "intent_id"),
    "arli/retention/archive": ("intent_id",),
    "arli/retention/delete": ("evidence_id",),
    "arli/claims/issuers": ("name", "public_pem"),
    "arli/claims/declare": ("feature", "level"),
    "arli/negotiation/open": ("negotiation_id", "peers", "intents"),
    "arli/negotiation/turn": ("negotiation_id", "agent", "act"),
    "arli/opportunity/decide": ("negotiation_id", "agent"),
}


def tool_name(method: str) -> str:
    """Имя инструмента MCP по имени метода."""
    return method.replace("/", "_")


# Таблица обратного перевода строится из перечня, а не из правил замены: правило
# замены обратимо не всегда, а таблица обратима по построению.
_TOOL_TO_METHOD = {tool_name(method): method for method in METHOD_NAMES}


def method_for_tool(name: str) -> str:
    """Имя метода по имени инструмента. Обратно к tool_name, точно или отказ."""
    try:
        return _TOOL_TO_METHOD[name]
    except KeyError:
        raise ValueError(f"unknown tool: {name!r}")


def is_mutating(method: str) -> bool:
    return method in MUTATING_METHODS


def required_fields(method: str) -> list[str]:
    return list(REQUIRED_FIELDS.get(method, ()))
