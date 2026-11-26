from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
import io
import json
from pathlib import Path
import re

from ..config import target_paths
from ..context.capsules import is_php_input, load_capsule_manifest, pinned_php_sources, read_pinned_php
from ..context.php_boundaries import static_source_indicators
from ..files import safe_target
from ..verification import roslyn_syntax
from .run_records import load_saved_run
from .worktree_targets import worktree_source_lock


_SOURCE_BYTES = 4 * 1024 * 1024
_TARGET_BYTES = 2 * 1024 * 1024
_TOTAL_BYTES = 8 * 1024 * 1024
_PLAN_BYTES = 256 * 1024
_MAX_RANGES = 4096
_MAX_TARGETS = 24
_MAX_CLAIMS = 256
_HASH = re.compile(r"[0-9a-f]{64}")
_LINES = re.compile(r"\r\n|[\r\n\x85\u2028\u2029]")
_FACTS = {"method", "type", "property", "const", "literal"}
_STATUSES = ("verified", "mismatch", "review_candidate", "unverified", "excluded")
_ACTIVE = {"junie_running", "editor_followup", "repairing", "building", "checking"}


@dataclass(frozen=True)
class Target:
    sha256: str
    lines: int
    reason: str
    complete: bool
    facts: tuple[dict, ...]


def migration_evidence(config: dict, run_id: str) -> dict:
    run_path, worktree, record, unit = load_saved_run(config, run_id)
    try:
        capsule, manifest = load_capsule_manifest(run_path)
        rows = _source_ranges(manifest)
    except (KeyError, OSError, TypeError, ValueError):
        return _report(run_id, [], "固定原文の保存情報を読み取れません。")
    if not rows or len(rows) > _MAX_RANGES:
        return _report(run_id, [], "検査できる原文範囲がないか、範囲数の上限を超えています。", range_count=len(rows))

    allowed = tuple(path.as_posix() for path in target_paths(unit))
    try:
        plan = _load_plan(config, unit, set(allowed))
        plan_error = None
    except (KeyError, OSError, TypeError, ValueError):
        plan, plan_error = {}, "静的証跡の設定ファイルが無効です。形式とファイルの配置を確認してください。"
    sources = _read_sources(capsule, manifest, {row["path"] for row in rows})
    pending = record.get("target_apply_pending") or record.get("status") in _ACTIVE
    completed = record.get("completed_capsules")
    completed = completed if type(completed) is int else 0
    mappings = record.get("migration_mappings")
    mappings = mappings if isinstance(mappings, dict) else {}
    plan_targets = {target["path"] for claims in plan.values() for claim in claims for target in claim.get("targets", ())}
    required_targets = {target["path"] for claims in plan.values() for claim in claims if claim["kind"] == "require"
                        for target in claim["targets"]}
    anchor_targets = {mapping.get("path") for mapping in mappings.values() if isinstance(mapping, dict)
                      and isinstance(mapping.get("path"), str)}
    requested = [path for path in allowed if path in plan_targets or path in anchor_targets]
    requested.sort(key=lambda path: path not in required_targets)
    can_scan = not pending and plan_error is None and any(
        row["part"] <= completed and not row["reference"] and row["path"] in sources for row in rows
    )
    targets = _analyze_targets(worktree, requested, required_targets) if can_scan and requested else {}
    for row in rows:
        unknown_source = False
        data = sources.get(row["path"])
        if data is not None:
            digest, max_line, indicators, lines, functions, starts, structural = data
            if 1 <= row["start_line"] <= row["end_line"] <= max_line:
                row["source_sha256"] = digest
                start = bisect_left(lines, row["start_line"])
                end = bisect_right(lines, row["end_line"])
                fn_start = max(0, bisect_right(starts, row["start_line"]) - 1)
                fn_end = bisect_right(starts, row["end_line"])
                overlapping = [fn for fn in functions[fn_start:fn_end] if row["start_line"] <= fn["end_line"]]
                row["structure"] = (overlapping + indicators[start:end])[:32]
                unknown_source = not structural or any(item["kind"] == "dynamic" for item in indicators[start:end])
                if unknown_source:
                    row["reasons"].append("動的処理または未解析部分の意味は確認できません。")
        if not row["source_sha256"]:
            row["reasons"].append("固定原文の内容・版または範囲を確認できません。")
        elif row["reference"]:
            row["status"] = "excluded"
            row["reasons"].append("移行ではなく参考資料として登録された範囲です。")
        elif pending or row["part"] > completed:
            row["reasons"].append("移行先への反映・この区間の実行が完了してから確認してください。")
        elif plan_error:
            row["reasons"].append(plan_error)
        else:
            _assess_row(row, plan.get(row["id"], ()), mappings, targets)
            if unknown_source and row["status"] == "verified":
                row["status"] = "unverified"
        del row["reference"], row["part"]

    known = {row["id"] for row in rows}
    unmatched = len(plan.keys() - known)
    if unmatched:
        plan_error = "現在のRunにない範囲IDを指定した明示条件があります。その条件は判定していません。"
    return _report(run_id, rows, plan_error)


def _report(run_id, rows, reason=None, *, range_count=None):
    counts = Counter(row["status"] for row in rows)
    return {
        "run_id": run_id, "status": "ready" if rows and reason is None else "unverified",
        "ranges": rows, "range_count": len(rows) if range_count is None else range_count,
        "summary": dict.fromkeys(_STATUSES, 0) | counts,
        **({"reason": reason} if reason else {}),
    }


def _source_ranges(manifest):
    assigned = [(f"{part['index']}:{index}", part["index"], source, False)
                for part in manifest["parts"] for index, source in enumerate(part.get("sources", ()), 1)]
    references = [*manifest.get("shared_sources", ()), *manifest.get("reference_sources", ())]
    rows = []
    for identifier, part, source, reference in [
        *assigned, *((f"reference:{i}", 0, row, True) for i, row in enumerate(references, 1)),
    ]:
        if not is_php_input(source):
            continue
        start, end = source["start_line"], source["end_line"]
        if type(start) is not int or type(end) is not int or not 1 <= start <= end:
            raise ValueError("invalid saved range")
        rows.append({"id": identifier, "path": source["path"], "start_line": start, "end_line": end,
                     "part": part, "source_sha256": None, "reference": reference or source.get("role") == "reference_php",
                     "character_slices": source.get("character_slices", []),
                     "status": "unverified", "reasons": [], "targets": [], "structure": [],
                     **({"symbol": source["symbol"]} if isinstance(source.get("symbol"), str) else {})})
    return rows


def _read_sources(capsule, manifest, paths):
    result = {}
    budget = _TOTAL_BYTES
    try:
        pins = pinned_php_sources(manifest)
    except ValueError:
        return result
    syntax = manifest.get("syntax_boundaries", {})
    for pin in pins or ():
        path = pin["path"]
        if path not in paths or not budget:
            continue
        try:
            raw = read_pinned_php(capsule, pin, max_bytes=min(_SOURCE_BYTES, budget))
            text = raw.decode(pin["encoding"])
        except (LookupError, OSError, ValueError):
            continue
        budget -= len(raw)
        max_line = sum(1 for _ in io.StringIO(text, newline=""))
        indicators, scanned = static_source_indicators(text)
        saved = syntax.get(path, {}) if isinstance(syntax, dict) else {}
        functions = saved.get("functions", []) if isinstance(saved, dict) else []
        lexical_functions = [
            {"kind": "function", "value": item["name"], "start_line": item["start_line"], "end_line": item["end_line"]}
            for item in functions if isinstance(item, dict) and isinstance(item.get("name"), str)
            and type(item.get("start_line")) is int and type(item.get("end_line")) is int
            and 1 <= item["start_line"] <= item["end_line"] <= max_line
        ] if isinstance(functions, list) else []
        lexical_functions.sort(key=lambda item: item["start_line"])
        result[path] = (pin["sha256"], max_line, list(indicators), [item["start_line"] for item in indicators],
                        lexical_functions, [item["start_line"] for item in lexical_functions], scanned)
    return result


def _bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("file exceeds the evidence limit")
    return raw


def _digest(value):
    return isinstance(value, str) and bool(_HASH.fullmatch(value))


def _line_pair(start, end):
    return type(start) is int and type(end) is int and 1 <= start <= end


def _load_plan(config, unit, allowed):
    if not (name := unit.get("static_evidence")):
        return {}
    path = safe_target(Path(config["project_root"]), Path(name))
    plan = json.loads(_bounded(path, _PLAN_BYTES))
    if (not isinstance(plan, dict) or set(plan) != {"version", "entries"} or type(plan["version"]) is not int
            or plan["version"] != 1 or not isinstance(plan["entries"], list) or len(plan["entries"]) > _MAX_CLAIMS):
        raise ValueError("invalid evidence plan")
    result = defaultdict(list)
    for claim in plan["entries"]:
        if not isinstance(claim, dict) or claim.get("kind") not in {"mapping", "exclude", "require"}:
            raise ValueError("invalid evidence claim")
        kind = claim["kind"]
        fields = {"kind", "source", "reason"} | ({"targets", "origin"} if kind == "mapping"
                 else {"targets", "fact"} if kind == "require" else set())
        if set(claim) != fields or not isinstance(claim["reason"], str) or not 1 <= len(claim["reason"].strip()) <= 500:
            raise ValueError("invalid claim fields or reason")
        source = claim["source"]
        if (not isinstance(source, dict) or set(source) != {"id", "path", "start_line", "end_line", "sha256"}
                or not isinstance(source["id"], str) or not isinstance(source["path"], str)
                or not _line_pair(source["start_line"], source["end_line"]) or not _digest(source["sha256"])):
            raise ValueError("invalid evidence source")
        if kind == "mapping" and claim["origin"] not in {"human", "ai"}:
            raise ValueError("invalid mapping provenance")
        if kind == "require":
            fact = claim["fact"]
            if (not isinstance(fact, dict) or set(fact) != {"kind", "value"} or fact["kind"] not in _FACTS
                    or not isinstance(fact["value"], str) or not 1 <= len(fact["value"]) <= 512):
                raise ValueError("invalid required fact")
        if kind != "exclude":
            scopes = claim["targets"]
            if not isinstance(scopes, list) or not 1 <= len(scopes) <= 12:
                raise ValueError("invalid evidence targets")
            for target in scopes:
                if (not isinstance(target, dict) or set(target) not in
                        ({"path"}, {"path", "start_line", "end_line", "sha256"})
                        or not isinstance(target["path"], str) or target["path"] not in allowed):
                    raise ValueError("evidence target is not in the Run")
                if "sha256" in target and (not _digest(target["sha256"]) or not _line_pair(target["start_line"], target["end_line"])):
                    raise ValueError("invalid target range or version")
                if kind == "mapping" and "sha256" not in target:
                    raise ValueError("a mapping must bind target version and range")
        existing = result[source["id"]]
        if existing and (kind == "exclude" or existing[0]["kind"] == "exclude"):
            raise ValueError("a range cannot be both excluded and assessed")
        existing.append(claim)
    return result


def _analyze_targets(worktree, allowed, required):
    metadata, requests = {}, []
    budget = _TOTAL_BYTES
    with worktree_source_lock(worktree):
        for name in allowed[:_MAX_TARGETS]:
            relative = Path(name)
            if not budget or relative.suffix.lower() not in {".cs", ".cshtml"}:
                continue
            try:
                raw = _bounded(safe_target(worktree, relative), min(budget, _TARGET_BYTES))
                text = raw.decode("utf-8-sig")
            except (OSError, ValueError):
                continue
            budget -= len(raw)
            if not text.strip() or "\0" in text:
                continue
            metadata[name] = (sha256(raw).hexdigest(), len(_LINES.split(text)))
            if name in required:
                requests.append({"Path": name, "Text": text})
    reports = {}
    if requests:
        try:
            results = roslyn_syntax.collect_static_evidence(requests)
            if len(results) == len(requests):
                reports = {request["Path"]: report for request, report in zip(requests, results, strict=True)}
        except (OSError, ValueError):
            pass
    return {name: _parsed_target(meta, reports.get(name, {})) for name, meta in metadata.items()}


def _parsed_target(metadata, report):
    return Target(*metadata, report.get("reason", "analyzer_unavailable"), report.get("complete") is True,
                  tuple(report.get("facts", ())))


def _scope(spec, targets, origin):
    path = spec["path"]
    target = targets.get(path)
    if target is None:
        return None
    start, end = spec.get("start_line", 1), spec.get("end_line", target.lines)
    if not _line_pair(start, end) or end > target.lines or ("sha256" in spec and target.sha256 != spec["sha256"]):
        return None
    return {"path": path, "start_line": start, "end_line": end, "sha256": target.sha256, "origin": origin}


def _assess_row(row, claims, mappings, targets):
    id_, source_index = row["id"].split(":")
    anchor = mappings.get(f"{id_}:{int(source_index) - 1}")
    if isinstance(anchor, dict) and isinstance(anchor.get("path"), str) and "sha256" in anchor:
        if evidence := _scope(anchor, targets, "anchor"):
            row["targets"].append(evidence)
            row["reasons"].append("アンカーに基づく対応候補です。意味の一致は確認していません。")
        else:
            row["reasons"].append("保存アンカーの移行先は現行の版または範囲と一致しません。")
    outcomes = []
    for claim in claims:
        selected = claim["source"]
        if row["character_slices"] or any(row[key] != selected[key] for key in ("path", "start_line", "end_line")
                                           ) or row["source_sha256"] != selected["sha256"]:
            row["reasons"].append("明示条件の原文版・範囲を確認できません（文字単位の分割も対象外）。")
            outcomes.append("unverified")
        elif claim["kind"] == "exclude":
            row["status"] = "excluded"
            row["targets"].clear()
            row["reasons"] = [claim["reason"]]
            return
        elif claim["kind"] == "mapping":
            scopes = [_scope(spec, targets, claim["origin"]) for spec in claim["targets"]]
            if any(scope is None for scope in scopes):
                row["reasons"].append("対応指定の移行先版・範囲が変わったか確認できません。")
                outcomes.append("unverified")
            else:
                row["targets"].extend(scopes)
                row["reasons"].append(claim["reason"])
                outcomes.append("review_candidate")
        else:
            outcomes.append(_check_requirement(row, claim, targets))
    if "mismatch" in outcomes:
        row["status"] = "mismatch"
    elif "unverified" in outcomes:
        row["status"] = "unverified"
    elif "verified" in outcomes:
        row["status"] = "verified"
    elif row["targets"]:
        row["status"] = "review_candidate"
    elif not claims:
        row["reasons"].append("根拠のある移行先の範囲が指定されていません。欠落とは断定しません。")
    row["targets"] = [dict(items) for items in dict.fromkeys(tuple(scope.items()) for scope in row["targets"])]


def _check_requirement(row, claim, targets):
    scopes = [_scope(spec, targets, "mechanical") for spec in claim["targets"]]
    expected = claim["fact"]
    uncertain = False
    for scope in scopes:
        if scope is None:
            uncertain = True
            continue
        target = targets[scope["path"]]
        row["targets"].append(scope)
        mapped = target.facts if target.complete or target.reason == "razor_context_unverified" else ()
        for fact in mapped:
            if (fact["kind"] == expected["kind"] and fact["value"] == expected["value"]
                    and scope["start_line"] <= fact["start_line"] <= fact["end_line"] <= scope["end_line"]):
                row["targets"][-1] = {**scope, "start_line": fact["start_line"], "end_line": fact["end_line"]}
                row["reasons"].append(f"明示条件の構文上の一致を確認: {claim['reason']}（意味同値は未確認）")
                return "verified"
        uncertain |= not target.complete
    if uncertain:
        row["reasons"].append(f"対象版、構文解析または参照が不十分で条件を判定できません: {claim['reason']}")
        return "unverified"
    row["reasons"].append(f"指定範囲に必要な宣言・リテラルがありません: {claim['reason']}")
    return "mismatch"
