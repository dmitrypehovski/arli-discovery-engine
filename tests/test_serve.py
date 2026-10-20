"""Ворота фазы 6: сторонний разработчик ставит пакет и проходит набор проверок.

Здесь проверяется то, что видит человек снаружи: он получает пакет, поднимает агентов
штатной командой пакета и получает ответ «соответствует». Каталог `tools` в пакет не
входит, поэтому агентов обязан уметь поднимать сам пакет. Если это сломается, ворота
фазы 6 закрыты, и тест обязан это показать.
"""

from __future__ import annotations

from arli_engine.conformance import Target, run_conformance
from arli_engine.serve import LIMITATIONS, DEFAULT_PORTS, build_pair, port_of


def test_the_package_can_raise_agents_on_its_own():
    """Порт 0 означает «любой свободный»: фактический порт берём у сервера."""
    states, servers = build_pair(0, 0)
    try:
        ports = [port_of(server) for server in servers]
        assert all(port > 0 for port in ports)
        assert len(states) == 2
        assert states[0].name == "arli-agent-a" and states[1].name == "arli-agent-b"
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_a_stranger_with_only_the_package_gets_a_conforming_answer():
    _states, servers = build_pair(0, 0)
    target = Target(base_url=f"http://127.0.0.1:{port_of(servers[0])}", name="arli-agent-a")
    holder = Target(base_url=f"http://127.0.0.1:{port_of(servers[1])}", name="arli-agent-b")
    try:
        report = run_conformance(target, holder)
    finally:
        target.close()
        holder.close()
        for server in servers:
            server.shutdown()
            server.server_close()
    assert report.failures == [], report.failures
    assert report.compliant
    assert len(report.checks) >= 25


def test_the_stand_declares_its_limits_rather_than_pretending():
    joined = " ".join(LIMITATIONS).lower()
    assert "not a service" in joined
    assert "synthetic" in joined


def test_the_default_ports_are_the_documented_ones():
    assert DEFAULT_PORTS == (8841, 8842)
