"""Калибровка измерителя на живых агентах, без людей.

Что это такое и чем не является. Это прогон измерителя по настоящим конвертам, которые выдали
настоящие агенты движка, чтобы к моменту пилота у нас были работающая методика и таблица.
Это НЕ числа пилота: признаки в стенде синтетические, людей здесь нет, и выдавать эти числа
за воронку нельзя. Пилот по плану идёт на согласившихся людях.

Запуск: python -m tools.measure_dry_run
Результат: out/measure_dry_run.json и таблица в выводе.
"""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arli_engine.client import AgentClient, AgentRefused  # noqa: E402
from arli_engine.core.crypto import public_pem_from_private, verify_envelope  # noqa: E402
from arli_engine.measure import measure  # noqa: E402
from arli_engine.serve import FLOOR_DEFAULT, build_pair, port_of  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "out" / "measure_dry_run.json"


def main() -> int:
    states, servers = build_pair(0, 0)
    ports = [port_of(server) for server in servers]
    url_a, url_b = (f"http://127.0.0.1:{port}" for port in ports)
    # Ключи выдающих: проверяем оба ключа по каждому конверту, а не угадываем автора.
    candidate_keys = {state.name: public_pem_from_private(state.private_pem) for state in states}
    public_keys: dict[str, bytes] = {}

    envelopes: list[dict] = []
    refusals: list[dict] = []
    matches: list[dict] = []
    intents: list[dict] = []

    try:
        with AgentClient(url_a) as client_a, AgentClient(url_b) as client_b:
            # 1. Полный обмен закрытого пересечения: только совпадения по своим признакам.
            session_id = secrets.token_hex(6)
            holder_keys = client_b.arli_psi_keys({"session_id": session_id})
            commit = client_a.arli_psi_commit({
                "session_id": session_id, "peer_name": states[1].name,
                "n": holder_keys["n"], "e": holder_keys["e"],
            })
            signed = client_b.arli_psi_sign({"session_id": session_id, "blinded": commit["blinded"]})
            match_a = client_a.arli_psi_finish({
                "session_id": session_id, "n": holder_keys["n"], "signed": signed["signed"],
                "holder_tokens": signed["holder_tokens"], "floor_basis_points": FLOOR_DEFAULT,
            })
            match_b = client_b.arli_psi_record({
                "session_id": session_id, "matched": match_a["matched_features"],
                "floor_basis_points": FLOOR_DEFAULT,
            })
            envelopes.extend([match_a["evidence"], match_b["evidence"]])
            matches.append({
                "passed_floor": bool(match_a["evidence"]["parameters"].get("passed_floor")),
                "matched": len(match_a["matched_features"]),
            })
            for side in ("a", "b"):
                intents.append({"intent_id": f"unit-{side}", "closed": bool(match_a["matched_features"])})

            # 2. Обмен с объявленным, но недостижимым порогом: находка не возвращается,
            # а не добивается до предела.
            tight_session = secrets.token_hex(6)
            tight_keys = client_b.arli_psi_keys({"session_id": tight_session})
            tight_commit = client_a.arli_psi_commit({
                "session_id": tight_session, "peer_name": states[1].name,
                "n": tight_keys["n"], "e": tight_keys["e"],
            })
            tight_signed = client_b.arli_psi_sign({
                "session_id": tight_session, "blinded": tight_commit["blinded"],
            })
            tight_match = client_a.arli_psi_finish({
                "session_id": tight_session, "n": tight_keys["n"], "signed": tight_signed["signed"],
                "holder_tokens": tight_signed["holder_tokens"], "floor_basis_points": 9900,
            })
            envelopes.append(tight_match["evidence"])
            matches.append({
                "passed_floor": bool(tight_match["evidence"]["parameters"].get("passed_floor")),
                "matched": len(tight_match["matched_features"]),
            })

            # 3. Отказы: каждый обязан прийти кодом из закрытого перечня.
            try:
                client_a.arli_registry_register({
                    "kid": "k-dry", "principal_id": "p-dry", "public_pem": "pem-dry",
                })
                client_a.arli_registry_rotate({
                    "kid": "k-dry", "new_kid": "k-dry-2", "new_public_pem": "pem-dry-2",
                    "reason": "because-i-said-so",
                })
                refusals.append({"code": None})
            except AgentRefused as exc:
                refusals.append({"code": exc.code})

            same_owner = client_a.arli_negotiation_open({
                "negotiation_id": "dry-same-owner", "peers": [states[0].name, states[1].name],
                "network": "dry-run",
                "intents": [
                    {"intent_id": "i1", "owner": states[0].name, "network": "dry-run", "type": "person"},
                    {"intent_id": "i2", "owner": states[0].name, "network": "dry-run", "type": "person"},
                ],
            })
            refusals.append({"code": same_owner.get("rejection")})

            synthetic = client_a.arli_negotiation_open({
                "negotiation_id": "dry-synthetic", "peers": [states[0].name, states[1].name],
                "network": "dry-run",
                "intents": [
                    {"intent_id": "i1", "owner": states[0].name, "network": "dry-run",
                     "type": "person", "provenance": "synthetic"},
                    {"intent_id": "i2", "owner": states[1].name, "network": "dry-run", "type": "person"},
                ],
            })
            refusals.append({"code": synthetic.get("rejection")})
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()

    # Ключ для каждого выдающего: тот, которым подпись конверта действительно проверяется.
    for envelope in envelopes:
        issuer = str(envelope.get("provider", {}).get("id"))
        for owner, pem in candidate_keys.items():
            verified, _errors = verify_envelope(envelope, pem)
            if verified:
                public_keys[issuer] = pem
                break

    record = {
        "public_keys": {name: pem.decode() for name, pem in public_keys.items()},
        "envelopes": envelopes,
        "refusals": refusals,
        "intents": intents,
        "matches": matches,
        # Решения, пометки человека и состояние участников здесь отсутствуют намеренно:
        # без людей эти величины неизмерены, и измеритель так и скажет.
    }
    report = measure(record, source="dry-run on synthetic features, not a pilot")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "caveat": (
            "Instrument calibration on synthetic features with no people. "
            "These are not pilot numbers and must not be presented as a funnel."
        ),
        **report.to_json(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("калибровка измерителя, синтетические признаки, людей нет")
    print(report.as_table())
    print(f"записано: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
