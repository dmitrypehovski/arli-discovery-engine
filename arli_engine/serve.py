"""Локальные агенты из самого пакета: поднять движок снаружи, не имея нашего дерева.

Зачем это в пакете, а не в каталоге `tools`. Ворота фазы 6 звучат так: сторонний
разработчик ставит движок и проходит набор проверок за вечер. Установленный пакет без
способа поднять агента эти ворота не проходит: у человека есть движок и набор проверок,
но нет ни одной реализации, по которой набор можно прогнать. Поэтому стенд едет внутри.

Это не производственная служба и не должно ею притворяться: два агента в одной памяти, ключи
на процесс, признаки для примера. Ограничения перечислены вслух.
"""

from __future__ import annotations

import argparse
import time
from http.server import ThreadingHTTPServer

from arli_engine.adapters.a2a.cards import build_card
from arli_engine.adapters.a2a.server import AgentState, start_agent
from arli_engine.core.capability import EFFECT_KEYS, Operation
from arli_engine.core.match import FeatureSet
from arli_engine.core.signing import KeyPair

DEFAULT_PORTS = (8841, 8842)

# Объявленный порог по умолчанию: сорок процентов совпадения.
FLOOR_DEFAULT = 4000

# Примерные признаки. Они здесь для того, чтобы пересечение было непустым и набор проверок
# проверял схему, а не пустоту. Настоящих данных о людях в этом файле нет.
FEATURES_A = ["role:founder", "skill:design", "city:sf", "need:infra"]
FEATURES_B = ["skill:design", "city:sf", "offer:infra", "role:investor"]

LIMITATIONS = [
    "A local stand for conformance runs, not a service: two agents share one process and one memory.",
    "Keys are generated per process and are not persisted.",
    "The example features are synthetic. No real personal data is used.",
]


def operations() -> list[Operation]:
    every = {key: False for key in EFFECT_KEYS}
    every["may_access_network"] = True
    return [
        Operation(name="discover.match", description="Match intents", effects=every),
        Operation(
            name="act.as.other",
            description="Act for another principal",
            effects=every,
            availability_code="disabled_by_policy",
            availability_reason="An agent acts only for its own principal.",
        ),
    ]


def build_pair(
    port_a: int = 0, port_b: int = 0, *, names: tuple[str, str] = ("arli-agent-a", "arli-agent-b")
) -> tuple[list[AgentState], list[ThreadingHTTPServer]]:
    """Собрать двух агентов и поднять их. Порт 0 означает «любой свободный».

    Возвращает состояния и серверы, поэтому фактический порт берётся у сервера, а не
    угадывается: на нулевом порту угадать его нельзя.
    """
    key_a, key_b = KeyPair.generate(), KeyPair.generate()
    name_a, name_b = names

    def make(name: str, key: KeyPair, port: int, features: list[str]) -> AgentState:
        card = build_card(
            name=name,
            description=f"ARLI engine agent {name}",
            base_url=f"http://127.0.0.1:{port}",
            skills=[{
                "id": "discover.match",
                "name": "Closed intersection",
                "description": "Match intents without an index holder.",
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            }],
            operations=operations(),
            public_features=features,
            kid=f"{name}-key-1",
            private_pem=key.private_pem,
        )
        return AgentState(
            name,
            card,
            operations(),
            FeatureSet(features),
            "eu-central",
            private_pem=key.private_pem,
            kid=f"{name}-key-1",
        )

    state_a = make(name_a, key_a, port_a, FEATURES_A)
    state_b = make(name_b, key_b, port_b, FEATURES_B)
    state_a.peer_keys = {name_b: key_b.public_pem}
    state_b.peer_keys = {name_a: key_a.public_pem}
    server_a, _ = start_agent(state_a, port_a)
    server_b, _ = start_agent(state_b, port_b)
    return [state_a, state_b], [server_a, server_b]


def port_of(server: ThreadingHTTPServer) -> int:
    return int(server.server_address[1])


def serve(port_a: int = DEFAULT_PORTS[0], port_b: int = DEFAULT_PORTS[1]) -> None:
    """Поднять пару и служить до прерывания."""
    _states, servers = build_pair(port_a, port_b)
    real_a, real_b = (port_of(server) for server in servers)
    print(
        f"agents: http://127.0.0.1:{real_a} and http://127.0.0.1:{real_b}\n"
        f"conformance: arli-discovery-conformance --target http://127.0.0.1:{real_a} "
        f"--holder http://127.0.0.1:{real_b}",
        flush=True,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arli-discovery-agent",
        description="Run two local ARLI discovery agents so a conformance suite has something to check.",
    )
    parser.add_argument("--port-a", type=int, default=DEFAULT_PORTS[0], help="port of the first agent")
    parser.add_argument("--port-b", type=int, default=DEFAULT_PORTS[1], help="port of the second agent")
    args = parser.parse_args(argv)
    serve(args.port_a, args.port_b)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
