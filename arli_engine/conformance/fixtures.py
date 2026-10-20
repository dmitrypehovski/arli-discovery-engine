"""Реализации-обманки для набора проверок.

Набор, который проходит на нашей же реализации, ничего не доказывает. Поэтому в
комплекте лежат три заведомо неправильные реализации, и тест требует, чтобы набор
каждую поймал. Так набор не превращается в штамп.

Обманки:
- ``leaky``: карточка без ключа в заголовке (чужой не проверит), схема объявлена
  как пересечение по хешам, а в конверте матча есть число чужих признаков;
- ``unsigned``: всё честно, кроме подписи конвертов: их нет;
- ``padding``: всё честно, но пересечение добивается до потолка и у сторон разное.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from arli_engine.adapters.a2a.cards import build_card
from arli_engine.core.capability import EFFECT_KEYS, Operation
from arli_engine.core.evidence import Evidence
from arli_engine.core.signing import KeyPair, sign_card

CHEAT_KINDS = ("leaky", "unsigned", "padding")


def _operations() -> list[Operation]:
    """Объявляем все семь эффектов: обманка ломает не это, а схему и подписи."""
    every_effect = {key: False for key in EFFECT_KEYS}
    every_effect["may_access_network"] = True
    every_effect["mutates_artifact"] = True
    return [
        Operation(name="discover.match", description="Match intents", effects=every_effect),
        Operation(
            name="act.as.other",
            description="Act for another principal",
            effects=every_effect,
            availability_code="disabled_by_policy",
            availability_reason="An agent acts only for its own principal.",
        ),
    ]


class CheatingAgent:
    """Минимальная реализация нашей поверхности с одной намеренной ошибкой."""

    def __init__(self, kind: str, name: str, port: int, features: list[str]) -> None:
        if kind not in CHEAT_KINDS:
            raise ValueError(f"unknown cheat kind: {kind}")
        self.kind = kind
        self.name = name
        self.port = port
        self.features = features
        self.key = KeyPair.generate()
        self.card = self._build_card()
        self.sessions: dict[str, dict] = {}

    def _build_card(self) -> dict:
        card = build_card(
            name=self.name,
            description=f"cheating target ({self.kind})",
            base_url=f"http://127.0.0.1:{self.port}",
            skills=[{"id": "discover.match", "name": "match", "description": "d",
                     "inputModes": ["application/json"], "outputModes": ["application/json"]}],
            operations=_operations(),
            public_features=list(self.features),
            kid=f"{self.name}-key-1",
            private_pem=self.key.private_pem,
        )
        if self.kind == "leaky":
            # ключ из заголовка убираем: чужая сторона не сможет проверить карточку
            for entry in card["signatures"]:
                protected = json.loads(
                    __import__("base64").urlsafe_b64decode(entry["protected"] + "==")
                )
                protected.pop("jwk", None)
                entry["protected"] = __import__("base64").urlsafe_b64encode(
                    json.dumps(protected, separators=(",", ":")).encode()
                ).rstrip(b"=").decode()
        return card

    def capabilities(self) -> dict:
        from arli_engine.adapters.a2a.cards import capability_document

        doc = capability_document(_operations())
        if self.kind == "leaky":
            # объявляем старую схему вместо закрытого пересечения
            doc["intersection"] = {
                "scheme": "hash_intersection",
                "feature_cap": 64,
                "closed": "nothing",
                "not_closed": "everything",
                "match_rejections": [],
            }
        return doc

    def _evidence(self, operation: str, parameters: dict, result: dict) -> dict:
        envelope = Evidence(
            provider={"id": "cheat", "name": f"cheating target {self.kind}", "version": "1"},
            operation=operation,
            parameters=parameters,
            normalized_result=result,
            confidence="derived",
            authority="local-calculation",
            limitations=["this is a deliberately non-conforming target"],
        ).to_json()
        if self.kind == "leaky":
            envelope["parameters"]["counterpart_feature_count"] = 3
        if self.kind == "unsigned":
            return envelope
        from arli_engine.core.crypto import sign_envelope

        return sign_envelope(envelope, self.key.private_pem, f"{self.name}-key-1")

    def handle(self, method: str, params: dict) -> tuple[dict | None, str | None]:
        if method == "arli/negotiation/open":
            standings = params.get("intents") or []
            if params.get("network") and len(standings) == 2:
                owners = {s.get("owner") for s in standings}
                active = all(s.get("active", True) for s in standings)
                kinds = {s.get("type", "person") for s in standings}
                synthetic = any(s.get("provenance") == "synthetic" for s in standings)
                if not active:
                    code = "intent_inactive"
                elif synthetic and not params.get("admits_synthetic", False):
                    code = "synthetic_not_admitted"
                elif len(owners) < 2:
                    code = "same_owner"
                elif kinds == {"supply"} or kinds == {"demand"}:
                    code = "types_not_comparable"
                else:
                    code = None
                if code:
                    return {"negotiation_id": params.get("negotiation_id"), "opened": False,
                            "rejection": code,
                            "evidence": self._evidence("negotiation_open", {}, {"opened": False})}, None
            return {"negotiation_id": params.get("negotiation_id"), "opened": True,
                    "initiator": (params.get("peers") or [self.name])[0]}, None

        if method == "arli/negotiation/turn":
            if params.get("seat") != self.name:
                return None, "not_a_seat: this agent holds another seat"
            if params.get("action") == "accept":
                return None, "first_turn_is_propose_or_decline: first turn was accept"
            return {"turn": params, "outcome": None,
                    "evidence": self._evidence("negotiation_turn", {}, {"outcome": None})}, None

        if method == "arli/psi/keys":
            session = params["session_id"]
            from arli_engine.core.psi import generate_keys

            keys = self.sessions.setdefault(session, {})
            if "keys" not in keys:
                keys["keys"] = generate_keys()
            n, e = keys["keys"].public()
            return {"session_id": session, "n": format(n, "x"), "e": format(e, "x"),
                    "scheme": "closed_blind_rsa", "feature_cap": 64}, None

        if method == "arli/psi/commit":
            return {"session_id": params["session_id"],
                    "blinded": ["aa"] * len(self.features)}, None

        if method == "arli/psi/sign":
            blinded = params.get("blinded") or []
            if len(blinded) > 64:
                return None, "too_many_features: cap is 64"
            return {"session_id": params["session_id"],
                    "signed": ["bb"] * len(blinded),
                    "holder_tokens": ["cc"] * len(self.features)}, None

        if method == "arli/psi/finish":
            if params.get("floor_basis_points") is None:
                return None, "missing_floor: floor_basis_points is required"
            matched = ["city:sf", "skill:design"]
            if params.get("floor_basis_points", 0) > 5000:
                matched = []
            if self.kind == "padding":
                matched = matched + [f"padded:{i}" for i in range(8)]
            return {"side": self.name, "matched_features": matched,
                    "overlap_basis_points": 5000, "overlap_ratio": "2/4",
                    "evidence": self._evidence("closed_intersection", {"side": self.name},
                                               {"matched_feature_count": len(matched)})}, None

        if method == "arli/psi/record":
            matched = params.get("matched") or []
            if self.kind == "padding":
                # у держателя получается другой набор: добивка несимметрична
                matched = [m for m in matched if not str(m).startswith("padded:")]
            return {"side": self.name, "matched_features": matched,
                    "overlap_basis_points": 5000, "overlap_ratio": "2/4",
                    "evidence": self._evidence("closed_intersection_holder", {"side": self.name},
                                               {"matched_feature_count": len(matched)})}, None

        return None, f"unknown method: {method}"


def start_cheating_agent(agent: CheatingAgent) -> ThreadingHTTPServer:
    handler_class = _handler_for(agent)
    server = ThreadingHTTPServer(("127.0.0.1", agent.port), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _handler_for(agent: CheatingAgent):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args, **kwargs) -> None:  # тишина в тестах
            return

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/.well-known/agent-card.json":
                self._json(200, agent.card)
            elif self.path == "/.well-known/arli-capabilities.json":
                self._json(200, agent.capabilities())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length) or b"{}")
            result, error = agent.handle(request.get("method", ""), request.get("params") or {})
            if error:
                self._json(200, {"jsonrpc": "2.0", "id": request.get("id"),
                                 "error": {"code": -32000, "message": error}})
            else:
                self._json(200, {"jsonrpc": "2.0", "id": request.get("id"), "result": result})

    return Handler
