"""One row per finding, for spreadsheets."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from ..models import Finding

COLUMNS = ["severity", "category", "finding_id", "url", "message", "evidence", "fix"]


def write_csv(path: str | Path, findings: Iterable[Finding], *, target: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for finding in findings:
            writer.writerow({
                "severity": finding.severity.value,
                "category": finding.category,
                "finding_id": finding.id,
                "url": finding.url or target,
                "message": finding.message,
                "evidence": finding.evidence,
                "fix": finding.fix,
            })
    return path
