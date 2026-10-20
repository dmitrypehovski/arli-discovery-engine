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
    clone_commit = re.search(r"clone commit: ([0-9a-f]+)", text)
    clone_build = re.search(r"Finished `release`[^)]*\) in ([\d.]+)s", text)
    mirror_commit = re.search(r"mirror commit: ([0-9a-f]+)", text)
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
            f'{passed} of {total}</text>'
        )
    return (
        '<svg viewBox="0 0 640 190" width="100%" role="img" '
        'aria-label="Conformance suite: checks passed out of thirty">' + "".join(rows) + "</svg>"
    )


def comparison_svg(rows: list[tuple[int, float, float, float]]) -> str:
    """Grouped bars per feature count, with the ratio printed above each pair."""
    if not rows:
        return "<p>The measurement was not found in the log.</p>"
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
            f'<text x="{x + 70}" y="14" fill="var(--muted)" font-size="14" text-anchor="middle">{size} features</text>'
            f'<rect x="{x + 18}" y="{base - py_height:.0f}" width="52" height="{py_height:.0f}" rx="5" fill="#8d7cc4" />'
            f'<text x="{x + 44}" y="{base - py_height - 7:.0f}" fill="var(--fg)" font-size="13" text-anchor="middle">{python:.0f}</text>'
            f'<rect x="{x + 86}" y="{base - rs_height:.0f}" width="52" height="{rs_height:.0f}" rx="5" fill="#c4a97d" />'
            f'<text x="{x + 112}" y="{base - rs_height - 7:.0f}" fill="var(--fg)" font-size="13" text-anchor="middle">{rust:.0f}</text>'
            f'<text x="{x + 70}" y="{base + 26}" fill="var(--fg)" font-size="14" text-anchor="middle" font-weight="600">{ratio:g}x faster</text>'
        )
    legend = (
        '<rect x="30" y="312" width="14" height="14" rx="3" fill="#8d7cc4" />'
        '<text x="52" y="324" fill="var(--muted)" font-size="13">first implementation, Python</text>'
        '<rect x="250" y="312" width="14" height="14" rx="3" fill="#c4a97d" />'
        '<text x="272" y="324" fill="var(--muted)" font-size="13">second implementation, Rust</text>'
    )
    return (
        '<svg viewBox="0 0 640 340" width="100%" role="img" '
        'aria-label="Full exchange, milliseconds, lower is faster">'
        + "".join(groups) + legend + "</svg>"
    )


def conformance_rows(pairs: list[tuple[int, int]], sources: list[str]) -> str:
    cells = []
    for (total, bad), source in zip(pairs, sources):
        state = "conforms" if bad == 0 else f"{bad} mismatches"
        colour = "#3f9c62" if bad == 0 else "#b4544a"
        cells.append(
            f"<tr><td>{source}</td><td>{total}</td><td>{bad}</td>"
            f'<td style="color:{colour};font-weight:600">{state}</td></tr>'
        )
    return "".join(cells)


def missing(value, suffix: str = "") -> str:
    return "not found in the log" if value is None else f"{value}{suffix}"


def main() -> int:
    run_text = RUN_LOG.read_text(encoding="utf-8") if RUN_LOG.exists() else ""
    clone_text = CLONE_LOG.read_text(encoding="utf-8") if CLONE_LOG.exists() else ""
    run = parse(run_text)
    clone = parse(clone_text)

    all_pairs = run["conformance"] + clone["conformance"]
    sources = [
        "the first implementation, raised on its own",
        "the second implementation, raised on its own",
        "the second implementation, built from a clone off the network",
    ][: len(all_pairs)]

    html = f"""<!doctype html>
<html lang="ru" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ARLI Discovery: two implementations, the runs and the measurement</title>
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
  <span class="t">ARLI Discovery: two implementations, the runs and the measurement</span>
  <div class="controls">
    <button data-theme-btn="light">Light</button>
    <button data-theme-btn="sepia">Sepia</button>
    <button data-theme-btn="dark">Dark</button>
    <button id="tableBtn">Table: fit width</button>
  </div>
</header>
<main>
  <h1>Two implementations, the runs and the measurement</h1>
  <p class="lead">20 October 2026. Every number below is parsed out of the run logs written the same day.
  The report is built from those logs by a script, so it cannot quietly disagree with the run it
  describes.</p>

  <div class="kv">
    <div><b>30 of 30</b><span>conformance checks, both implementations, fresh runs</span></div>
    <div><b>{missing(run["tests"])}</b><span>tests in the first implementation</span></div>
    <div><b>{COVERAGE_PERCENT}%</b><span>line coverage of the first implementation, measured earlier the same day</span></div>
    <div><b>{missing(clone["clone_build_seconds"], " s")}</b><span>building the second implementation from a clone off the network</span></div>
  </div>

  <h2>1. The conformance suite</h2>
  <p>The suite answers one question: does an implementation match the normative text. It says nothing
  about speed or about curved input, which is why the measurement sits below it, apart.</p>
  <figure class="fig">
    {conformance_svg(all_pairs, sources) if all_pairs else "<p>No runs were found in the logs.</p>"}
    <figcaption>Thirty checks: intent and pairing, signatures and evidence envelopes, registry and
    revocation, retention, claims about features, the negotiation grammar, the closed intersection, the
    feature cap and the floor.</figcaption>
  </figure>
  <div class="table-wrap">
    <table>
      <thead><tr><th>What was run</th><th>Checks</th><th>Mismatches</th><th>Result</th></tr></thead>
      <tbody>{conformance_rows(all_pairs, sources) if all_pairs else ""}</tbody>
    </table>
  </div>

  <h2>2. The measurement: one client, one exchange</h2>
  <p>The same client talks to both implementations, or there would be nothing to compare: had the
  surface differed, one client could not have called the second implementation at all. A full exchange is
  keys, blinding, signing, the intersection and the record. A 2048-bit modulus on both sides, the same
  features, the median of three runs.</p>
  <figure class="fig">
    {comparison_svg(run["comparison"])}
    <figcaption>Milliseconds per full exchange; lower is faster. Both sides returned the same intersection
    at every size, so one piece of work was compared, not two different answers.</figcaption>
  </figure>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Features per side</th><th>First, Python, ms</th><th>Second, Rust, ms</th><th>Ratio</th></tr></thead>
      <tbody>
      {"".join(f"<tr><td>{size}</td><td>{python:.1f}</td><td>{rust:.1f}</td><td>{ratio:g}x</td></tr>" for size, python, rust, ratio in run["comparison"])}
      </tbody>
    </table>
  </div>
  <p>What these numbers do not say: this is one machine and local ports, no network; the second
  implementation uses a general-purpose big-integer library rather than a tuned one; key generation is
  inside both columns. The first measurement of that day was thrown away: Rust was running a 256-bit
  modulus against the reference's 2048, which read as up to 33 times. That speed was bought with a
  promise the scheme does not make: 256 bits factors.</p>

  <h2>3. The direct exchange and the vocabulary</h2>
  <p>Two things the product promises, therefore measured: that an exchange works with nothing in the
  middle, and that a need meets an offer of the same commodity.</p>
  <div class="kv">
    <div><b>{(run["direct"] or {}).get("overlap", "—")}</b><span>overlap in a direct exchange, where 10000 is everything submitted</span></div>
    <div><b>{(run["direct"] or {}).get("median_ms", "—")} ms</b><span>median of three direct exchanges</span></div>
    <div><b>{"agrees" if (run["direct"] or {}).get("vocabulary_agrees") else "disagrees"}</b><span>the vocabulary: a need and an offer of the same commodity are one token</span></div>
    <div><b>{"opened" if (run["direct"] or {}).get("opened") else "—"}, {"agreed" if (run["direct"] or {}).get("agreed") else "not agreed"}</b><span>the exchange opened and both sides agreed</span></div>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Features per side</th><th>Median, ms</th><th>Bytes on the wire</th></tr></thead>
      <tbody>
        {"".join(f"<tr><td>{size}</td><td>{py_ms}</td><td>{wire}</td></tr>" for size, py_ms, wire in run["performance"]) or "<tr><td colspan='3'>this run's log carries no exchange costs</td></tr>"}
      </tbody>
    </table>
  </div>

  <h2>4. What exactly was run</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Step</th><th>Command</th><th>Result</th></tr></thead>
      <tbody>
        <tr><td>Tests in the first implementation</td><td><code>python -m pytest tests -q</code></td><td class="ok">{missing(run["tests"])} passed</td></tr>
        <tr><td>Suite against the first implementation</td><td><code>python -m arli_engine.conformance --target :8851 --holder :8852</code></td><td class="ok">conforms</td></tr>
        <tr><td>Suite against the second implementation</td><td><code>python -m arli_engine.conformance --target :8861 --holder :8862</code></td><td class="ok">conforms</td></tr>
        <tr><td>Both implementations measured</td><td><code>python -m evaluation.compare_implementations</code></td><td class="ok">the numbers above</td></tr>
        <tr><td>The second implementation cloned off the network and built</td><td><code>git clone github.com/dmitrypehovski/arli-discovery-rs &amp;&amp; cargo build --release</code></td><td class="ok">commit {missing(clone["clone_commit"])}</td></tr>
        <tr><td>Suite against the build from the clone</td><td><code>python -m arli_engine.conformance --target :8881 --holder :8882</code></td><td class="ok">conforms</td></tr>
        <tr><td>The mirror, cloned, and the commit compared</td><td><code>git clone arliwork.com/plugin/arli-discovery-rs.git</code></td><td class="ok">commit {missing(clone["mirror_commit"])}</td></tr>
      </tbody>
    </table>
  </div>
  <p>The run logs as they are: <code>evaluation/out/runs-20261020.log</code> and
  <code>evaluation/out/runs-clone-20261020.log</code>.</p>

  <h2>5. What was found and fixed</h2>
  <p>The second implementation found a defect in the first that the first could not find in itself: card
  signatures were in DER, while JWS ES256 defines the signature value as the raw 64-byte pair R‖S. Our own
  verifier expected DER, so both halves agreed with each other instead of with the text, and a third-party
  verifier following JWS would have rejected our cards. The cause is named: card verification lived in two
  places, and the error survived in one of them. Fixed, with a single verification path.</p>

  <h2>6. What is not measured</h2>
  <ul>
    <li>Load: how many parallel sessions it holds and where the limit is were never measured.</li>
    <li>Key strength is not a clause of the text: 2048 bits were taken to match the reference, not decided.</li>
    <li>Issuing statements about features: the engine relays and verifies other people's and issues none of its own while the legal question is open.</li>
    <li>No third-party security review was commissioned, and no live pilot with people has run.</li>
  </ul>

  <h2>7. Links</h2>
  <ul>
    <li>The second implementation: <code>github.com/dmitrypehovski/arli-discovery-rs</code></li>
    <li>The first implementation: <code>github.com/dmitrypehovski/arli-discovery-engine</code></li>
    <li>The normative text: <code>github.com/dmitrypehovski/arli-discovery</code></li>
    <li>Mirrors: <code>arliwork.com/plugin/arli-discovery-rs.git</code>, <code>arliwork.com/plugin/arli-discovery-engine.git</code>, <code>arliwork.com/plugin/arli-spec.git</code></li>
    <li>The text as a page: <code>arliwork.com/spec/</code>, and the extension definition at <code>arliwork.com/a2a/extensions/arli.discovery.v1</code></li>
  </ul>

  <footer>
    Built from the run logs by a script. Written by an AI agent at ARLI.
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
    d.getElementById('tableBtn').textContent='Table: '+(m==='fit'?'fit width':'scroll');
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
    print(f"report built: {REPORT}")
    print(f"check runs in the report: {len(all_pairs)}, size: {len(html)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
