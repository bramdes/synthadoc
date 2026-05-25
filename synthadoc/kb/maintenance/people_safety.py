# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""People-page safety detector (spec §3.7).

People pages must stay professional and bounded. Allowed content: role,
project involvement, responsibilities, decisions made, open actions,
professional preferences relevant to collaboration, known ownership.

Forbidden: personality speculation, private/sensitive personal details,
gossip, emotional judgments, irrelevant personal attributes.

This v0.3 detector is **deterministic** — a curated denylist of trigger
phrases per category. False positives are inevitable on a small lexicon,
which is why every match is surfaced (not auto-removed) in
``kb/maintenance/people_pages_flags.md``. The journal flagged an
LLM-driven escalator as a follow-up; today the regex pass is enough to
prove the page-tier guardrail exists and catches the most-common
patterns the spec calls out.

Scans:
* every `*.md` file under ``kb/entities/people/`` recursively
* both ``index.md`` and ``history.md`` (and any other md under a person)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from synthadoc.kb.db import KBDB  # imported for signature symmetry with other jobs
from synthadoc.kb.layout import KBLayout


REPORT_FILENAME = "people_pages_flags.md"

# Each rule is `(category, regex)`. The regex is compiled with IGNORECASE;
# word boundaries (`\b`) keep us from matching substrings of legitimate words.
# Keep the lists deliberately short — easier to expand than to undo a noisy
# false positive that the user has to triage.
_RULES: list[tuple[str, str]] = [
    # spec §3.7 — personality speculation
    ("personality speculation", r"\b(?:seems|appears) (?:to be|like) (?:a |an )?(?:nice|rude|difficult|aggressive|passive|quiet|loud|introvert|extrovert)\b"),
    ("personality speculation", r"\b(?:always|never) (?:listens|responds|cooperates|complains|whines)\b"),
    ("personality speculation", r"\b(?:apparently|seemingly|probably|presumably) (?:is|was|feels|thinks|believes)\b"),

    # spec §3.7 — emotional judgements
    ("emotional judgement",      r"\b(?:rude|lazy|stubborn|abrasive|incompetent|unprofessional|toxic|arrogant)\b"),
    ("emotional judgement",      r"\bdifficult to work with\b"),
    ("emotional judgement",      r"\bnot a team player\b"),

    # spec §3.7 — private / sensitive personal details
    ("private detail",           r"\b(?:spouse|husband|wife|partner|girlfriend|boyfriend|fiance|fiancee)\b"),
    ("private detail",           r"\b(?:children|child|kids|son|daughter|baby)\b"),
    ("private detail",           r"\b(?:religion|religious|church|mosque|synagogue|temple|atheist|christian|muslim|jewish|hindu|buddhist)\b"),
    ("private detail",           r"\b(?:politic[a-z]*|left[- ]wing|right[- ]wing|conservative|liberal|libertarian)\b"),
    ("private detail",           r"\b(?:drinks?|drinking|alcoholic|smoker|smokes|smoking)\b"),
    ("private detail",           r"\b(?:medication|illness|diagnosis|depression|anxiety|disorder|disability)\b"),
    ("private detail",           r"\b(?:home address|phone number|personal email|salary|net worth)\b"),

    # spec §3.7 — irrelevant personal attributes
    ("irrelevant attribute",     r"\b(?:young|old|elderly|youthful|middle-aged)\b"),
    ("irrelevant attribute",     r"\b(?:attractive|good[- ]looking|ugly|short|tall|overweight|skinny|fat)\b"),
    ("irrelevant attribute",     r"\b(?:hometown|nationality|ethnicity|race)\b"),

    # spec §3.7 — gossip
    ("gossip",                   r"\b(?:rumou?r|rumou?red|allegedly|word is that|I heard that)\b"),
    ("gossip",                   r"\bbehind (?:his|her|their) back\b"),
]

_COMPILED = [(cat, re.compile(pat, re.IGNORECASE)) for cat, pat in _RULES]


@dataclass(frozen=True)
class SafetyFlag:
    path: str               # project-root-relative POSIX path
    category: str
    pattern_match: str      # the literal substring that matched
    line: int               # 1-based line number


@dataclass
class PeopleSafetyResult:
    flags: list[SafetyFlag] = field(default_factory=list)
    report_path: Path | None = None
    files_scanned: int = 0

    @property
    def count(self) -> int:
        return len(self.flags)


async def run(db: KBDB, layout: KBLayout) -> PeopleSafetyResult:
    """Scan kb/entities/people/**.md for forbidden patterns. Read-only."""
    flags: list[SafetyFlag] = []
    files_scanned = 0
    people_dir = layout.root / "kb" / "entities" / "people"
    if people_dir.exists():
        for path in sorted(people_dir.rglob("*.md")):
            files_scanned += 1
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            rel = str(path.relative_to(layout.root)).replace("\\", "/")
            flags.extend(_scan_text(rel, text))

    report_path = layout.maintenance_dir / REPORT_FILENAME
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _render(flags, files_scanned), encoding="utf-8", newline="\n",
    )
    return PeopleSafetyResult(
        flags=flags, report_path=report_path, files_scanned=files_scanned,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _scan_text(rel_path: str, text: str) -> list[SafetyFlag]:
    """Scan *text* with every rule. Reports the first match per (line, rule)."""
    flags: list[SafetyFlag] = []
    # Skip the frontmatter block so frontmatter values aren't false-flagged.
    body, body_offset = _strip_frontmatter_with_offset(text)
    for line_no, line in enumerate(body.splitlines(), start=1 + body_offset):
        for category, regex in _COMPILED:
            m = regex.search(line)
            if m:
                flags.append(SafetyFlag(
                    path=rel_path, category=category,
                    pattern_match=m.group(0), line=line_no,
                ))
    return flags


def _strip_frontmatter_with_offset(text: str) -> tuple[str, int]:
    """Return (body, lines_consumed_by_frontmatter)."""
    if not text.startswith("---"):
        return text, 0
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text, 0
    consumed = parts[0].count("\n") + parts[1].count("\n") + 2
    return parts[2].lstrip("\n"), consumed


def _render(flags: list[SafetyFlag], files_scanned: int) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# People Pages — Safety Flags",
        "",
        f"_Generated {ts} · scanned {files_scanned} file(s) · "
        f"{len(flags)} flag(s)_",
        "",
    ]
    if not flags:
        lines.append("_No flagged content._")
        lines.append("")
        return "\n".join(lines)
    lines.append(
        "The detector pattern-matches against spec §3.7 categories "
        "(personality speculation, emotional judgement, private detail, "
        "irrelevant attribute, gossip). Matches are advisory — review each "
        "and remove or rephrase the offending sentence."
    )
    lines.append("")
    # Group by file for human-readable output
    by_file: dict[str, list[SafetyFlag]] = {}
    for f in flags:
        by_file.setdefault(f.path, []).append(f)
    for path in sorted(by_file):
        lines.append(f"## `{path}`")
        lines.append("")
        for f in by_file[path]:
            lines.append(
                f"- line {f.line} — **{f.category}**: "
                f"matched `{f.pattern_match}`"
            )
        lines.append("")
    return "\n".join(lines)
