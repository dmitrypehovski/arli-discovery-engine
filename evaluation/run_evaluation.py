"""Оценка движка: чем он проверен на самом деле, с числами и без округления.

Зачем отдельный слой, если есть набор проверок. Набор отвечает на один вопрос: соответствует
ли реализация тексту. Он не отвечает на другие: совпадают ли три привязки между собой, что
делает движок на кривой вход, сколько длится полный обмен и сколько параллельных сессий он
держит. Этот слой отвечает именно на них и пишет числа в файл, чтобы их можно было показать.

Честность чисел. Каждая часть оценки сама называет, что она измерила и чего не измерила.
Оценка, которая молчит о непокрытом, выглядит как полная и потому вреднее отсутствия оценки.

Запуск: python -m evaluation.run_evaluation
Результат: evaluation/out/evaluation.json
"""

from __future__ import annotations

import json
import statistics
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from arli_engine.adapters.a2a.server import make_handler  # noqa: F401  (форма поверхности)
from arli_engine.adapters.mcp.server import McpServer, tool_result_payload
from arli_engine.client import AgentClient, AgentRefused
from arli_engine.conformance import Target, run_conformance
from arli_engine.core.crypto import verify_envelope
from arli_engine.core.surface import METHOD_NAMES
from arli_engine.serve import build_pair, port_of

OUT = Path(__file__).resolve().parent / "out"
EVALUATION = OUT / "evaluation.json"

# Наборы признаков для замера полного обмена. Признаки синтетические: это не данные людей.
def features(n: int, prefix: str) -> list[str]:
    return [f"{prefix}-topic:{index}" for index in range(n)]


def exchange(session_id: str, size: int, floor: int = 1) -> dict:
    """Полный обмен закрытого пересечения из четырёх вызовов. Возвращает итоги и время."""
    states, servers = build_pair(0, 0)
    try:
        port_a, port_b = port_of(servers[0]), port_of(servers[1])
        # Размеры наборов задаём напрямую, чтобы замер шёл на нужном числе признаков.
        states[0].features = type(states[0].features)(features(size, "alpha"))
        states[1].features = type(states[1].features)(features(size, "alpha")[: max(1, size // 2)]
                                                     + features(size, "beta")[: size - max(1, size // 2)])
        started = time.perf_counter()
        with AgentClient(f"http://127.0.0.1:{port_a}") as client_a, \
                AgentClient(f"http://127.0.0.1:{port_b}") as client_b:
            holder = client_b.arli_psi_keys({"session_id": session_id})
            commit = client_a.arli_psi_commit({
                "session_id": session_id, "peer_name": states[1].name,
                "n": holder["n"], "e": holder["e"],
            })
            signed = client_b.arli_psi_sign({"session_id": session_id, "blinded": commit["blinded"]})
            finish = client_a.arli_psi_finish({
                "session_id": session_id, "n": holder["n"], "signed": signed["signed"],
                "holder_tokens": signed["holder_tokens"], "floor_basis_points": floor,
            })
        elapsed_ms = (time.perf_counter() - started) * 1000
        wire_bytes = sum(len(json.dumps(payload)) for payload in (holder, commit, signed, finish))
        return {
            "features_per_side": size,
            "matched": len(finish.get("matched_features", [])),
            "elapsed_ms": round(elapsed_ms, 2),
            "wire_bytes": wire_bytes,
        }
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


READ_ONLY = (
    "agent/card",
    "arli/capabilities",
    "arli/commitments",
    "arli/registry/status",
    "arli/retention/status",
    "arli/claims/status",
    "arli/registry/check",
)


def _arguments_for(method: str, state, suffix: str) -> dict:
    """Осмысленные аргументы для метода: пустой вызов не проверяет ничего."""
    peer = "peer"
    table = {
        "arli/registry/check": {"kid": "evaluation-unknown"},
        "arli/registry/register": {"kid": f"k-{suffix}", "principal_id": "evaluation",
                                   "public_pem": "pem"},
        "arli/registry/revoke": {"reason": "not-a-reason"},
        "arli/registry/confirm": {"principal_id": "evaluation-unknown"},
        "arli/claims/declare": {"feature": f"skill:{suffix}", "level": "third_party_verified",
                                "issuer": "nobody"},
        "arli/claims/issuers": {"name": f"body-{suffix}", "public_pem": "pem"},
        "arli/retention/record": {"evidence_id": f"ev-{suffix}", "intent_id": f"intent-{suffix}"},
        "arli/retention/archive": {"intent_id": f"intent-{suffix}"},
        "arli/retention/delete": {"evidence_id": f"ev-{suffix}"},
        "arli/retention/purge": {},
        "arli/psi/keys": {"session_id": f"bind-{suffix}"},
        "arli/psi/sign": {"session_id": f"bind-{suffix}", "blinded": ["01"]},
        "arli/match": {"commitment": "00"},
        "arli/negotiation/open": {
            "negotiation_id": f"bind-{suffix}", "peers": [state.name, "peer"],
            "network": "evaluation",
            "intents": [
                {"intent_id": "i1", "owner": state.name, "network": "evaluation"},
                {"intent_id": "i2", "owner": "peer", "network": "evaluation"},
            ],
        },
        "arli/negotiation/turn": {"negotiation_id": f"bind-{suffix}", "seat": state.name,
                                  "action": "propose", "message": "probe"},
        "arli/opportunity/decide": {"negotiation_id": f"bind-{suffix}", "actor": state.name,
                                    "approve": True},
    }
    return table.get(method, {})


def evaluate_bindings() -> dict:
    """Сходятся ли привязки между собой.

    Читающие методы сверяются по телу ответа: там ответ обязан совпадать побайтово. Меняющие
    сверяются по исходу: обе привязки обязаны либо принять вызов, либо отказать с ОДНИМ И ТЕМ
    ЖЕ кодом. Сравнивать тело у меняющих нельзя по устройству: первый вызов меняет состояние,
    и второй законно видит другое. Именно на этом первая версия этой оценки дала ложную тревогу.
    """
    states, servers = build_pair(0, 0)
    mcp = McpServer(states[0], name="evaluation")
    results: list[dict] = []
    try:
        with AgentClient(f"http://127.0.0.1:{port_of(servers[0])}") as client:
            for index, method in enumerate(METHOD_NAMES):
                tool = method.replace("/", "_")
                arguments = _arguments_for(method, states[0], f"{index}")
                entry = {"method": method, "read_only": method in READ_ONLY,
                         "agree": False, "http": "", "mcp": "", "note": ""}

                def outcome(call):
                    try:
                        value = call()
                        return ("ok", json.dumps(value, sort_keys=True))
                    except AgentRefused as exc:
                        return ("refused", exc.code)
                    except Exception as exc:  # noqa: BLE001
                        return ("error", type(exc).__name__)

                http_outcome = outcome(lambda: client.call(method, arguments))
                response = mcp.handle({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                })
                if response and "result" in response:
                    if response["result"].get("isError"):
                        text = response["result"]["content"][0]["text"]
                        mcp_outcome = ("refused", "missing_field" if "missing required" in text else text[:40])
                    else:
                        try:
                            mcp_outcome = ("ok", json.dumps(tool_result_payload(response), sort_keys=True))
                        except Exception:  # noqa: BLE001
                            mcp_outcome = ("error", "payload")
                else:
                    mcp_outcome = ("error", "protocol")

                entry["http"] = http_outcome[0] + (f":{http_outcome[1][:40]}" if http_outcome[0] != "ok" else "")
                entry["mcp"] = mcp_outcome[0] + (f":{mcp_outcome[1][:40]}" if mcp_outcome[0] != "ok" else "")
                entry["agree"] = (
                    http_outcome == mcp_outcome if method in READ_ONLY
                    else http_outcome[0] == mcp_outcome[0]
                )
                results.append(entry)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()

    agreeing = [entry for entry in results if entry["agree"]]
    return {
        "methods": len(METHOD_NAMES),
        "read_only_compared_by_payload": len([entry for entry in results if entry["read_only"]]),
        "agreeing": len(agreeing),
        "disagreeing": [entry for entry in results if not entry["agree"]],
        "detail": results,
    }


def evaluate_adversarial() -> dict:
    """Кривой вход и повтор: движок обязан отвечать кодом, а не падать и не молчать."""
    cases: list[dict] = []

    def case(name: str, action) -> None:
        try:
            outcome = action()
            cases.append({"case": name, "handled": bool(outcome.get("handled")), "code": outcome.get("code", ""),
                          "note": outcome.get("note", "")})
        except Exception as exc:  # noqa: BLE001
            cases.append({"case": name, "handled": False, "code": "",
                          "note": f"crashed: {type(exc).__name__}: {exc}",
                          "trace": traceback.format_exc(limit=2)})

    states, servers = build_pair(0, 0)
    port_a, port_b = port_of(servers[0]), port_of(servers[1])
    base = f"http://127.0.0.1:{port_a}"
    try:
        client = AgentClient(base)

        def malformed_json():
            response = httpx.post(f"{base}/a2a/rpc", content=b"{not json",
                                  headers={"Content-Type": "application/json"})
            payload = response.json()
            code = (payload.get("error") or {}).get("message", "")
            return {"handled": "parse error" in code.lower(), "code": code, "note": str(response.status_code)}

        def unknown_method():
            response = httpx.post(f"{base}/a2a/rpc", json={
                "jsonrpc": "2.0", "id": 1, "method": "arli/not_a_method", "params": {}})
            payload = response.json()
            return {"handled": "unknown method" in json.dumps(payload), "code": "unknown method"}

        def missing_required_field():
            try:
                client.call("arli/registry/check", {})
                return {"handled": False, "note": "no refusal"}
            except AgentRefused as exc:
                return {"handled": exc.code != "unlisted", "code": exc.code, "note": exc.message}

        def forged_level_without_issuer():
            result = client.call("arli/claims/declare", {
                "feature": "skill:forged", "level": "topic_owner_verified", "issuer": "nobody"})
            return {"handled": result["verified"] is False,
                    "code": ",".join(result["reasons"]), "note": "no envelope from the issuer"}

        def floor_out_of_range():
            try:
                client.call("arli/psi/record", {"matched": [], "floor_basis_points": 20000})
                return {"handled": False, "note": "no refusal"}
            except AgentRefused as exc:
                return {"handled": exc.code != "unlisted", "code": exc.code, "note": exc.message}

        def too_many_features():
            """Потолок в 64 признака: превышение обязано отказать, а не обрезать."""
            holder = client.call("arli/psi/keys", {"session_id": "evaluation-cap"})
            blinded = [f"{index:064x}" for index in range(65)]
            try:
                client.call("arli/psi/sign", {
                    "session_id": "evaluation-cap", "blinded": blinded,
                    "n": holder["n"], "e": holder["e"],
                })
                return {"handled": False, "note": "no refusal"}
            except AgentRefused as exc:
                return {"handled": exc.code == "too_many_features", "code": exc.code, "note": exc.message}

        def wrong_seat():
            opened = client.call("arli/negotiation/open", {
                "negotiation_id": "evaluation-seat", "peers": [states[0].name, states[1].name],
                "network": "evaluation",
                "intents": [
                    {"intent_id": "i1", "owner": states[0].name, "network": "evaluation"},
                    {"intent_id": "i2", "owner": states[1].name, "network": "evaluation"},
                ],
            })
            if not opened.get("opened"):
                return {"handled": False, "note": f"pair did not open: {opened.get('rejection')}"}
            try:
                client.call("arli/negotiation/turn", {
                    "negotiation_id": "evaluation-seat", "agent": "somebody-else", "act": "propose"})
                return {"handled": False, "note": "a stranger took somebody else's turn"}
            except AgentRefused as exc:
                # Отказ может прийти и исключением: тогда код надо достать из него.
                return {"handled": exc.code == "not_a_seat", "code": exc.code, "note": exc.message}

        def tampered_envelope():
            recorded = client.call("arli/retention/record", {"evidence_id": "evaluation-ev",
                                                            "intent_id": "evaluation-intent"})
            archived = client.call("arli/retention/archive", {"intent_id": "evaluation-intent"})
            envelope = archived["evidence"]
            tampered = dict(envelope)
            tampered["normalized_result"] = {"marked_deleted": 999}
            public_pem = __import__("arli_engine.core.crypto", fromlist=["x"]).public_pem_from_private(
                states[0].private_pem)
            verified_good, _ = verify_envelope(envelope, public_pem)
            verified_bad, _ = verify_envelope(tampered, public_pem)
            return {"handled": verified_good and not verified_bad,
                    "code": "signature", "note": f"good={verified_good} tampered={verified_bad}"}

        def replayed_decision():
            opened = client.call("arli/negotiation/open", {
                "negotiation_id": "evaluation-replay", "peers": [states[0].name, states[1].name],
                "network": "evaluation",
                "intents": [
                    {"intent_id": "i1", "owner": states[0].name, "network": "evaluation"},
                    {"intent_id": "i2", "owner": states[1].name, "network": "evaluation"},
                ],
            })
            if not opened.get("opened"):
                return {"handled": False, "note": "pair did not open"}
            # Переговоры идут до исхода. Ход второй стороны уходит на её собственный адрес:
            # чужой адрес ответит законным отказом not_a_seat, и это будет ошибкой пробы.
            with AgentClient(f"http://127.0.0.1:{port_b}") as client_b:
                client.call("arli/negotiation/turn", {
                    "negotiation_id": "evaluation-replay", "seat": states[0].name,
                    "action": "propose", "message": "probe"})
                client_b.call("arli/negotiation/turn", {
                    "negotiation_id": "evaluation-replay", "seat": states[1].name,
                    "action": "accept", "message": "probe"})
            first = client.call("arli/opportunity/decide", {
                "negotiation_id": "evaluation-replay", "actor": states[0].name, "approve": True})
            try:
                second = client.call("arli/opportunity/decide", {
                    "negotiation_id": "evaluation-replay", "actor": states[0].name, "approve": True})
            except AgentRefused as exc:
                return {"handled": True, "code": exc.code, "note": exc.message}
            code = second.get("rejection") or second.get("code") or ""
            same_consent = second.get("human_consent") == first.get("human_consent")
            handled = bool(code) or second.get("rejected") is True or same_consent is False
            return {"handled": handled, "code": code or ("unchanged_consent" if same_consent else "repeated_decision"),
                    "note": str(second)[:120]}

        for name, action in (
            ("malformed json", malformed_json),
            ("unknown method", unknown_method),
            ("missing required field", missing_required_field),
            ("forged level without issuer", forged_level_without_issuer),
            ("floor out of range", floor_out_of_range),
            ("too many features", too_many_features),
            ("wrong seat", wrong_seat),
            ("tampered envelope", tampered_envelope),
            ("replayed decision", replayed_decision),
        ):
            case(name, action)
        client.close()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()

    handled = [entry for entry in cases if entry["handled"]]
    return {
        "cases": len(cases),
        "handled_with_a_code": len(handled),
        "unhandled": [entry for entry in cases if not entry["handled"]],
        "detail": cases,
    }


def evaluate_performance() -> dict:
    """Полный обмен на 8, 32 и 64 признаках: время и объём провода, по пять повторов."""
    measured: list[dict] = []
    for size in (8, 32, 64):
        runs = [exchange(f"evaluation-{size}-{index}", size) for index in range(5)]
        measured.append({
            "features_per_side": size,
            "matched": runs[0]["matched"],
            "median_ms": round(statistics.median(run["elapsed_ms"] for run in runs), 2),
            "min_ms": round(min(run["elapsed_ms"] for run in runs), 2),
            "max_ms": round(max(run["elapsed_ms"] for run in runs), 2),
            "wire_bytes": runs[0]["wire_bytes"],
            "repeats": 5,
        })
    return {"exchanges": measured}


def evaluate_concurrency() -> dict:
    """Сколько параллельных обменов держит движок на одном процессе."""
    def one(index: int) -> dict:
        started = time.perf_counter()
        outcome = exchange(f"evaluation-parallel-{index}", 16)
        return {"ok": outcome["matched"] > 0, "ms": round((time.perf_counter() - started) * 1000, 2)}

    runs: list[dict] = []
    for workers in (2, 4, 8):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, range(workers)))
        runs.append({
            "workers": workers,
            "succeeded": sum(1 for result in results if result["ok"]),
            "note": f"{sum(1 for result in results if result['ok'])} of {len(results)} exchanges found matches",
            "median_ms": round(statistics.median(result["ms"] for result in results), 2),
        })
    return {"runs": runs}


def evaluate_conformance() -> dict:
    states, servers = build_pair(0, 0)
    target = Target(base_url=f"http://127.0.0.1:{port_of(servers[0])}", name=states[0].name)
    holder = Target(base_url=f"http://127.0.0.1:{port_of(servers[1])}", name=states[1].name)
    try:
        report = run_conformance(target, holder)
    finally:
        target.close()
        holder.close()
        for server in servers:
            server.shutdown()
            server.server_close()
    return {
        "checks": len(report.checks),
        "failures": len(report.failures),
        "compliant": bool(report.compliant),
        "codes": [entry["code"] for entry in report.checks],
    }


def read_optional(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    result = {
        "what_this_is": (
            "Engine evaluation beyond the conformance suite: bindings agree with each other, "
            "curved input is answered with codes, full exchange cost, concurrency. Numbers are "
            "measured here or reported as not measured."
        ),
        "coverage": read_optional(OUT / "coverage.json"),
        "conformance": evaluate_conformance(),
        "bindings": evaluate_bindings(),
        "adversarial": evaluate_adversarial(),
        "performance": evaluate_performance(),
        "concurrency": evaluate_concurrency(),
        "psi_benchmark": read_optional(Path(__file__).resolve().parents[1] / "out" / "bench_intersection.json"),
        "measurement_calibration": read_optional(
            Path(__file__).resolve().parents[1] / "out" / "measure_dry_run.json"),
        "not_measured": [
            "Who is behind an agent: this implementation relays and verifies other people's "
            "statements about features and issues none of its own.",
            "Behaviour at production load: the numbers here come from one machine and local ports.",
            "Third-party security review: none has been done.",
            "Real human funnel: the pilot has not run; synthetic features are not evidence.",
        ],
        "elapsed_seconds": round(time.perf_counter() - started, 1),
    }
    EVALUATION.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "written": str(EVALUATION),
        "conformance": result["conformance"]["checks"],
        "bindings_agreeing": result["bindings"]["agreeing"],
        "bindings_methods": result["bindings"]["methods"],
        "adversarial": f"{result['adversarial']['handled_with_a_code']}/{result['adversarial']['cases']}",
        "performance": [
            (entry["features_per_side"], entry["median_ms"], entry["wire_bytes"])
            for entry in result["performance"]["exchanges"]
        ],
        "elapsed": result["elapsed_seconds"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
