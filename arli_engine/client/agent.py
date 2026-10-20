"""Тонкая обёртка над поверхностью агента для внешних клиентов.

Мы держим один слой и много обёрток. Слой это перечень методов в `core/surface.py`,
а обёртки это способы дотянуться до него: A2A по HTTP, MCP по потокам, плагин. Этот файл
четвёртая обёртка, для того, кто пишет на Python и не хочет собирать запросы руками.

Обёртка не описывает методы заново, а разворачивает тот же перечень: по методу на каждый
метод поверхности. Если метод появится в перечне, он появится и здесь, а если исчезнет,
здесь его не останется. Тест сверяет это.

Отказы не съедаются. Причина, которую движок вернул кодом, остаётся кодом и на стороне
клиента: `AgentRefused.code`. Клиент, который показывает «ошибка 500» вместо причины,
заставляет людей заново выяснять то, что мы уже знали.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx

from arli_engine.core.intent import INTENT_REJECTIONS
from arli_engine.core.match import MATCH_REJECTIONS
from arli_engine.core.negotiate import OPENING_REJECTIONS, REJECTIONS
from arli_engine.core.registry import REGISTRY_REJECTIONS
from arli_engine.core.retention import RETENTION_REJECTIONS
from arli_engine.core.surface import METHOD_NAMES

CARD_PATH = "/.well-known/agent-card.json"
CAPS_PATH = "/.well-known/arli-capabilities.json"
RPC_PATH = "/a2a/rpc"

# Все коды отказов движка в одном месте: по ним причина достаётся из текста ошибки.
KNOWN_REJECTION_CODES: tuple[str, ...] = (
    tuple(REJECTIONS)
    + tuple(OPENING_REJECTIONS)
    + tuple(MATCH_REJECTIONS)
    + tuple(RETENTION_REJECTIONS)
    + tuple(REGISTRY_REJECTIONS)
    + tuple(INTENT_REJECTIONS)
)


class AgentClientError(RuntimeError):
    """Поверхность ответила так, что разобрать ответ нельзя."""


class AgentRefused(RuntimeError):
    """Агент отказал по причине из закрытого перечня."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if code != "unlisted" else message)


def rejection_code(message: str) -> str:
    """Достать код отказа из текста. Нет кода — вернуть «unlisted», а не выдумывать."""
    for code in KNOWN_REJECTION_CODES:
        if code in message:
            return code
    return "unlisted"


class AgentClient:
    """Клиент одного агента. Методы поверхности разворачиваются из общего перечня."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, transport=transport)

    # --- служебное ---
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AgentClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_card(self) -> dict:
        response = self._client.get(f"{self.base_url}{CARD_PATH}")
        response.raise_for_status()
        return response.json()

    def get_capabilities(self) -> dict:
        response = self._client.get(f"{self.base_url}{CAPS_PATH}")
        response.raise_for_status()
        return response.json()

    def call(self, method: str, params: dict | None = None) -> dict:
        """Вызвать метод по имени. Причина отказа приходит как AgentRefused."""
        if method not in METHOD_NAMES:
            raise ValueError(f"unknown method: {method!r}")
        response = self._client.post(
            f"{self.base_url}{RPC_PATH}",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentClientError(f"{method} returned a non-JSON body: {exc}") from exc
        if "error" in payload:
            message = str(payload["error"].get("message", payload["error"]))
            raise AgentRefused(rejection_code(message), message)
        if "result" not in payload:
            raise AgentClientError(f"{method} returned neither result nor error: {payload}")
        return payload["result"]


def _bind(method: str) -> Callable[..., dict]:
    def call(self: AgentClient, params: dict | None = None) -> dict:
        return self.call(method, params)

    call.__name__ = method.replace("/", "_")
    call.__qualname__ = f"AgentClient.{call.__name__}"
    call.__doc__ = f"Вызвать {method}. Аргументы передаются движку без правок."
    return call


# По методу на каждый метод поверхности. Расхождение ловит тест.
def _install_bindings() -> None:
    for method in METHOD_NAMES:
        setattr(AgentClient, method.replace("/", "_"), _bind(method))


_install_bindings()


def bound_method_names() -> list[str]:
    """Имена, под которыми методы доступны в клиенте, в порядке перечня."""
    return [method.replace("/", "_") for method in METHOD_NAMES]


def as_params(**kwargs: Any) -> dict:
    """Мелкая удобность: собрать параметры из именованных аргументов."""
    return dict(kwargs)
