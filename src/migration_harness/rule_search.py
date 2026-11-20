from __future__ import annotations

import hashlib
import json
import re
from io import StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_STORED, BadZipFile, ZipFile

from .execution import check_cancelled, report_activity
from .files import sha256_file

MODEL_FILES = {
    "model.onnx": "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88",
    "tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
}
VERSION = 1
MAX_ROWS = 32_768
VECTOR_SIZE = 384
MAX_METADATA = 4 * 1024 * 1024
MAX_LINKS = 3
LINE = re.compile(r"^ *(\d+) \| ([\s\S]*)$")
HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*)")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)")
LINK = re.compile(r"\]\(<?([^()\s>#]+\.(?:md|markdown|txt))(?:#[^()\s>]*)?>?\)", re.I)


class SearchError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def normalize_rule_search(raw, config_path, project, units):
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("model_dir"), str) or not raw["model_dir"].strip():
        raise SearchError("[rule_search].model_dir にローカルモデルのフォルダーを指定してください。")
    model_dir = (config_path.parent / Path(raw["model_dir"]).expanduser()).resolve()
    root = Path(project["project_root"])
    if model_dir.is_relative_to(root):
        raise SearchError("[rule_search].model_dir はアプリリポジトリの外に配置してください。")
    by_unit = {unit["id"]: unit for unit in units}
    configured = raw.get("units")
    if not isinstance(configured, list) or not configured:
        raise SearchError("[[rule_search.units]] に検索範囲を明示してください。")
    scopes = {}
    for item in configured:
        unit_id = item.get("unit") if isinstance(item, dict) else None
        if not isinstance(unit_id, str) or unit_id not in by_unit or unit_id in scopes:
            raise SearchError("検索範囲には存在する移行単位を重複なく指定してください。")
        query, paths = item.get("query", ""), item.get("references")
        if not isinstance(query, str) or len(query) > 500 or "\0" in query:
            raise SearchError("検索語は500文字までの文字列にしてください。")
        if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not p.strip() or "\0" in p for p in paths):
            raise SearchError("検索範囲には詳細ルールのファイルを指定してください。")
        normalized = []
        for value in paths:
            path = (root / Path(value).expanduser()).resolve()
            normalized.append((path.relative_to(root) if path.is_relative_to(root) else path).as_posix())
        if len(normalized) != len(set(normalized)):
            raise SearchError("検索範囲のファイルが重複しています。")
        # 旧版を指したままなら設定全体を止めず、この検索だけをフォールバックする。
        scopes[unit_id] = {"query": query.strip(), "references": normalized}
    return {"model_dir": str(model_dir), "scopes": scopes}


def _identity(config, unit):
    return _digest([str(config["project_root"]), unit["id"], sorted(unit.get("targets", [unit["target"]]))])


def _index_path(config, unit):
    return Path(config["run_root"]) / ".rule-search" / (_identity(config, unit) + ".zip")


def _indexed_items(scope, rule_sources):
    indexed = {}
    for item, text in rule_sources:
        path = item["path"]
        if path not in scope["references"]:
            continue
        if path in indexed or item.get("usage") != "reference":
            raise SearchError("検索範囲の詳細ルールが重複、または必須ルールに変わっています。")
        indexed[path] = (item, text)
    if set(indexed) != set(scope["references"]):
        raise SearchError("検索範囲に登録されていない詳細ルールがあります。")
    return indexed


def _manifest(config, unit, indexed):
    return {
        "version": VERSION, "model": MODEL_FILES, "scope": _identity(config, unit),
        "references": {
            path: [hashlib.sha256(text.encode()).hexdigest(), item["description"]]
            for path, (item, text) in sorted(indexed.items())
        },
    }


def _segments(rendered):
    start = end = None
    heading = ""
    body = []
    fence = None
    previous_heading = ""
    chars = 0
    for physical, line in enumerate(StringIO(rendered, newline=""), 1):
        match = LINE.fullmatch(line)
        if not match:
            continue
        raw = match[2]
        marker = FENCE.match(raw)
        title = HEADING.match(raw) if fence is None and marker is None else None
        if body and (title or chars + len(raw) > 500 or len(body) >= 12):
            yield start, end, heading, "".join(body)
            body, chars = [], 0
        if not body:
            start = physical
            heading = previous_heading
        if title:
            heading = previous_heading = title[1].strip()[:120]
        if marker:
            tag = marker[1]
            if fence is None:
                fence = tag
            elif tag[0] == fence[0] and len(tag) >= len(fence) and not marker[2].strip():
                fence = None
        body.append(raw)
        chars += len(raw)
        end = physical
    if body:
        yield start, end, heading, "".join(body)


class _Embedder:
    def __init__(self, directory):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        for name, checksum in MODEL_FILES.items():
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file() or sha256_file(path) != checksum:
                raise SearchError("検索モデルまたはトークナイザーが未配置、または指定版と一致しません。")
        try:
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            self.session = ort.InferenceSession(str(Path(directory) / "model.onnx"), options, providers=["CPUExecutionProvider"])
            self.names = [item.name for item in self.session.get_inputs()]
            self.tokenizer = Tokenizer.from_file(str(Path(directory) / "tokenizer.json"))
            self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
            self.tokenizer.enable_truncation(max_length=512, stride=64)
        except Exception as exc:
            raise SearchError("ローカル検索モデルを読み込めませんでした。") from exc

    def encode(self, texts):
        np = self.np
        try:
            for start in range(0, len(texts), 16):
                check_cancelled()
                encoded = self.tokenizer.encode_batch(texts[start:start + 16])
                arrays = [(parent, window) for parent, entry in enumerate(encoded) for window in (entry, *entry.overflowing)]
                for offset in range(0, len(arrays), 16):
                    batch = arrays[offset:offset + 16]
                    width = max(len(window.ids) for _, window in batch)
                    ids = np.asarray([window.ids + [1] * (width - len(window.ids)) for _, window in batch], dtype="int64")
                    masks = np.asarray([window.attention_mask + [0] * (width - len(window.ids))
                                        for _, window in batch], dtype="int64")
                    tensors = {"input_ids": ids, "attention_mask": masks, "token_type_ids": np.zeros_like(ids)}
                    output = self.session.run(None, {name: tensors[name] for name in self.names})[0]
                    if output.ndim == 3:
                        output = (output * masks[..., None]).sum(axis=1) / masks.sum(axis=1)[:, None]
                    output = output / np.linalg.norm(output, axis=1, keepdims=True)
                    for (parent, _), vector in zip(batch, output, strict=True):
                        if vector.shape != (VECTOR_SIZE,) or not np.isfinite(vector).all():
                            raise SearchError("検索モデルの出力が不正です。")
                        yield start + parent, vector.astype("<f4")
        except SearchError:
            raise
        except Exception as exc:
            raise SearchError("ローカル検索モデルの推論に失敗しました。") from exc


def build_rule_index(config, unit, rule_sources, *, embedder_factory=_Embedder):
    scope = config.get("rule_search", {}).get("scopes", {}).get(unit["id"])
    if scope is None:
        raise SearchError("この移行単位の検索範囲が設定されていません。")
    indexed = _indexed_items(scope, rule_sources)
    manifest = _manifest(config, unit, indexed)
    embedder = embedder_factory(config["rule_search"]["model_dir"])
    passages, ranges = [], []
    for path, (item, text) in sorted(indexed.items()):
        for low, high, title, body in _segments(text):
            if body.strip():
                if len(body) > 128_000 or len(ranges) >= MAX_ROWS:
                    raise SearchError("検索対象が索引の上限を超えました。従来の参照は利用できます。")
                passages.append(f"passage: {item['description'][:200]}\n{title}\n{body}")
                ranges.append([path, low, high])
    if not ranges:
        raise SearchError("索引へ登録する本文がありません。")
    path = _index_path(config, unit)
    if path.parent.is_symlink() or path.is_symlink():
        raise SearchError("索引にはシンボリックリンクを使えません。")
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        staging = Path(tmp.name)
    rows = []
    try:
        with ZipFile(staging, "w", compression=ZIP_STORED) as destination, destination.open("vectors", "w") as out:
            # 512トークンを超える単一行も捨てず、同じ原文行へ複数の窓を紐付ける。
            for parent, vector in embedder.encode(passages):
                if len(rows) >= MAX_ROWS:
                    raise SearchError("検索対象が索引の上限を超えました。従来の参照は利用できます。")
                rows.append(ranges[parent])
                out.write(vector.tobytes())
        metadata = json.dumps({**manifest, "rows": rows}, ensure_ascii=False, separators=(",", ":")).encode()
        if len(metadata) > MAX_METADATA:
            raise SearchError("索引のメタデータが上限を超えました。")
        with ZipFile(staging, "a", compression=ZIP_STORED) as destination:
            destination.writestr("manifest.json", metadata)
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)
    return path, len(rows)


def _load_index(path, expected):
    if path.parent.is_symlink() or path.is_symlink() or path.stat().st_size > MAX_METADATA + MAX_ROWS * VECTOR_SIZE * 4 + 4096:
        raise SearchError("索引の形式または容量が不正です。")
    with ZipFile(path) as archive:
        if sorted(archive.namelist()) != ["manifest.json", "vectors"]:
            raise SearchError("索引の構成が不正です。")
        meta = archive.getinfo("manifest.json")
        vectors = archive.getinfo("vectors")
        if meta.file_size > MAX_METADATA or any(info.compress_type != ZIP_STORED for info in (meta, vectors)):
            raise SearchError("索引の形式または容量が不正です。")
        manifest = json.loads(archive.read(meta))
        if not isinstance(manifest, dict):
            raise SearchError("索引のメタデータが不正です。")
        rows = manifest.pop("rows", None)
        if manifest != expected:
            raise SearchError("ルール・範囲・モデルの版に索引が一致しません。")
        if (not isinstance(rows, list) or not 0 < len(rows) <= MAX_ROWS
                or vectors.file_size != len(rows) * VECTOR_SIZE * 4):
            raise SearchError("索引の行情報が不正です。")
        return rows, archive.read(vectors)


def _linked_candidates(path, body, candidates, by_path):
    result = []
    for link in LINK.findall(body):
        # 暗黙の別名や外部URIは辿らず、実在確認も固定した索引の登録情報だけで行う。
        if ":" in link or "\\" in link or link.startswith("/"):
            continue
        key = (Path(path).parent / link).as_posix()
        if ".." in Path(key).parts:
            import posixpath
            key = posixpath.normpath(key)
        if key != path and key in candidates and key not in result and key in by_path:
            result.append(key)
        if len(result) == MAX_LINKS:
            break
    return result


def prepare_rule_hint(config, unit, rule_sources, rule_files, *, embedder_factory=_Embedder):
    scope = config.get("rule_search", {}).get("scopes", {}).get(unit["id"])
    if scope is None:
        return None
    try:
        return _prepare_rule_hint(config, unit, scope, rule_sources, rule_files, embedder_factory)
    except SearchError as exc:
        reason = str(exc)
    except FileNotFoundError:
        reason = "索引またはモデルが未配置です。"
    except ImportError:
        reason = "ローカル検索の追加依存がありません。"
    except (OSError, ValueError, RuntimeError, BadZipFile):
        reason = "検索索引が読み取れないか、検索処理に失敗しました。"
    report_activity(f"{reason} 従来のルール参照を使用します。")
    return None


def _prepare_rule_hint(config, unit, scope, rule_sources, rule_files, embedder_factory):
    query = scope["query"] or " ".join([unit["id"], *unit.get("targets", [unit["target"]]),
                                        *(item.get("symbol", "") for item in unit["inputs"] if not item.get("shared"))])
    indexed = _indexed_items(scope, rule_sources)
    rows, vectors = _load_index(_index_path(config, unit), _manifest(config, unit, indexed))
    valid = {path: {(low, high): body for low, high, _, body in _segments(text)}
             for path, (_, text) in indexed.items()}
    if any(not isinstance(row, list) or len(row) != 3 or not isinstance(row[0], str) or row[0] not in valid
           or any(type(bound) is not int for bound in row[1:]) or tuple(row[1:]) not in valid[row[0]] for row in rows):
        raise SearchError("索引に固定原文と一致しない行があります。")
    by_path = {entry["path"]: entry for entry in rule_files if entry["usage"] == "reference"}
    if any(path not in by_path or hashlib.sha256(text.encode()).hexdigest() != by_path[path]["sha256"]
           for path, (_, text) in indexed.items()):
        raise SearchError("索引と保存資料が一致しません。")
    embedder = embedder_factory(config["rule_search"]["model_dir"])
    np = embedder.np
    matrix = np.frombuffer(vectors, dtype="<f4").reshape(len(rows), VECTOR_SIZE)
    if not np.isfinite(matrix).all() or not np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-3):
        raise SearchError("索引のベクトルが不正です。")
    query_vector = next(embedder.encode(["query: " + query[:500]]))[1]
    path, low, high = rows[int(np.argmax(matrix @ query_vector))]
    links = _linked_candidates(path, valid[path][(low, high)], indexed, by_path)
    return {
        "file": by_path[path]["file"], "sha256": by_path[path]["sha256"], "lines": [low, high],
        "related": [{key: by_path[target][key] for key in ("file", "sha256")} for target in links],
    }


def review_hint(manifest, line_counts):
    hint = manifest.get("semantic_rule_hint")
    if not isinstance(hint, dict):
        return ""
    entries = {entry["file"]: entry["sha256"] for entry in manifest.get("rule_files", ())
               if entry["usage"] == "reference"}
    file, bounds = hint.get("file"), hint.get("lines")
    if (not isinstance(file, str) or file not in line_counts or entries.get(file) != hint.get("sha256")
            or not isinstance(bounds, list) or len(bounds) != 2 or any(type(value) is not int for value in bounds)
            or not 1 <= bounds[0] <= bounds[1] <= line_counts[file]):
        return ""
    related = hint.get("related", [])
    if not isinstance(related, list) or len(related) > MAX_LINKS:
        return ""
    if any(not isinstance(entry, dict) or not isinstance(entry.get("file"), str)
           or entry["file"] not in line_counts or entries.get(entry["file"]) != entry.get("sha256") for entry in related):
        return ""
    linked_text = (" Check the explicit links to " + ", ".join("details/" + entry["file"] for entry in related)
                   + " where relevant.") if related else ""
    return (
        f" Optional semantic navigation only: inspect details/{file}, physical lines {bounds[0]}–{bounds[1]} if relevant."
        + linked_text + " Read the original conditions, exceptions and precedence before relying on it."
        " This suggestion is not evidence of applicability; it neither replaces the full inventory nor supports exclusion. "
    )
