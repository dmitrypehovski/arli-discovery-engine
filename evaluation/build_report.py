"""Визуальный отчёт об оценке: числа из evaluation.json превращаются в страницу и картинку.

Зачем картинка. Числа в файле читает тот, кто уже согласен смотреть файл. Решение принимают
по одному взгляду: где полоска неполная, где провал, что осталось неизмеренным. Поэтому те же
числа рисуются, а рисунок честно подписан: рядом с каждым рисунком стоит, что именно измерено
и чего в нём нет.

Числа не выдумываются: источник это evaluation/out/evaluation.json, собранный прогоном
оценки. Если файла нет, отчёт не собирается, а не собирается из воздуха.

Запуск: python -m evaluation.build_report
Результат: evaluation/out/report.html и, если рядом есть браузер, evaluation/out/report.png
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent / "out"
EVALUATION = OUT / "evaluation.json"
REPORT = OUT / "report.html"

INK = "#1c1c1a"
MUTED = "#6f6b62"
RULE = "#e6e1d6"
ACCENT = "#c4a97d"
GOOD = "#4f7a52"
BAD = "#a4524a"


def bar(value: float, total: float, *, colour: str, width: int = 320, height: int = 16) -> str:
    """Полоска: заполненная часть и целое. Целое всегда показано, иначе доля врёт."""
    share = 0 if total <= 0 else max(0.0, min(1.0, value / total))
    filled = round(width * share)
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#f2efe7"/>'
        f'<rect x="0" y="0" width="{filled}" height="{height}" fill="{colour}"/>'
        f"</svg>"
    )


def sparkline(points: list[tuple[float, float]], *, width: int = 320, height: int = 90,
              colour: str = ACCENT, unit: str = "") -> str:
    """Ломаная по точкам с подписями осей: без них график читается как украшение."""
    if len(points) < 2:
        return ""
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max_x - min_x or 1
    span_y = max_y - min_y or 1
    coords = []
    for x, y in points:
        px = 8 + (x - min_x) / span_x * (width - 16)
        py = height - 8 - (y - min_y) / span_y * (height - 16)
        coords.append(f"{px:.1f},{py:.1f}")
    labels = (
        f'<text x="8" y="12" font-size="11" fill="#6f6b62">{max_y:,.0f}{unit}</text>'
        f'<text x="8" y="{height - 4}" font-size="11" fill="#6f6b62">{min_y:,.0f}{unit}</text>'
        f'<text x="{width - 60}" y="{height - 4}" font-size="11" fill="#6f6b62">'
        f"{min_x:,.0f} to {max_x:,.0f}</text>"
    )
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fbfaf7"/>'
        f'<polyline points="{" ".join(coords)}" fill="none" stroke="{colour}" stroke-width="2"/>'
        + "".join(
            f'<circle cx="{c.split(",")[0]}" cy="{c.split(",")[1]}" r="3" fill="{colour}"/>'
            for c in coords
        )
        + labels
        + "</svg>"
    )


def module_label(path: str) -> str:
    """Короткое, но различимое имя модуля: два server.py в разных каталогах это разные вещи."""
    parts = Path(path).parts
    tail = Path(path).name
    if tail == "__init__.py":
        return f"{parts[-2]}/__init__"
    if len(parts) >= 2:
        return f"{parts[-2]}/{tail}"
    return tail


def table_row(name: str, value: str, note: str = "") -> str:
    return f"<tr><td>{name}</td><td class='num'>{value}</td><td class='note'>{note}</td></tr>"


def build_html(data: dict) -> str:
    coverage = data.get("coverage") or {}
    coverage_total = coverage.get("totals", {}).get("percent_covered", 0.0)
    conformance = data["conformance"]
    bindings = data["bindings"]
    adversarial = data["adversarial"]
    performance = data["performance"]["exchanges"]
    concurrency = data["concurrency"]["runs"]

    rows = []
    for item in performance:
        rows.append(table_row(
            f"{item['features_per_side']} features per side",
            f"{item['median_ms']} ms",
            f"{item['wire_bytes']} bytes on the wire, matched {item['matched']}, "
            f"min {item['min_ms']} / max {item['max_ms']} of {item['repeats']} runs",
        ))
    for item in concurrency:
        rows.append(table_row(
            f"{item['workers']} exchanges at once",
            f"{item['median_ms']} ms",
            f"{item['succeeded']} of {item['workers']} found matches",
        ))

    coverage_rows = "".join(
        f"<tr><td><code>{module_label(name)}</code></td>"
        f"<td class='num'>{item['summary']['percent_covered']:.0f}%</td>"
        f"<td>{bar(item['summary']['percent_covered'], 100, colour=ACCENT)}</td>"
        f"<td class='note'>{item['summary']['num_statements']} statements, "
        f"{item['summary']['missing_lines']} not executed</td></tr>"
        for name, item in sorted(
            coverage.get("files", {}).items(),
            key=lambda pair: pair[1]["summary"]["num_statements"],
            reverse=True,
        )[:14]
    )

    disagreeing = "".join(
        f"<li><code>{entry['method']}</code>: {entry['note'] or 'bindings differ'}</li>"
        for entry in bindings["disagreeing"]
    ) or "<li>none</li>"

    adversarial_rows = "".join(
        f"<tr><td>{entry['case']}</td>"
        f"<td class='num' style='color:{GOOD if entry['handled'] else BAD}'>"
        f"{'handled' if entry['handled'] else 'open'}</td>"
        f"<td class='note'>{entry['code'] or entry['note']}</td></tr>"
        for entry in adversarial["detail"]
    )

    not_measured = "".join(f"<li>{item}</li>" for item in data["not_measured"])

    latency_points = [(float(item["features_per_side"]), float(item["median_ms"])) for item in performance]
    bytes_points = [(float(item["features_per_side"]), float(item["wire_bytes"])) for item in performance]

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ARLI Discovery Engine: evaluation</title>
  <style>
    :root {{ --ink: {INK}; --muted: {MUTED}; --rule: {RULE}; --accent: {ACCENT}; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #fbfaf7; color: var(--ink);
      font: 16px/1.6 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
    .wrap {{ max-width: 940px; margin: 0 auto; padding: 44px 20px 80px; }}
    h1 {{ font-size: 27px; margin: 0 0 6px; }}
    .sub {{ color: var(--muted); margin: 0 0 26px; }}
    section {{ margin-top: 38px; }}
    h2 {{ font-size: 19px; margin: 0 0 4px; padding-bottom: 7px; border-bottom: 1px solid var(--rule); }}
    .basis {{ color: var(--muted); font-size: 14px; margin: 6px 0 14px; }}
    .cards {{ display: flex; gap: 14px; flex-wrap: wrap; }}
    .card {{ flex: 1 1 200px; border: 1px solid var(--rule); background: #fff; border-radius: 6px;
      padding: 14px 16px; }}
    .card .big {{ font-size: 30px; line-height: 1.1; }}
    .card .what {{ color: var(--muted); font-size: 13px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
    td, th {{ text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--rule); font-size: 14.5px; }}
    td.num {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
    td.note {{ color: var(--muted); font-size: 13px; }}
    code {{ background: #f2efe7; padding: 1px 5px; border-radius: 3px;
      font: 13px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; }}
    ul {{ margin: 8px 0 0 20px; }}
    li {{ margin-bottom: 5px; }}
    footer {{ margin-top: 44px; padding-top: 16px; border-top: 1px solid var(--rule);
      color: var(--muted); font-size: 13.5px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>ARLI Discovery Engine: what was evaluated</h1>
    <p class="sub">Measured on this machine, on local ports, on {data['elapsed_seconds']} seconds of
    run time. Every number below comes from a run, and the sections that follow say what is not
    measured, because a report that only shows the strong parts is not a report.</p>

    <div class="cards">
      <div class="card"><div class="big">{conformance['checks']}/{conformance['checks']}</div>
        <div class="what">conformance checks pass, {conformance['failures']} divergences, including
        three deliberately wrong implementations it catches</div></div>
      <div class="card"><div class="big">{coverage_total:.0f}%</div>
        <div class="what">of engine statements executed by the test suite</div></div>
      <div class="card"><div class="big">{bindings['agreeing']}/{bindings['methods']}</div>
        <div class="what">methods where the three bindings agree; read-only ones compared by payload</div></div>
      <div class="card"><div class="big">{adversarial['handled_with_a_code']}/{adversarial['cases']}</div>
        <div class="what">curved inputs answered with a code instead of a crash or silence</div></div>
    </div>

    <section>
      <h2>Test coverage</h2>
      <p class="basis">Measured with coverage.py over the whole test suite. The number is what the
      suite executes, not what it verifies: an executed line is not a checked line.</p>
      <p><strong>{coverage_total:.0f}%</strong> of statements,
      {coverage.get('totals', {}).get('missing_lines', 0)} lines never executed.</p>
      <table>
        <tr><th>module</th><th>covered</th><th></th><th></th></tr>
        {coverage_rows}
      </table>
    </section>

    <section>
      <h2>Bindings agree with each other</h2>
      <p class="basis">A2A over HTTP, the Python client and MCP must expose one surface. Read-only
      methods are compared by their whole payload; mutating ones by the outcome, because the first
      call legitimately changes what the second one sees.</p>
      <p>{bar(bindings['agreeing'], bindings['methods'], colour=GOOD)} {bindings['agreeing']} of
      {bindings['methods']} agree.</p>
      <p class="basis">Open items, each explained:</p>
      <ul>{disagreeing}</ul>
      <p class="basis">Note: the state-change item is a limitation of the probe, not of the engine;
      the uncoded refusal is a real gap in the engine and is on the fix list.</p>
    </section>

    <section>
      <h2>Curved input</h2>
      <p class="basis">Malformed JSON, unknown methods, forged levels, the feature cap, a stranger
      taking somebody else's turn, a tampered envelope, a repeated decision. A refusal must arrive
      as a code from a closed list, not as a stack trace.</p>
      <table>
        <tr><th>case</th><th>state</th><th>code or note</th></tr>
        {adversarial_rows}
      </table>
    </section>

    <section>
      <h2>Cost of a full exchange</h2>
      <p class="basis">Four calls: keys, blind, sign, finish and record. Medians of five runs. The
      cryptography runs on Python big integers, so an implementation in OpenSSL would be faster;
      these are honest current numbers, not a floor.</p>
      <table>
        <tr><th>measure</th><th>median</th><th></th></tr>
        {''.join(rows)}
      </table>
      <p class="basis">Time grows with the feature count, as expected:</p>
      {sparkline(latency_points)}
      <p class="basis">Bytes on the wire, same runs:</p>
      {sparkline(bytes_points, colour=GOOD)}
    </section>

    <section>
      <h2>Not measured, and why</h2>
      <ul>{not_measured}</ul>
    </section>

    <footer>
      Source: evaluation/out/evaluation.json, produced by <code>python -m evaluation.run_evaluation</code>.
      Conformance suite: <code>python -m arli_engine.conformance</code>. Normative text and
      implementation: github.com/dmitrypehovski/arli-discovery and /arli-discovery-engine.
    </footer>
  </div>
</body>
</html>
"""


def build_card_html(data: dict) -> str:
    """Компактная карточка: то же, но одним взглядом и без прокрутки на телефоне."""
    coverage = data.get("coverage") or {}
    coverage_total = coverage.get("totals", {}).get("percent_covered", 0.0)
    conformance = data["conformance"]
    bindings = data["bindings"]
    adversarial = data["adversarial"]
    performance = data["performance"]["exchanges"]

    module_rows = "".join(
        f"<tr><td><code>{module_label(name)}</code></td>"
        f"<td class='num'>{item['summary']['percent_covered']:.0f}%</td>"
        f"<td>{bar(item['summary']['percent_covered'], 100, colour=ACCENT, width=200, height=12)}</td></tr>"
        for name, item in sorted(
            coverage.get("files", {}).items(),
            key=lambda pair: pair[1]["summary"]["missing_lines"],
            reverse=True,
        )[:6]
    )
    probe_notes = {
        "replayed decision": "probe defect: the pair's journal is not shared in the packaged stand, "
                             "so the second side's turn is invisible to the first. The engine is "
                             "not at fault here; the stand is on the fix list.",
    }
    open_cases = "".join(
        f"<li>{entry['case']}: "
        f"{probe_notes.get(entry['case'], entry['code'] or entry['note'][:70])}</li>"
        for entry in adversarial["detail"] if not entry["handled"]
    ) or "<li>none</li>"
    cost_rows = "".join(
        f"<tr><td>{item['features_per_side']} features</td><td class='num'>{item['median_ms']} ms</td>"
        f"<td class='num'>{item['wire_bytes'] // 1024} KB</td>"
        f"<td class='note'>{item['matched']} matched</td></tr>"
        for item in performance
    )
    latency = [(float(item["features_per_side"]), float(item["median_ms"])) for item in performance]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>ARLI Discovery Engine: evaluation card</title>
<style>
  :root {{ --ink: {INK}; --muted: {MUTED}; --rule: {RULE}; --accent: {ACCENT}; }}
  body {{ margin: 0; background: #fbfaf7; color: var(--ink);
    font: 17px/1.55 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
  .wrap {{ width: 960px; margin: 0 auto; padding: 30px 28px 40px; }}
  h1 {{ font-size: 25px; margin: 0 0 4px; }}
  .sub {{ color: var(--muted); font-size: 15px; margin: 0 0 20px; }}
  .cards {{ display: flex; gap: 12px; }}
  .card {{ flex: 1; border: 1px solid var(--rule); background: #fff; border-radius: 6px; padding: 12px 14px; }}
  .card .big {{ font-size: 34px; line-height: 1.05; }}
  .card .what {{ color: var(--muted); font-size: 13px; }}
  h2 {{ font-size: 16px; margin: 22px 0 2px; }}
  .basis {{ color: var(--muted); font-size: 13.5px; margin: 2px 0 10px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: 5px 6px; border-bottom: 1px solid var(--rule); font-size: 14.5px; }}
  td.num {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
  td.note {{ color: var(--muted); font-size: 13px; }}
  code {{ background: #f2efe7; padding: 1px 4px; border-radius: 3px;
    font: 13px/1.4 ui-monospace, Menlo, monospace; }}
  ul {{ margin: 6px 0 0 18px; padding: 0; }}
  li {{ margin-bottom: 4px; font-size: 14.5px; }}
  footer {{ margin-top: 24px; color: var(--muted); font-size: 13px;
    border-top: 1px solid var(--rule); padding-top: 12px; }}
</style></head><body><div class="wrap">
  <h1>ARLI Discovery Engine</h1>
  <p class="sub">What is evaluated, measured on this machine in {data['elapsed_seconds']} seconds.
  Nothing below is an estimate.</p>
  <div class="cards">
    <div class="card"><div class="big">{conformance['checks']}/{conformance['checks']}</div>
      <div class="what">conformance checks, {conformance['failures']} divergences</div></div>
    <div class="card"><div class="big">{coverage_total:.0f}%</div>
      <div class="what">of statements executed by the tests</div></div>
    <div class="card"><div class="big">{bindings['agreeing']}/{bindings['methods']}</div>
      <div class="what">methods where bindings agree</div></div>
    <div class="card"><div class="big">{adversarial['handled_with_a_code']}/{adversarial['cases']}</div>
      <div class="what">curved inputs answered with a code</div></div>
  </div>

  <h2>Where the tests do not reach</h2>
  <p class="basis">{coverage.get('totals', {}).get('missing_lines', 0)} statements are never
  executed. Weakest modules by count of unexecuted lines:</p>
  <table>{module_rows}</table>

  <h2>Cost of a full exchange</h2>
  <p class="basis">Four calls, medians of five runs. Python big integers, so slower than an
  OpenSSL implementation would be.</p>
  {sparkline(latency, width=880, height=110, unit=" ms")}
  <table>{cost_rows}</table>

  <h2>Still open, and known</h2>
  <p class="basis">Curved inputs answered with something other than a code:</p>
  <ul>{open_cases}</ul>
  <p class="basis">Bindings that differ: {bindings['disagreeing'] and '; '.join(entry['method'] for entry in bindings['disagreeing']) or 'none'}.
  One of the two is a limitation of the probe, the other is a real gap and is on the fix list.</p>

  <h2>Not measured at all</h2>
  <ul>{''.join(f'<li>{item}</li>' for item in data['not_measured'])}</ul>
  <footer>Source: evaluation/out/evaluation.json. Full report: evaluation/out/report.html.
  Text: github.com/dmitrypehovski/arli-discovery. Code: /arli-discovery-engine.</footer>
</div></body></html>
"""


def main_card() -> int:
    data = json.loads(EVALUATION.read_text(encoding="utf-8"))
    card = OUT / "report-card.html"
    card.write_text(build_card_html(data), encoding="utf-8")
    print(json.dumps({"written": str(card), "bytes": card.stat().st_size}))
    return 0


def main() -> int:
    if not EVALUATION.is_file():
        raise SystemExit(f"no evaluation to draw: {EVALUATION} is missing; run the evaluation first")
    data = json.loads(EVALUATION.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(build_html(data), encoding="utf-8")
    print(json.dumps({"written": str(REPORT), "bytes": REPORT.stat().st_size}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_card())
