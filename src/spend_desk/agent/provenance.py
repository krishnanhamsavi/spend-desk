"""Number provenance guard.

Every number in AI generated text must trace to the output of a tool call from the same run.
The guard extracts each number from the draft, works out how precisely it was written, and
looks for a matching value in the outputs of the tool calls cited on the same line ([T1] style).

What is not checked, by design and stated in the README:
  dates, ISO weeks, fiscal labels (Q3, FY27), and identifiers such as AN008 or R03
  small whole numbers from 0 to 10, which the memo uses for counts and list positions
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|"
    "December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
_STRIP = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),  # ISO dates
    re.compile(r"\b\d{4}-W\d{1,2}\b", re.I),  # ISO weeks
    re.compile(rf"\b(?:{MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*\d{{4}})?", re.I),
    re.compile(rf"\b\d{{1,2}}\s+(?:{MONTHS})\b(?:\s+\d{{4}})?", re.I),
    re.compile(r"\b(?:week|wk)\s+\d{1,2}\b", re.I),
    re.compile(r"\bQ[1-4]\b|\bFY\s?\d{2,4}\b", re.I),
    re.compile(r"\b(?:19|20)\d{2}\b"),  # bare years
    re.compile(r"\[T\d+\]"),  # citations
    re.compile(r"\b[A-Z]+[A-Z0-9_]*\d+[A-Z0-9_]*\b"),  # ids like AN008, R03
]
_NUM = re.compile(
    r"(?<![\w.])(?P<neg>-)?\$?(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<dec>\.\d+)?"
    r"\s?(?P<suf>[kKmM](?![a-zA-Z])|%|percent\b|x\b)?"
)
_CITE = re.compile(r"\[(T\d+)\]")
_DASH = re.compile(r"[–—]|\s-\s|\s--\s")


@dataclass
class Found:
    text: str
    value: float
    tolerance: float
    is_pct: bool


@dataclass
class GuardResult:
    passed: bool
    numbers_checked: int
    unmatched: list[dict] = field(default_factory=list)
    uncited_lines: list[str] = field(default_factory=list)
    style_violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def feedback(self) -> str:
        parts = []
        if self.unmatched:
            parts.append(
                "These numbers do not appear in the outputs of the tool calls cited on "
                "the same line: "
                + "; ".join(f"'{u['text']}' in: {u['line'][:120]}" for u in self.unmatched)
                + ". Quote numbers exactly as a cited tool returned them. Do not add, "
                "subtract, convert, or round further."
            )
        if self.uncited_lines:
            parts.append(
                "These lines contain numbers but no [T#] citation: "
                + " | ".join(line[:100] for line in self.uncited_lines)
            )
        if self.style_violations:
            parts.append(
                "Do not use em dashes, en dashes, or hyphens as connectors. Rewrite "
                "with full stops or commas: " + " | ".join(self.style_violations[:5])
            )
        return " ".join(parts)


def extract_numbers(line: str) -> list[Found]:
    clean = line
    for pat in _STRIP:
        clean = pat.sub(" ", clean)
    out = []
    for m in _NUM.finditer(clean):
        whole = m.group("num").replace(",", "")
        dec = m.group("dec") or ""
        value = float(whole + dec)
        suf = (m.group("suf") or "").lower()
        mult = {"k": 1e3, "m": 1e6}.get(suf, 1.0)
        decimals = len(dec) - 1 if dec else 0
        tol = 0.5 * 10 ** (-decimals) * mult
        value *= mult
        if m.group("neg"):
            value = -value
        is_pct = suf in ("%", "percent")
        if not is_pct and suf != "x" and mult == 1 and float(whole + dec) <= 10 and not dec:
            continue  # small counts and list positions
        out.append(Found(m.group(0).strip(), value, tol, is_pct))
    return out


def tool_values(output) -> list[float]:
    vals = []

    def walk(x):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            vals.append(float(x))
        elif isinstance(x, str):
            for f in extract_numbers(x):
                vals.append(f.value)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(output)
    return vals


def _matches(f: Found, values: list[float]) -> bool:
    for v in values:
        for cand in (v, -v, abs(v)):
            if abs(f.value - cand) <= max(f.tolerance, 1e-9) + 1e-9:
                return True
            # A percent the tool reports as a ratio, e.g. 0.958 quoted as 95.8%.
            if f.is_pct and abs(f.value - cand * 100) <= f.tolerance + 1e-9:
                return True
    return False


def check(text: str, calls: dict[str, dict]) -> GuardResult:
    """calls maps citation ids (T1, T2...) to that call's output."""
    unmatched, uncited, style = [], [], []
    n = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        if _DASH.search(line):
            style.append(line.strip()[:120])
        found = extract_numbers(line)
        if not found:
            continue
        n += len(found)
        cited = _CITE.findall(line)
        if not cited:
            uncited.append(line.strip())
            continue
        values = [v for c in cited if c in calls for v in tool_values(calls[c])]
        for f in found:
            if not _matches(f, values):
                unmatched.append(
                    {"text": f.text, "value": f.value, "line": line.strip(), "cited": cited}
                )
    return GuardResult(not unmatched and not uncited, n, unmatched, uncited, style)


def log_record(run_id: str, attempt: int, result: GuardResult, kind: str) -> str:
    return json.dumps({"run_id": run_id, "attempt": attempt, "kind": kind, **result.to_dict()})
