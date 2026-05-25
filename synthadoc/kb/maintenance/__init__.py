# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Layer 3 maintenance jobs (plan §9 Layer 3, spec §4.10).

Each module exposes a single ``run(db, layout)`` coroutine that:

* Reads from ``kb.db`` (pure SQL — no LLM calls for v0.3).
* Writes a markdown report under ``kb/maintenance/<name>.md``.
* Returns a result dataclass with counts the aggregator can roll up.

The aggregator :func:`run_all` chains them and emits the periodic
``kb_health.md`` snapshot plus a timestamped report under
``kb/maintenance/reports/``.
"""
