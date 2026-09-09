"""Evidence report for the end-to-end pipeline run.

The end-to-end test is the only place where every phase runs against one
snapshot in one process, so it is the only place that can answer "does the whole
thing work?" with numbers rather than with a green dot. This module turns the
run into a readable artefact: what went in, what each stage produced, which
economic property was asserted about it, and what was observed.

Two formats fall out of the same recording, because they are read in two places:
Markdown for a diff and a pull request, HTML for a browser.

No pytest import here on purpose. The recorder is a plain object the test fills
in, which keeps the report renderable from a script or a notebook if the
pipeline ever has to be demonstrated outside the test suite.
"""

from __future__ import annotations

import html
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

__all__ = ["Check", "PipelineEvidence", "Stage", "Value", "report_directory"]

REPORT_DIR_ENV = "TES_PRICER_E2E_REPORT_DIR"
"""Override for where the report is written; defaults to ``<repo>/reports``."""


def report_directory(project_root: Path) -> Path:
    """Return the directory the report is written to."""
    override = os.environ.get(REPORT_DIR_ENV)
    return Path(override) if override else project_root / "reports"


@dataclass(frozen=True, slots=True)
class Value:
    """One input or output of a stage, with its provenance or a caveat."""

    label: str
    value: str
    source: str = ""


@dataclass(frozen=True, slots=True)
class Check:
    """One asserted economic property and what the run actually produced."""

    claim: str
    expected: str
    observed: str
    passed: bool


@dataclass(slots=True)
class Stage:
    """One phase of the pipeline as it ran."""

    index: int
    title: str
    module: str
    purpose: str
    inputs: list[Value] = field(default_factory=list)
    outputs: list[Value] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add_input(self, label: str, value: str, source: str = "") -> None:
        """Record an input the stage consumed."""
        self.inputs.append(Value(label, value, source))

    def add_output(self, label: str, value: str, source: str = "") -> None:
        """Record a number the stage produced."""
        self.outputs.append(Value(label, value, source))

    def check(self, claim: str, expected: str, observed: str, passed: bool) -> bool:
        """Record an asserted property and return ``passed`` for the caller to assert on.

        Returning the flag rather than asserting keeps the recording here and the
        assertion in the test, so a failure points at the test's own line and the
        report still shows which property was the one that broke.
        """
        self.checks.append(Check(claim, expected, observed, bool(passed)))
        return bool(passed)

    def note(self, text: str) -> None:
        """Record a caveat or an explanation that belongs with this stage."""
        self.notes.append(text)

    @property
    def passed(self) -> bool:
        """Whether every recorded check on this stage held."""
        return all(check.passed for check in self.checks)


@dataclass(slots=True)
class PipelineEvidence:
    """Recorder for one end-to-end run, renderable as Markdown or HTML."""

    snapshot_date: date
    mode: str
    """``"cached (fixtures, no network)"`` or ``"live"``."""
    stages: list[Stage] = field(default_factory=list)
    headline: list[Value] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    started_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )

    def stage(self, title: str, module: str, purpose: str) -> Stage:
        """Open and return the next stage, numbered in call order."""
        stage = Stage(index=len(self.stages) + 1, title=title, module=module, purpose=purpose)
        self.stages.append(stage)
        return stage

    def add_headline(self, label: str, value: str, source: str = "") -> None:
        """Record a figure for the summary strip at the top of the report."""
        self.headline.append(Value(label, value, source))

    def limitation(self, text: str) -> None:
        """Record a limitation of the run as a whole."""
        self.limitations.append(text)

    # -- aggregates ---------------------------------------------------------
    @property
    def checks(self) -> list[Check]:
        """Every check across every stage, in run order."""
        return [check for stage in self.stages for check in stage.checks]

    @property
    def passed(self) -> bool:
        """Whether every check across every stage held."""
        return all(check.passed for check in self.checks)

    # -- rendering ----------------------------------------------------------
    def write(self, directory: Path) -> tuple[Path, Path]:
        """Write ``e2e_pipeline_report.md`` and ``.html``; return both paths."""
        directory.mkdir(parents=True, exist_ok=True)
        markdown_path = directory / "e2e_pipeline_report.md"
        html_path = directory / "e2e_pipeline_report.html"
        markdown_path.write_text(self.to_markdown(), encoding="utf-8")
        html_path.write_text(self.to_html(), encoding="utf-8")
        return markdown_path, html_path

    def to_markdown(self) -> str:
        """Render the run as a Markdown document."""
        total = len(self.checks)
        held = sum(1 for check in self.checks if check.passed)
        lines = [
            "# TES pipeline - end-to-end evidence",
            "",
            f"**Snapshot** {self.snapshot_date.isoformat()} &middot; "
            f"**Mode** {self.mode} &middot; "
            f"**Run** {self.started_at} &middot; "
            f"**Checks** {held}/{total} held",
            "",
            "Ingestion through greeks, one snapshot, one process. Every figure below was "
            "produced by the run that generated this file.",
            "",
        ]

        if self.headline:
            lines += ["| Figure | Value | Note |", "| --- | --- | --- |"]
            lines += [f"| {v.label} | `{v.value}` | {v.source} |" for v in self.headline]
            lines.append("")

        for stage in self.stages:
            lines += [
                f"## {stage.index}. {stage.title}",
                "",
                f"`{stage.module}` - {stage.purpose}",
                "",
            ]
            if stage.inputs:
                lines += ["**Inputs**", "", "| Input | Value | Source |", "| --- | --- | --- |"]
                lines += [f"| {v.label} | `{v.value}` | {v.source} |" for v in stage.inputs]
                lines.append("")
            if stage.outputs:
                lines += ["**Outputs**", "", "| Output | Value | Note |", "| --- | --- | --- |"]
                lines += [f"| {v.label} | `{v.value}` | {v.source} |" for v in stage.outputs]
                lines.append("")
            if stage.checks:
                lines += [
                    "**Checks**",
                    "",
                    "| Property asserted | Threshold | Observed | Result |",
                    "| --- | --- | --- | --- |",
                ]
                lines += [
                    f"| {c.claim} | `{c.expected}` | `{c.observed}` | "
                    f"{'PASS' if c.passed else 'FAIL'} |"
                    for c in stage.checks
                ]
                lines.append("")
            for note in stage.notes:
                lines += [f"> {note}", ""]

        if self.limitations:
            lines += ["## Limitations of this run", ""]
            lines += [f"- {text}" for text in self.limitations]
            lines.append("")
        return "\n".join(lines)

    def to_html(self) -> str:
        """Render the run as a self-contained HTML page."""
        total = len(self.checks)
        held = sum(1 for check in self.checks if check.passed)
        status = "all checks held" if self.passed else f"{total - held} of {total} checks failed"
        status_class = "ok" if self.passed else "bad"

        parts = [
            _HTML_HEAD,
            "<main>",
            "<header class='masthead'>",
            "<p class='eyebrow'>Colombian TES &middot; NSS curve &middot; USD/COP forward</p>",
            "<h1>End-to-end pipeline evidence</h1>",
            "<p class='standfirst'>Ingestion through greeks, one snapshot, one process. "
            "Every figure on this page was produced by the run that generated it.</p>",
            "<dl class='runbar'>",
            _dl_item("Snapshot", self.snapshot_date.isoformat()),
            _dl_item("Mode", self.mode),
            _dl_item("Run at", self.started_at),
            _dl_item("Checks", f"{held}/{total}", status_class),
            "</dl>",
            f"<p class='verdict {status_class}'>{_e(status)}</p>",
            "</header>",
        ]

        if self.headline:
            parts.append("<section class='headline'><h2>Result</h2><div class='figures'>")
            for value in self.headline:
                note = f"<span class='figure-note'>{_e(value.source)}</span>" if value.source else ""
                parts.append(
                    "<div class='figure'>"
                    f"<span class='figure-label'>{_e(value.label)}</span>"
                    f"<span class='figure-value'>{_e(value.value)}</span>"
                    f"{note}</div>"
                )
            parts.append("</div></section>")

        parts += [_stage_html(stage) for stage in self.stages]

        if self.limitations:
            parts.append("<section class='limits'><h2>Limitations of this run</h2><ul>")
            parts += [f"<li>{_e(text)}</li>" for text in self.limitations]
            parts.append("</ul></section>")

        parts += [
            "<footer><p>Generated by "
            "<code>tests/integration/test_end_to_end_pipeline.py</code>. Regenerate with "
            "<code>pytest -m integration tests/integration/test_end_to_end_pipeline.py</code>."
            "</p></footer>",
            "</main>",
            "</body>",
            "</html>",
        ]
        return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------- #
# HTML helpers
# --------------------------------------------------------------------------- #
def _e(text: object) -> str:
    """HTML-escape a value for interpolation into the page."""
    return html.escape(str(text), quote=True)


def _dl_item(label: str, value: str, extra_class: str = "") -> str:
    """One label/value pair of the run bar."""
    classes = f" class='{extra_class}'" if extra_class else ""
    return f"<div><dt>{_e(label)}</dt><dd{classes}>{_e(value)}</dd></div>"


def _table(headers: list[str], rows: list[list[str]], *, kind: str) -> str:
    """A horizontally scrollable table; cells are already-escaped HTML."""
    head = "".join(f"<th scope='col'>{_e(header)}</th>" for header in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return (
        f"<div class='scroller'><table class='{kind}'>"
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _value_rows(values: list[Value]) -> list[list[str]]:
    """Escape a list of values into table rows."""
    return [
        [_e(v.label), f"<span class='num'>{_e(v.value)}</span>", _e(v.source)] for v in values
    ]


def _check_rows(checks: list[Check]) -> list[list[str]]:
    """Escape a list of checks into table rows."""
    rows = []
    for check in checks:
        state = "pass" if check.passed else "fail"
        rows.append(
            [
                _e(check.claim),
                f"<span class='num'>{_e(check.expected)}</span>",
                f"<span class='num'>{_e(check.observed)}</span>",
                f"<span class='chip {state}'>{state.upper()}</span>",
            ]
        )
    return rows


def _stage_html(stage: Stage) -> str:
    """Render one pipeline stage."""
    parts = [
        f"<section class='stage {'ok' if stage.passed else 'bad'}'>",
        "<div class='stage-head'>",
        f"<span class='stage-index'>{stage.index:02d}</span>",
        f"<div><h2>{_e(stage.title)}</h2>",
        f"<p class='module'><code>{_e(stage.module)}</code></p></div>",
        "</div>",
        f"<p class='purpose'>{_e(stage.purpose)}</p>",
    ]
    if stage.inputs:
        parts.append("<h3>Inputs</h3>")
        parts.append(_table(["Input", "Value", "Source"], _value_rows(stage.inputs), kind="io"))
    if stage.outputs:
        parts.append("<h3>Outputs</h3>")
        parts.append(_table(["Output", "Value", "Note"], _value_rows(stage.outputs), kind="io"))
    if stage.checks:
        parts.append("<h3>Checks</h3>")
        parts.append(
            _table(
                ["Property asserted", "Threshold", "Observed", "Result"],
                _check_rows(stage.checks),
                kind="checks",
            )
        )
    parts += [f"<p class='note'>{_e(note)}</p>" for note in stage.notes]
    parts.append("</section>")
    return "".join(parts)


_HTML_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TES Pipeline Evidence</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&amp;family=IBM+Plex+Sans:wght@400;600;700&amp;family=IBM+Plex+Serif:wght@400;600&amp;display=swap">
<style>
:root {
  color-scheme: light dark;
  --ground: #eceff2;
  --surface: #ffffff;
  --surface-sunk: #f4f6f8;
  --ink: #151a20;
  --ink-muted: #59656f;
  --rule: #d3dae0;
  --rule-strong: #b4bec7;
  --accent: #0c5f5b;
  --pass: #1c6b45;
  --pass-soft: #dcefe4;
  --fail: #9a2626;
  --fail-soft: #f6dedd;
  --sans: "IBM Plex Sans", "Helvetica Neue", Arial, sans-serif;
  --serif: "IBM Plex Serif", Georgia, "Times New Roman", serif;
  --mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #10141a;
    --surface: #181e25;
    --surface-sunk: #131920;
    --ink: #e4eaf0;
    --ink-muted: #95a1ad;
    --rule: #2a333d;
    --rule-strong: #3a4550;
    --accent: #58c2b6;
    --pass: #6fd39c;
    --pass-soft: #142e21;
    --fail: #f0918c;
    --fail-soft: #351a1b;
  }
}
:root[data-theme="dark"] {
  --ground: #10141a;
  --surface: #181e25;
  --surface-sunk: #131920;
  --ink: #e4eaf0;
  --ink-muted: #95a1ad;
  --rule: #2a333d;
  --rule-strong: #3a4550;
  --accent: #58c2b6;
  --pass: #6fd39c;
  --pass-soft: #142e21;
  --fail: #f0918c;
  --fail-soft: #351a1b;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: var(--serif);
  font-size: 16px;
  line-height: 1.6;
  -webkit-text-size-adjust: 100%;
}
main {
  max-width: 62rem;
  margin: 0 auto;
  padding: 3rem 1.25rem 5rem;
  display: flex;
  flex-direction: column;
  gap: 1.75rem;
}
h1, h2, h3, .eyebrow, .chip, dt, th, .verdict { font-family: var(--sans); }
.num, code, .figure-value { font-family: var(--mono); font-variant-numeric: tabular-nums; }

.masthead { border-bottom: 2px solid var(--rule-strong); padding-bottom: 1.5rem; }
.eyebrow {
  margin: 0 0 .5rem; font-size: .72rem; font-weight: 600;
  letter-spacing: .14em; text-transform: uppercase; color: var(--accent);
}
h1 {
  margin: 0; font-size: clamp(1.9rem, 4.4vw, 2.9rem); font-weight: 700;
  letter-spacing: -.02em; text-wrap: balance;
}
.standfirst { margin: .75rem 0 0; max-width: 50ch; font-size: 1.05rem; color: var(--ink-muted); }
.runbar { display: flex; flex-wrap: wrap; gap: .9rem 2.25rem; margin: 1.5rem 0 0; }
.runbar div { margin: 0; }
.runbar dt {
  font-size: .68rem; font-weight: 600; letter-spacing: .12em;
  text-transform: uppercase; color: var(--ink-muted);
}
.runbar dd { margin: .15rem 0 0; font-family: var(--mono); font-size: .95rem; }
.runbar dd.ok { color: var(--pass); font-weight: 600; }
.runbar dd.bad { color: var(--fail); font-weight: 600; }
.verdict {
  margin: 1.25rem 0 0; font-weight: 600; font-size: .82rem;
  letter-spacing: .08em; text-transform: uppercase;
}
.verdict.ok { color: var(--pass); }
.verdict.bad { color: var(--fail); }

.headline h2, .limits h2 {
  margin: 0 0 .9rem; font-size: .75rem; font-weight: 600;
  letter-spacing: .14em; text-transform: uppercase; color: var(--ink-muted);
}
.figures {
  display: grid; gap: 1px; background: var(--rule);
  grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
  border: 1px solid var(--rule);
}
.figure { background: var(--surface); padding: 1rem 1.1rem; display: flex; flex-direction: column; gap: .3rem; }
.figure-label {
  font-family: var(--sans); font-size: .68rem; font-weight: 600;
  letter-spacing: .1em; text-transform: uppercase; color: var(--ink-muted);
}
.figure-value { font-size: 1.3rem; font-weight: 600; letter-spacing: -.01em; }
.figure-note { font-size: .8rem; color: var(--ink-muted); line-height: 1.45; }

.stage {
  background: var(--surface);
  border: 1px solid var(--rule);
  border-left: 3px solid var(--accent);
  padding: 1.5rem 1.5rem 1.25rem;
}
.stage.bad { border-left-color: var(--fail); }
.stage-head { display: flex; align-items: baseline; gap: 1rem; }
.stage-index {
  font-family: var(--mono); font-size: 1.5rem; font-weight: 600;
  color: var(--accent); letter-spacing: -.03em;
}
.stage h2 { margin: 0; font-size: 1.22rem; font-weight: 600; letter-spacing: -.01em; }
.module { margin: .2rem 0 0; font-size: .82rem; color: var(--ink-muted); }
.purpose { margin: .9rem 0 0; max-width: 68ch; color: var(--ink-muted); }
.stage h3 {
  margin: 1.5rem 0 .55rem; font-size: .68rem; font-weight: 600;
  letter-spacing: .13em; text-transform: uppercase; color: var(--ink-muted);
}
.note {
  margin: 1.1rem 0 0; padding: .8rem 1rem; background: var(--surface-sunk);
  border-left: 2px solid var(--rule-strong); font-size: .92rem; max-width: 74ch;
}

.scroller { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: .9rem; }
th {
  text-align: left; font-size: .68rem; font-weight: 600; letter-spacing: .1em;
  text-transform: uppercase; color: var(--ink-muted);
  padding: 0 .9rem .45rem 0; border-bottom: 1px solid var(--rule-strong);
  white-space: nowrap;
}
td { padding: .5rem .9rem .5rem 0; border-bottom: 1px solid var(--rule); vertical-align: top; }
tbody tr:last-child td { border-bottom: none; }
table.io td:first-child, table.checks td:first-child { min-width: 13rem; }
td .num { font-size: .86rem; white-space: nowrap; }
.chip {
  display: inline-block; padding: .1rem .5rem; border-radius: 2px;
  font-size: .66rem; font-weight: 600; letter-spacing: .09em;
}
.chip.pass { background: var(--pass-soft); color: var(--pass); }
.chip.fail { background: var(--fail-soft); color: var(--fail); }

.limits ul { margin: 0; padding-left: 1.1rem; max-width: 74ch; }
.limits li { margin-bottom: .55rem; }
footer { border-top: 1px solid var(--rule); padding-top: 1rem; font-size: .82rem; color: var(--ink-muted); }
code { font-size: .88em; background: var(--surface-sunk); padding: .1em .35em; }
@media (max-width: 40rem) {
  main { padding: 2rem 1rem 3rem; }
  .stage { padding: 1.15rem 1rem 1rem; }
}
</style>
</head>
<body>"""
