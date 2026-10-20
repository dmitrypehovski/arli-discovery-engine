"""Живой прогон стенда: два агента, подписанные карточки, локальный матчинг,
ограниченные переговоры и отдельное согласие человека.

Запуск (из корня репозитория):
    .venv/bin/python examples/two_agents/main.py

Пишет конверты доказательств в out/ и печатает отчёт по шагам.
"""

from __future__ import annotations

import base64
import json
import secrets
import sys
from pathlib import Path

import httpx

import sys as _sys
from pathlib import Path as _Path

# Пример запускается из корня репозитория, поэтому корень поднимаем в путь сами.
_ROOT = _Path(__file__).resolve().parents[2]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

from arli_engine import (
    EXTENSION_URI,
    FeatureSet,
    KeyPair,
    NegotiationError,
    Operation,
    build_card,
    verify_card,
)
from arli_engine.core.capability import EFFECT_KEYS
from arli_engine.core.crypto import verify_envelope
from arli_engine.core.psi import feature_hash
from arli_engine.adapters.a2a.server import CAPS_PATH, CARD_PATH, RPC_PATH, AgentState, start_agent

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

# Сеть, в которой ищется совпадение: годность пары проверяется внутри одной сети.
NETWORK = "network-eu-design"

# Порог объявляет вызывающий: ниже него находка не возвращается вовсе.
# 4000 это сорок процентов общих признаков.
FLOOR = 4000

STEPS: list[str] = []


def step(text: str) -> None:
    STEPS.append(text)
    print(text, flush=True)


def rpc(port: int, method: str, params: dict | None = None) -> dict:
    response = httpx.post(
        f"http://127.0.0.1:{port}{RPC_PATH}",
        json={"jsonrpc": "2.0", "id": method, "method": method, "params": params or {}},
        timeout=10,
    )
    payload = response.json()
    if "error" in payload:
        raise NegotiationError(payload["error"]["message"])
    if "result" not in payload:
        # Ответ без результата и без ошибки это дефект поверхности, а не тихая пустота.
        raise RuntimeError(f"{method} returned neither result nor error: {payload}")
    return payload["result"]


def rpc_error(port: int, method: str, params: dict | None = None) -> str:
    response = httpx.post(
        f"http://127.0.0.1:{port}{RPC_PATH}",
        json={"jsonrpc": "2.0", "id": method, "method": method, "params": params or {}},
        timeout=10,
    )
    payload = response.json()
    return payload.get("error", {}).get("message", "") if "error" in payload else ""


def operations(name: str) -> list[Operation]:
    """Одна операция доступна, вторая честно недоступна с причиной."""
    return [
        Operation(
            name="discover.match",
            description="Compare intent features with a counterpart using salted commitments",
            effects={
                "mutates_artifact": False,
                "launches_process": False,
                "may_show_ui": False,
                "may_access_network": True,
                "may_write_filesystem": True,
                "changes_permissions": False,
                "requires_root": False,
            },
            limitations=(
                "Counterparty can test guesses about coarse features.",
                "Returns a signal, not a proof of fit.",
            ),
        ),
        Operation(
            name="directory.impersonate",
            description="Act under another principal's identity",
            effects={"may_access_network": True, "changes_permissions": True},
            availability_code="disabled_by_policy",
            availability_reason="Attribution invariant: an agent acts only for its own principal.",
        ),
    ]


def main() -> int:
    key_a, key_b = KeyPair.generate(), KeyPair.generate()
    port_a, port_b = 8801, 8802

    features_a = FeatureSet(["role:founder", "skill:design", "city:sf", "need:infra partner"])
    features_b = FeatureSet(["skill:design", "city:sf", "offer:infra", "role:investor"])

    card_a = build_card(
        name="arli-agent-a",
        description="ARLI prototype agent A: verified discovery over A2A",
        base_url=f"http://127.0.0.1:{port_a}",
        skills=[
            {
                "id": "discover.match",
                "name": "Salted commitment matching",
                "description": "Find overlap between intent features without a third party",
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
                "examples": ["role:founder + skill:design"],
            }
        ],
        operations=operations("a"),
        public_features=list(features_a),
        kid="arli-a-key-1",
        private_pem=key_a.private_pem,
    )
    card_b = build_card(
        name="arli-agent-b",
        description="ARLI prototype agent B: verified discovery over A2A",
        base_url=f"http://127.0.0.1:{port_b}",
        skills=[
            {
                "id": "discover.match",
                "name": "Salted commitment matching",
                "description": "Find overlap between intent features without a third party",
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
                "examples": ["skill:design + city:sf"],
            }
        ],
        operations=operations("b"),
        public_features=list(features_b),
        kid="arli-b-key-1",
        private_pem=key_b.private_pem,
    )

    # Журнал переговоров общий: по нашему замыслу протокол и есть сервер этого журнала.
    shared_negotiations: dict = {}
    shared_opportunities: dict = {}
    state_a = AgentState("arli-agent-a", card_a, operations("a"), features_a, "eu-central",
                         negotiations=shared_negotiations, opportunities=shared_opportunities,
                         private_pem=key_a.private_pem, kid="arli-a-key-1",
                         peer_keys={"arli-agent-b": key_b.public_pem})
    state_b = AgentState("arli-agent-b", card_b, operations("b"), features_b, "eu-central",
                         negotiations=shared_negotiations, opportunities=shared_opportunities,
                         private_pem=key_b.private_pem, kid="arli-b-key-1",
                         peer_keys={"arli-agent-a": key_a.public_pem})
    server_a, _ = start_agent(state_a, port_a)
    server_b, _ = start_agent(state_b, port_b)

    evidence: dict[str, dict] = {}
    try:
        # 1. Карточка по стандартному адресу A2A и проверка подписи.
        fetched_b = httpx.get(f"http://127.0.0.1:{port_b}{CARD_PATH}", timeout=10).json()
        trusted = {"arli-b-key-1": key_b.public_pem}
        kid = fetched_b["signatures"][0]["protected"]
        header = json.loads(base64.urlsafe_b64decode(kid + "=" * (-len(kid) % 4)))
        ok, reasons = verify_card(fetched_b, trusted[header["kid"]])
        step(f"1. Карточка агента B получена по {CARD_PATH}; подпись: {'верна' if ok else 'НЕВЕРНА'} {reasons or ''}")
        step(f"   расширение: {EXTENSION_URI}")

        # 2. Подмена карточки должна ломать подпись.
        tampered = dict(fetched_b)
        tampered["description"] = "ARLI prototype agent B: trusted by everyone"
        ok_t, reasons_t = verify_card(tampered, key_b.public_pem)
        step(f"2. Подмена описания в карточке: подпись {'прошла (ПЛОХО)' if ok_t else 'отклонена'} {reasons_t}")

        # 3. Возможности: доступное и недоступное с причиной, необъявленное считается возможным.
        caps = httpx.get(f"http://127.0.0.1:{port_b}{CAPS_PATH}", timeout=10).json()["manifest"]
        first = caps["operations"][0]
        unavailable = [o for o in caps["operations"] if not o["available"]]
        step(f"3. Возможностей: {caps['operation_count']}; недоступных: {len(unavailable)} с причиной: {unavailable[0]['availability_reason']}")
        step(f"   у доступной операции эффектов объявлено {len(first['declared_effect_keys'])} из {len(EFFECT_KEYS)}, остальные трактуются как возможные")

        # 4. Закрытое пересечение: держатель публикует открытый ключ, заявитель
        # ослепляет свои признаки, держатель подписывает, заявитель снимает
        # ослепление и получает только совпадения по своим признакам.
        session_id = secrets.token_hex(6)
        holder_keys = rpc(port_b, "arli/psi/keys", {"session_id": session_id})
        step(f"4. Закрытое пересечение, схема {holder_keys['scheme']}, потолок признаков {holder_keys['feature_cap']}")
        commit = rpc(port_a, "arli/psi/commit", {
            "session_id": session_id,
            "peer_name": "arli-agent-b",
            "n": holder_keys["n"],
            "e": holder_keys["e"],
        })
        step(f"   заявитель отправил ослеплённые значения: {len(commit['blinded'])}, держатель видит только их количество")
        signed = rpc(port_b, "arli/psi/sign", {
            "session_id": session_id,
            "blinded": commit["blinded"],
        })
        match_a = rpc(port_a, "arli/psi/finish", {
            "session_id": session_id,
            "n": holder_keys["n"],
            "signed": signed["signed"],
            "holder_tokens": signed["holder_tokens"],
            "floor_basis_points": FLOOR,
        })
        match_b = rpc(port_b, "arli/psi/record", {
            "session_id": session_id,
            "matched": match_a["matched_features"],
            "floor_basis_points": FLOOR,
        })
        evidence["match_a"] = match_a["evidence"]
        evidence["match_b"] = match_b["evidence"]
        step(f"   порог объявлен вызывающим: {FLOOR} базисных пунктов, пройден: {match_a['evidence']['parameters']['passed_floor']}")
        step(f"   совпало у A: {match_a['matched_features']} (доля {match_a['overlap_ratio']}, базисных пунктов {match_a['overlap_basis_points']})")
        step(f"   совпало у B: {match_b['matched_features']} (доля {match_b['overlap_ratio']})")
        unmatched_a = sorted(set(map(str.lower, features_a)) - set(match_a["matched_features"]))
        step(f"   чего B о A не узнал: {len(unmatched_a)} несовпавших признаков A остались внутри A")
        step(f"   конверт матча: {match_a['evidence']['evidence_id'][:24]}… схема {match_a['evidence']['parameters']['scheme']}")
        # Подпись конверта: без неё доказательство неотличимо от подделки.
        sig_ok, sig_errors = verify_envelope(match_a["evidence"], key_a.public_pem)
        tampered = dict(match_a["evidence"])
        tampered["normalized_result"] = dict(tampered["normalized_result"], matched_feature_count=99)
        sig_bad, _ = verify_envelope(tampered, key_a.public_pem)
        step(f"   подпись конверта ключом A: верна {sig_ok}; после подмены тела: {sig_bad} (ожидается False)")
        if not sig_ok or sig_bad:
            raise RuntimeError(f"envelope signature check failed: {sig_errors}")
        step(f"   в конверте нет числа чужих признаков: {'counterpart_feature_count' not in match_a['evidence']['parameters']}")
        # Офлайн-перебор закрыт: подписи держателя не сопоставляются с догадками без его ключа.
        tokens = {int(v, 16) for v in signed["holder_tokens"]}
        n_holder, e_holder = int(holder_keys["n"], 16), int(holder_keys["e"], 16)
        guessable = [
            g for g in ("skill:design", "city:sf", "role:investor", "city:berlin")
            if pow(feature_hash(g), e_holder, n_holder) in tokens
        ]
        step(f"   офлайн-перебор догадок по полученным подписям: найдено {len(guessable)} из 4 (ожидается 0)")
        if guessable:
            raise RuntimeError(f"closed intersection leaked: {guessable}")

        # 4б. Единица работы это закрытое намерение, а не знакомство: спрос
        # сопоставляется с предложением, два предложения между собой нет,
        # и синтетическое намерение не входит в человеческую сеть.
        unit_id = "unit-" + secrets.token_hex(3)
        def _open(standings, **extra):
            params = {"negotiation_id": unit_id, "peers": ["arli-agent-a", "arli-agent-b"],
                      "network": NETWORK, "intents": standings}
            params.update(extra)
            return rpc(port_a, "arli/negotiation/open", params)

        supply_demand = _open([
            {"intent_id": "i-supply", "owner": "arli-agent-a", "network": NETWORK,
             "type": "supply", "statement": "свободная линия на две недели"},
            {"intent_id": "i-demand", "owner": "arli-agent-b", "network": NETWORK,
             "type": "demand", "statement": "нужна линия под заказ"},
        ])
        two_supplies = _open([
            {"intent_id": "i-supply", "owner": "arli-agent-a", "network": NETWORK, "type": "supply"},
            {"intent_id": "i-supply-2", "owner": "arli-agent-b", "network": NETWORK, "type": "supply"},
        ])
        synthetic = _open([
            {"intent_id": "i-supply", "owner": "arli-agent-a", "network": NETWORK,
             "type": "person", "provenance": "synthetic"},
            {"intent_id": "i-demand", "owner": "arli-agent-b", "network": NETWORK, "type": "person"},
        ])
        step(f"4б. Намерения: спрос с предложением открыт {supply_demand['opened']}; "
             f"два предложения {two_supplies['opened']} с причиной «{two_supplies.get('rejection')}»; "
             f"синтетическое в человеческой сети {synthetic['opened']} с причиной «{synthetic.get('rejection')}»")
        if not supply_demand["opened"] or two_supplies.get("rejection") != "types_not_comparable" \
                or synthetic.get("rejection") != "synthetic_not_admitted":
            raise RuntimeError("intent rules did not behave as declared")

        # 5. Переговоры по грамматике, по одному ходу на сторону.
        # Годность пары проверяется до открытия: оба намерения активны и приписаны
        # одной сети. Это правило взято у Index, но у нас отказ не молчит.
        neg_id = "neg-" + secrets.token_hex(4)
        standing = [
            {"intent_id": "intent-a", "owner": "arli-agent-a", "network": NETWORK},
            {"intent_id": "intent-b", "owner": "arli-agent-b", "network": NETWORK},
        ]
        open_params = {"negotiation_id": neg_id, "peers": ["arli-agent-a", "arli-agent-b"],
                       "network": NETWORK, "intents": standing}
        opened_a = rpc(port_a, "arli/negotiation/open", open_params)
        opened_b = rpc(port_b, "arli/negotiation/open", open_params)
        step(f"5. Годность пары: открыта {opened_a['opened']} и {opened_b['opened']}, инициатор {opened_a['initiator']}")
        plan = [
            ("arli-agent-a", "propose", "Мы оба на дизайне и в SF. Ищу партнёра по инфраструктуре."),
            ("arli-agent-b", "counter", "Инфраструктуру предлагаю я. Подтвердите, что речь о дизайне и раннем запуске."),
            ("arli-agent-a", "propose", "Да, дизайн и ранний запуск. Короткая совместная работа."),
            ("arli-agent-b", "accept", "Совпадает. Готов знакомить."),
        ]
        for seat, action, message in plan:
            port = port_a if seat.endswith("-a") else port_b
            result = rpc(port, "arli/negotiation/turn", {
                "negotiation_id": neg_id, "seat": seat, "action": action, "message": message,
            })
            evidence[f"turn_{action}_{seat[-1]}"] = result["evidence"]
        state_pos = state_a.negotiations[neg_id].outcome
        state_pos_b = state_b.negotiations[neg_id].outcome
        step(f"5б. Переговоры: исход у A {state_pos}, у B {state_pos_b}, журналы совпадают: {state_a.negotiations[neg_id].to_json()['turns'] == state_b.negotiations[neg_id].to_json()['turns']}")

        # 6. Отказы грамматики, которые обязаны работать.
        wrong_seat = rpc_error(port_b, "arli/negotiation/turn", {
            "negotiation_id": neg_id, "seat": "arli-agent-a", "action": "propose", "message": "",
        })
        neg_id2 = "neg-" + secrets.token_hex(4)
        rpc(port_a, "arli/negotiation/open", {"negotiation_id": neg_id2, "peers": ["arli-agent-a", "arli-agent-b"]})
        rpc(port_b, "arli/negotiation/open", {"negotiation_id": neg_id2, "peers": ["arli-agent-a", "arli-agent-b"]})
        rpc(port_a, "arli/negotiation/turn", {"negotiation_id": neg_id2, "seat": "arli-agent-a", "action": "propose", "message": "x"})
        rpc(port_b, "arli/negotiation/turn", {"negotiation_id": neg_id2, "seat": "arli-agent-b", "action": "counter", "message": "?"})
        accept_after_counter = rpc_error(port_a, "arli/negotiation/turn", {
            "negotiation_id": neg_id2, "seat": "arli-agent-a", "action": "accept", "message": ""})
        step(f"6. Отказ чужого места: «{wrong_seat[:60]}»")
        step(f"   Отказ accept после counter: «{accept_after_counter[:60]}»")

        # 6b. Негодная пара не открывается, и отказ приходит кодом с конвертом.
        inactive_standing = [
            {"intent_id": "intent-a", "owner": "arli-agent-a", "network": NETWORK, "active": False},
            {"intent_id": "intent-b", "owner": "arli-agent-b", "network": NETWORK},
        ]
        refused = rpc(port_a, "arli/negotiation/open", {
            "negotiation_id": "neg-refused-" + secrets.token_hex(2),
            "peers": ["arli-agent-a", "arli-agent-b"],
            "network": NETWORK,
            "intents": inactive_standing,
        })
        evidence["opening_refused"] = refused["evidence"]
        step(f"6b. Негодная пара: открыта {refused['opened']}, причина «{refused['rejection']}», "
             f"конверт {refused['evidence']['evidence_id'][:24]}…")

        # 7. Согласие человека отдельно от согласия агента.
        decide_a = rpc(port_a, "arli/opportunity/decide", {"negotiation_id": neg_id, "actor": "arli-agent-a", "approve": True})
        step(f"7. После согласия агентов состояние: {decide_a['status']} (A подтвердил, B ещё нет)")
        decide_b = rpc(port_b, "arli/opportunity/decide", {"negotiation_id": neg_id, "actor": "arli-agent-b", "approve": True})
        step(f"   После второго подтверждения: {decide_b['status']}")
        twice = rpc_error(port_b, "arli/opportunity/decide", {"negotiation_id": neg_id, "actor": "arli-agent-b", "approve": True})
        step(f"   Повторное решение отклонено: «{twice[:60]}»")

        summary = {
            "steps": STEPS,
            "evidence_ids": {k: v["evidence_id"] for k, v in evidence.items()},
            "cards": {"a": card_a, "b": fetched_b},
            "manifest_b": caps,
            "match": {"a": match_a, "b": match_b},
            "negotiation": state_a.negotiations[neg_id].to_json(),
            "opportunity_status": decide_b["status"],
            "limits": {
                "prototype": True,
                "matching_leak": "a counterparty can test guesses about coarse features",
                "no_third_party": "neither side sends raw features, both match locally",
            },
        }
        (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        for name, env in evidence.items():
            (OUT / f"evidence_{name}.json").write_text(json.dumps(env, ensure_ascii=False, indent=1), encoding="utf-8")
        step(f"8. Записано конвертов: {len(evidence)} в {OUT}/summary.json")
    finally:
        server_a.shutdown()
        server_b.shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(main())
