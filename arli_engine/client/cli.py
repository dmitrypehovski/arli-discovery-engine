"""Командная строка: вызвать метод агента из оболочки, без своего кода.

Запуск: python -m arli_engine.client.cli <адрес> <метод> ['{параметры в JSON}']
Печатает результат как JSON. Отказ печатает причину в поток ошибок и выходит с кодом 3,
чтобы в скриптах отказ нельзя было спутать с успехом. Прочие сбои выходят с кодом 2.
"""

from __future__ import annotations

import json
import sys

from arli_engine.client.agent import AgentClient, AgentClientError, AgentRefused

USAGE = "usage: python -m arli_engine.client.cli <base_url> <method> [json-params]"


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 2
    base_url, method = argv[0], argv[1]
    raw_params = argv[2] if len(argv) > 2 else "{}"
    try:
        params = json.loads(raw_params)
    except json.JSONDecodeError as exc:
        print(f"parameters are not JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(params, dict):
        print("parameters must be a JSON object", file=sys.stderr)
        return 2

    try:
        with AgentClient(base_url) as client:
            result = client.call(method, params)
    except AgentRefused as exc:
        print(json.dumps({"refused": exc.code, "message": exc.message}, ensure_ascii=False), file=sys.stderr)
        return 3
    except (AgentClientError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
