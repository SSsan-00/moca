from __future__ import annotations

from pathlib import Path
from typing import Any


def navigation_plan(
    manifest: dict[str, Any], record: dict[str, Any], stage: str, targets: tuple[Path, ...],
    *, current: int | None = None, diagnostic: Path | None = None,
) -> tuple[dict[str, Any], str]:
    through = len(manifest["parts"]) if current is None else current
    if current is None:
        ranges = [{"role": "repair-reference", "first_capsule": 1, "last_capsule": through,
                   "reason": "reconcile diagnostics against all saved excerpts",
                   "file_pattern": ".migration-materials/source-NNNN.md"}]
    else:
        ranges = [{"role": "current", "first_capsule": current, "last_capsule": current,
                   "reason": "check assigned behavior", "file_pattern": ".migration-context.md"}]
        if current > 1:
            ranges.append({"role": "prior", "first_capsule": 1, "last_capsule": current - 1,
                           "reason": "reconcile earlier behavior without reassigning it",
                           "file_pattern": ".migration-materials/source-NNNN.md"})
    declared_symbols = []
    if current is not None:
        for source in manifest["parts"][current - 1].get("sources", []):
            symbol = source.get("symbol")
            if isinstance(symbol, str) and symbol not in declared_symbols:
                declared_symbols.append(symbol)
            if len(declared_symbols) == 12:
                break
    diagnosis = diagnostic.name if diagnostic is not None else None
    return {
        "version": 1, "unit_id": record.get("unit_id", manifest.get("unit_id")), "stage": stage,
        "through_capsule": through, "sources": ranges,
        "targets": [{"path": path.as_posix(), "recorded_sha256": record.get("target_hashes", {}).get(path.as_posix())}
                    for path in targets],
        "rules_record": "supplied_rules",
        "declared_symbol_hints": declared_symbols,
        "diagnostic": diagnosis,
        "unclassified_policy": "Use the original staged inputs when no feature relationship has been established.",
    }, (
        f"Context navigation ({stage}): "
        + (f"start from the diagnostics in `{diagnosis}`; check the complete saved source sequence when related. "
           if diagnosis else
           "review the currently assigned source; use earlier excerpts only to reconcile prior behavior. ")
        + "Use the supplied full mandatory and selected rules. Unclassified or absent index entries do not exclude evidence. "
    )
