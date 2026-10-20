"""Connecting somebody else's agent, and matching against it over the network.

Why this exists. A form on a page creates agents we host, which is a catalogue with extra steps.
A network accepts agents that run anywhere, on anybody's harness, as long as they speak the text:
the agent card is signed by their key, they answer our extension's calls, and matching happens
between two agents that never see each other's lists.

Two things this file is careful about.

Ownership. Anyone can point at someone else's agent, so connecting proves control of the key: we
fetch the card, verify its signature, send a nonce, and require the same key to sign that nonce
before the agent enters the directory.

Reach. A directory that fetches URLs on demand is an invitation to make our server probe its own
insides, so addresses are checked before anything is requested: only http and https, no loopback,
no private ranges, no link-local, bounded time and size.

Run: used by app/service.py.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arli_engine.client import AgentClient  # noqa: E402
from arli_engine.core.signing import verify_card_self  # noqa: E402
from arli_engine.serve import build_pair, port_of  # noqa: E402

CARD_PATH = "/.well-known/agent-card.json"
RPC_PATH = "/a2a/rpc"
NETWORK = "network-eu-design"
FETCH_TIMEOUT = 8.0
MAX_BYTES = 200_000
ALLOW_PRIVATE = "ARLI_MATCH_ALLOW_PRIVATE"


class ConnectRefused(Exception):
    """A refusal always carries a code, so a caller can tell the cases apart."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _address_is_reachable(host: str, allow_private: bool) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False
        if parsed.is_loopback or parsed.is_private or parsed.is_link_local or parsed.is_reserved:
            if not allow_private:
                return False
    return True


def normalize_endpoint(raw: str, *, allow_private: bool) -> str:
    """Check the address before a single byte is requested."""
    text = (raw or "").strip().rstrip("/")
    if not text:
        raise ConnectRefused("missing_endpoint", "an endpoint is required")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise ConnectRefused("unsupported_scheme", f"only http and https, not {parsed.scheme or 'none'}")
    if not parsed.hostname:
        raise ConnectRefused("bad_endpoint", f"no host in {text}")
    if not _address_is_reachable(parsed.hostname, allow_private):
        raise ConnectRefused(
            "address_not_reachable",
            f"{parsed.hostname} resolves to a loopback or private address; that is refused unless "
            f"{ALLOW_PRIVATE} is set for a local test",
        )
    return text


def fetch_card(endpoint: str) -> dict:
    url = endpoint + CARD_PATH
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "arli-match/0.3"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise ConnectRefused("card_unreachable", f"{url} answered {error.code}") from error
    except Exception as error:  # noqa: BLE001
        raise ConnectRefused("card_unreachable", f"{url}: {type(error).__name__}") from error
    if len(body) > MAX_BYTES:
        raise ConnectRefused("card_too_large", f"the card is over {MAX_BYTES} bytes")
    try:
        card = json.loads(body)
    except json.JSONDecodeError as error:
        raise ConnectRefused("card_not_json", str(error)) from error
    if not isinstance(card, dict):
        raise ConnectRefused("card_not_json", "the card is not an object")
    return card


def card_key(card: dict) -> tuple[dict, str]:
    """The public key and its id, from the card's own signature header."""
    import base64

    signatures = card.get("signatures") or []
    if not signatures:
        raise ConnectRefused("card_unsigned", "the card carries no signature")
    entry = signatures[0]
    protected = entry.get("protected") or ""
    try:
        header = json.loads(base64.urlsafe_b64decode(protected + "=" * (-len(protected) % 4)))
    except Exception as error:  # noqa: BLE001
        raise ConnectRefused("card_malformed", f"the header cannot be read: {error}") from error
    jwk = header.get("jwk") or {}
    if not jwk.get("x") or not jwk.get("y"):
        raise ConnectRefused("card_unsigned", "the header carries no public key")
    return jwk, str(header.get("kid") or "")


def verify_endpoint(endpoint: str) -> dict:
    """Fetch the card and verify it. Nothing about the agent is trusted before this passes."""
    card = fetch_card(endpoint)
    verified, reasons = verify_card_self(card)
    if not verified:
        raise ConnectRefused("card_not_verified", "; ".join(reasons) or "signature does not verify")
    jwk, kid = card_key(card)
    return {"card": card, "jwk": jwk, "kid": kid, "name": str(card.get("name") or endpoint)}


def verify_signed_nonce(jwk: dict, nonce: str, signature_b64: str) -> bool:
    """The connecting agent proves control of its key by signing the nonce we chose."""
    import base64

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    def decode(text: str) -> bytes:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

    try:
        x = int.from_bytes(decode(jwk["x"]), "big")
        y = int.from_bytes(decode(jwk["y"]), "big")
        key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        raw = decode(signature_b64)
        if len(raw) == 64:
            der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        else:
            return False
        key.verify(der, nonce.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


def run_remote_exchange(my_features: list[str], remote_endpoint: str, floor: int, *,
                        remote_id: str, remote_kind: str, my_kind: str = "demand",
                        local_name: str = "you", network: str = NETWORK) -> dict:
    """One full exchange where the counterparty runs on its own machine, on its own harness.

    Our side stays local: the agent that holds our features is raised here for the length of the
    exchange. Their side is reached over the network, and only the extension's own calls travel.
    """
    import re

    states, servers = build_pair(0, 0, names=(local_name, "peer"))
    try:
        states[0].features = type(states[0].features)(my_features)
        local_port = port_of(servers[0])
        seat_a, seat_b = local_name, remote_id
        session = f"remote-{abs(hash((remote_endpoint, floor))) % 10**8}"

        with AgentClient(f"http://127.0.0.1:{local_port}") as mine, \
                AgentClient(remote_endpoint) as theirs:
            open_params = {
                "negotiation_id": session, "peers": [seat_a, seat_b], "network": network,
                "admits_synthetic": False,
                "intents": [
                    {"intent_id": f"{seat_a}-{my_kind}", "owner": seat_a, "type": my_kind,
                     "network": network, "provenance": "human", "active": True,
                     "claim_level": "declared"},
                    {"intent_id": f"{seat_b}-{remote_kind}", "owner": seat_b, "type": remote_kind,
                     "network": network, "provenance": "human", "active": True,
                     "claim_level": "declared"},
                ],
            }
            wire = []

            def call(client: AgentClient, method: str, params: dict) -> dict:
                wire.append(json.dumps(params, ensure_ascii=False))
                return client.call(method, params)

            opened = call(mine, "arli/negotiation/open", open_params)
            if not opened.get("opened"):
                return _refused(str(opened.get("rejection")), remote_id, floor)
            try:
                call(theirs, "arli/negotiation/open", open_params)

                mine_card = mine.get_card()
                theirs_card = theirs.get_card()

                # Our turn, then the counterparty records it in its own journal: two machines do
                # not share a log, so each side witnesses what the other said.
                propose = call(mine, "arli/negotiation/turn",
                               {"negotiation_id": session, "seat": seat_a, "action": "propose",
                                "message": {"scope": "matching only"}})
                call(theirs, "arli/negotiation/witness",
                     {"negotiation_id": session, "seat": seat_a, "action": "propose",
                      "message": {"scope": "matching only"},
                      "proof": propose["evidence"], "card": mine_card})

                accept = call(theirs, "arli/negotiation/turn",
                              {"negotiation_id": session, "seat": seat_b, "action": "accept",
                               "message": {"agreed": True}})
                call(mine, "arli/negotiation/witness",
                     {"negotiation_id": session, "seat": seat_b, "action": "accept",
                      "message": {"agreed": True},
                      "proof": accept["evidence"], "card": theirs_card})

                call(mine, "arli/opportunity/decide",
                     {"negotiation_id": session, "actor": seat_a, "approve": True})
                call(theirs, "arli/opportunity/decide",
                     {"negotiation_id": session, "actor": seat_b, "approve": True})

                keys = call(theirs, "arli/psi/keys", {"session_id": session})
                commit = call(mine, "arli/psi/commit",
                              {"session_id": session, "peer_name": seat_b, "n": keys["n"],
                               "e": keys["e"]})
                signed = call(theirs, "arli/psi/sign",
                              {"session_id": session, "blinded": commit["blinded"]})
                finish = call(mine, "arli/psi/finish",
                              {"session_id": session, "n": keys["n"], "signed": signed["signed"],
                               "holder_tokens": signed["holder_tokens"],
                               "floor_basis_points": floor})
                call(theirs, "arli/psi/record",
                     {"session_id": session, "matched": finish.get("matched_features", []),
                      "floor_basis_points": floor})
            except Exception as error:  # noqa: BLE001
                refusal_code = getattr(error, "code", None)
                code = refusal_code or f"{type(error).__name__}"
                # A protocol refusal carries a code; a transport failure does not.
                return _refused(f"remote_refused: {code}", remote_id, floor,
                                answered=bool(refusal_code))

            # Two different questions, kept apart. Before the intersection is revealed, no
            # feature of ours may travel: that is the promise. The record call comes after by
            # design, because both sides hold an envelope over the same intersection, so it may
            # carry the shared items and nothing else.
            before = wire[:-1] if wire else []
            revealed = wire[-1] if wire else ""
            leaked = [feature for feature in my_features
                      for payload in before
                      if re.search(re.escape(feature.strip()), payload, re.IGNORECASE)]
            revealed_extras = [feature for feature in my_features
                               if feature not in (finish.get("matched_features") or [])
                               and re.search(re.escape(feature.strip()), revealed, re.IGNORECASE)]
            basis = int(finish.get("overlap_basis_points") or 0)
            return {
                "id": remote_id, "name": remote_id, "kind": remote_kind, "city": "",
                "headline": f"connected agent at {remote_endpoint}", "claim_level": "declared",
                "will_not_publish": "whatever its owner keeps at home",
                "feature_count": None, "sample": False, "remote": True,
                "endpoint": remote_endpoint, "answered": True,
                "opened": True, "rejection": None,
                "agreed": accept.get("outcome") == "agreed",
                "overlap_basis_points": basis,
                "overlap_ratio": finish.get("overlap_ratio"),
                "matched_features": finish.get("matched_features", []),
                "passed_floor": basis >= floor,
                "wires_clean": not leaked and not revealed_extras,
                "leaked_features": leaked + revealed_extras,
                "evidence_ids": {},
            }
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def _refused(code: str, remote_id: str, floor: int, *, answered: bool = True) -> dict:
    """A refused exchange. `answered` separates the two cases a caller must not confuse: the agent
    spoke and said no, or nothing came back at all. A dead address and a strict agent are different
    problems, and only one of them means the agent has left the room."""
    return {
        "answered": answered,
        "id": remote_id, "name": remote_id, "kind": "?", "city": "",
        "headline": "connected agent", "claim_level": "declared", "will_not_publish": "",
        "feature_count": None, "sample": False, "remote": True,
        "opened": False, "rejection": code, "agreed": False,
        "overlap_basis_points": None, "overlap_ratio": None, "matched_features": [],
        "passed_floor": False, "wires_clean": True, "leaked_features": [], "evidence_ids": {},
    }


def allow_private_addresses() -> bool:
    import os

    value = os.environ.get(ALLOW_PRIVATE, "")
    return value.strip().lower() in ("1", "true", "yes")
