"""チェッカー出力の解析と、指摘ID・承認の照合。"""

from __future__ import annotations

from codecs import BOM_UTF8
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from ..saved_state import content_hash


_CHECKER_FINDING = re.compile(
    r"(?P<rule>[\w.-]+)\s+(?P<path>.+?):(?P<line>\d+)(?::\d+)?\s+(?P<severity>warning|error):\s*(?P<message>.+)",
    re.IGNORECASE,
)
_CHECKER_FIELDS = ("ファイル", "対処", "内容", "行番号", "コード", "備考")
CHECKER_SUCCESS_MESSAGE = "特に気になる点はありません。"
_CHECKER_FIELD = re.compile(
    r"\s*(?P<field>" + "|".join(r"\s*".join(name) for name in _CHECKER_FIELDS)
    + r")\s*[:：]\s*(?P<value>.*)"
)


def _decode_checker_cp932(raw: bytes) -> str:
    text = raw.decode("cp932")
    # CPython also decodes reserved single bytes to control or private-use characters.
    if invalid := re.search(r"[\u0080\uf8f0-\uf8f3]", text):
        offset = len(text[:invalid.start()].encode("cp932"))
        raise UnicodeDecodeError("cp932", raw, offset, offset + 1, "reserved byte in checker output")
    return text


def decode_checker_output(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        if raw.startswith(BOM_UTF8):
            raise
        return _decode_checker_cp932(raw)


def checker_response_status(log_path: Path) -> str | None:
    with log_path.open("rb") as stream:
        raw = stream.read(65_537)
    if len(raw) > 65_536:
        return "checker_needs_review"
    try:
        text = decode_checker_output(raw).strip()
    except UnicodeDecodeError:
        return "checker_needs_review"
    # ログやJSONの値に含まれた文言を、チェッカー自身の合格判定として扱わない。
    if text == CHECKER_SUCCESS_MESSAGE:
        return "checker_succeeded"
    if re.fullmatch(
        r"(?:error:\s*)?(?:no checks (?:were )?(?:run|performed)|(?:the )?checker (?:did not|could not) run)"
        r"\s*[.!]?",
        text, re.IGNORECASE,
    ):
        return "checker_incomplete"
    return "checker_needs_review" if text else None


def _checker_diagnostics(log_path: Path, encoding: str, *, strict: bool = True) -> Iterator[str]:
    """ファイル欄から次の指摘までをまとめ、コードや備考の改行を残す。"""
    block = []
    with log_path.open("rb") as stream:
        for index, data in enumerate(stream):
            try:
                raw = data.decode("utf-8" if index else "utf-8-sig") if encoding == "utf-8" else _decode_checker_cp932(data)
            except UnicodeDecodeError:
                if strict:
                    raise
                if block:
                    yield "\n".join(block).strip()
                    block = []
                yield "\0"
                continue
            for line in raw.splitlines():
                field = _CHECKER_FIELD.fullmatch(line)
                starts_block = field is not None and "".join(field["field"].split()) == "ファイル"
                if block and (starts_block or _CHECKER_FINDING.fullmatch(line.strip())):
                    yield "\n".join(block).strip()
                    block = []
                if starts_block or block:
                    block.append(line)
                elif line.strip():
                    yield line.strip()
    if block:
        yield "\n".join(block).strip()


def _prefixed_checker_fields(diagnostic: str) -> dict[str, str] | None:
    fields = {}
    current = None
    for line in diagnostic.splitlines():
        match = _CHECKER_FIELD.fullmatch(line)
        if match:
            current = "".join(match["field"].split())
            if current in fields:
                return None
            fields[current] = match["value"]
        elif line.strip():
            if current not in {"対処", "内容", "コード", "備考"}:
                return None
            fields[current] += "\n" + line
        elif current:
            fields[current] += "\n"
    if (set(fields) != set(_CHECKER_FIELDS) or "\0" in diagnostic
            or not fields["ファイル"].strip() or not fields["内容"].strip()
            or not re.fullmatch(r"0*[1-9][0-9]*", fields["行番号"].strip())):
        return None
    return fields


@dataclass(frozen=True)
class CheckerFinding:
    """指摘の表示内容と、修正履歴・承認に使う識別情報を保持する。"""
    finding_id: str
    path: str
    diagnostic: str
    line: str


def checker_findings(log_path: Path, worktree: Path) -> tuple[list[CheckerFinding], bool]:
    try:
        return parse_checker_diagnostics(_checker_diagnostics(log_path, "utf-8"), worktree)
    except UnicodeDecodeError:
        with log_path.open("rb") as stream:
            utf8_bom = stream.read(len(BOM_UTF8)) == BOM_UTF8
        if not utf8_bom:
            try:
                return parse_checker_diagnostics(_checker_diagnostics(log_path, "cp932"), worktree)
            except UnicodeDecodeError:
                pass
        return parse_checker_diagnostics(_checker_diagnostics(log_path, "utf-8", strict=False), worktree)


def parse_checker_diagnostics(diagnostics: Iterable[str], worktree: Path) -> tuple[list[CheckerFinding], bool]:
    worktree = worktree.resolve()
    findings = []
    unrecognized = False
    for diagnostic in diagnostics:
        match = _CHECKER_FINDING.fullmatch(diagnostic)
        fields = None if match else _prefixed_checker_fields(diagnostic)
        if match:
            path_text, line, severity = match["path"], match["line"], match["severity"].lower()
            key = (match["rule"].casefold(), " ".join(match["message"].split()))
        elif fields:
            path_text, line, severity = fields["ファイル"].strip(), fields["行番号"].strip(), "finding"
            key = ("prefixed", *(" ".join(fields[name].split()) for name in ("対処", "内容", "備考")),
                   fields["コード"].strip())
        else:
            unrecognized = True
            continue
        if "\0" in path_text:
            unrecognized = True
            continue
        path = Path(path_text.replace("\\", "/"))
        try:
            resolved = (worktree / path).resolve()
        except (OSError, RuntimeError, ValueError):
            unrecognized = True
            continue
        if not resolved.is_relative_to(worktree):
            unrecognized = True
            continue
        relative = resolved.relative_to(worktree).as_posix()
        # 行・列のずれでは変えず、対象や重大度の異なる指摘とは区別する。
        review_key = (*key, relative, severity)
        finding_id = hashlib.sha256(json.dumps(review_key).encode("utf-8")).hexdigest()
        findings.append(CheckerFinding(finding_id, relative, diagnostic, line))
    return findings, unrecognized or not findings


def accepted_findings(record: dict[str, Any], findings: list[CheckerFinding], hashes: dict[str, str | None]) -> set[str]:
    """指摘IDと対象ファイルの版がともに一致する承認だけを返す。"""
    reviews = record.get("checker_reviews")
    if not isinstance(reviews, list):
        return set()
    accepted = {
        (entry["finding_id"], entry["target_sha256"]) for entry in reviews
        if isinstance(entry, dict) and content_hash(entry.get("finding_id"))
        and content_hash(entry.get("target_sha256")) and isinstance(entry.get("reason"), str) and entry["reason"].strip()
    }
    counts = Counter(finding.finding_id for finding in findings)
    # 同じIDが複数ある場合は承認対象を一意に特定できないため、承認済みとみなさない。
    return {
        finding.finding_id for finding in findings
        if counts[finding.finding_id] == 1 and (finding.finding_id, hashes.get(finding.path)) in accepted
    }
