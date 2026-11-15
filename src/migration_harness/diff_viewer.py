from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from migration_harness.workflow import (
    _input_encoding, _load_capsule_manifest, _load_saved_run, _pinned_php_sources, _read_pinned_php,
    worktree_source_lock,
)


def _inside_file(root: Path, relative: str) -> Path:
    """相対パスが指定ディレクトリ内の既存ファイルを指すことを確認する。"""
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"比較に必要なファイルを参照できません: {relative}")
    return path


def _pane(files: list[tuple[str, str]]) -> tuple[str, list[dict[str, Any]]]:
    """複数ファイルを連結し、Viewerが元のファイルと行へ戻れる境界情報を作る。"""
    parts = []
    segments = []
    start = 1
    for index, (name, text) in enumerate(files, 1):
        ends_with_newline = text.endswith("\n")
        last = index == len(files)
        # Viewerに合わせ、途中のファイルの末尾改行は次ファイルとの境界として扱う。
        line_count = max(1, text.count("\n") + (last or not ends_with_newline))
        segments.append({
            "fileName": name, "fileIndex": index, "startLine": start,
            "lineCount": line_count, "endsWithNewline": ends_with_newline,
        })
        parts.append(text if last or ends_with_newline else text + "\n")
        start += line_count
    return "".join(parts), segments


def capture_workspace(
    config: dict[str, Any], run_id: str, *, focus: dict[str, Any] | None = None,
    target_hashes: dict[str, str] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Runの参照PHPと移行先をそろえて読み、比較時点の内容を固定する。"""
    run_path, worktree, record, unit = _load_saved_run(config, run_id)
    if record.get("target_apply_pending"):
        raise ValueError("移行先の反映が途中です。同じRun IDで再開してから比較してください。")
    capsule_path, manifest = _load_capsule_manifest(run_path)
    sources = _pinned_php_sources(manifest)
    legacy_paths = []
    if sources is None:
        # 保存済みPHPを持たない旧Runでは、manifestのパスと文字コードで現在の入力を読む。
        project = Path(config["project_root"])
        encodings: dict[str, str] = {}
        for part in manifest["parts"]:
            for source in part["sources"]:
                if Path(source["path"]).suffix.lower() == ".php":
                    encodings.setdefault(source["path"], source["encoding"])
        legacy_paths = [(_inside_file(project, name), name, encoding) for name, encoding in encodings.items()]
    if not (sources or legacy_paths):
        raise ValueError("この実行の入力に参照PHPがありません。")
    target = Path(unit["target"]).as_posix()
    if "targets" in unit:
        targets = unit["targets"]
    elif target.lower().endswith(".cshtml.cs"):
        targets = [target, target[:-3]]
    elif target.lower().endswith(".cshtml"):
        targets = [target + ".cs", target]
    else:
        targets = [target]
    right_paths = {name: _inside_file(worktree, name) for name in targets}
    paths = [path for path, _, _ in legacy_paths] + list(right_paths.values())
    with worktree_source_lock:
        before = [path.stat() for path in paths]
        left_files = [
            (Path(name).as_posix(), path.read_text(encoding=_input_encoding(path, encoding)))
            for path, name, encoding in legacy_paths
        ]
        for source in sources or []:
            text = _read_pinned_php(capsule_path, source).decode(source["encoding"])
            left_files.append((Path(source["path"]).as_posix(), text.replace("\r\n", "\n").replace("\r", "\n")))
        right_files = []
        for name, path in right_paths.items():
            content = path.read_bytes()
            right_files.append((name, content.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")))
            if target_hashes is not None:
                target_hashes[name] = hashlib.sha256(content).hexdigest()
        # 外部のエディターと競合した場合も、不揃いなスナップショットを保存しない。
        after = [path.stat() for path in paths]
    if any(
        (old.st_ino, old.st_size, old.st_mtime_ns) != (new.st_ino, new.st_size, new.st_mtime_ns)
        for old, new in zip(before, after, strict=True)
    ):
        raise ValueError("取得中にソースが変更されました。もう一度お試しください。")
    left_text, left_segments = _pane(left_files)
    right_text, right_segments = _pane(right_files)
    payload = {
        "kind": "diff-viewer-workspace",
        "version": 1,
        "snapshot": {"runId": run_id, "capturedAt": datetime.now(timezone.utc).isoformat()},
        "workspace": {
            "name": unit["id"].strip().encode("utf-16-le")[:50].decode("utf-16-le", errors="ignore"),
            "leftText": left_text, "rightText": right_text,
            "leftSegments": left_segments, "rightSegments": right_segments,
            "leftActiveFile": left_files[0][0], "rightActiveFile": right_files[0][0],
            "anchors": {"manualAnchors": [], "staleManualAnchors": []},
        },
    }
    if isinstance(focus, dict):
        name, line = focus.get("path"), focus.get("line")
        if isinstance(name, str) and type(line) is int:
            for segment in right_segments:
                if Path(segment["fileName"]) == Path(name) and 1 <= line <= segment["lineCount"]:
                    # ファイル内の行番号を連結した右ペインの座標へ変換する。
                    payload["workspace"]["rightActiveFile"] = segment["fileName"]
                    payload["workspace"]["rightCursor"] = {
                        "lineNumber": segment["startLine"] + line - 1, "column": 1,
                    }
                    break
    return run_path, payload


def save_workspace(run_path: Path, payload: dict[str, Any]) -> str:
    """過去の比較と区別できるIDを発行し、ワークスペースを上書きせずに保存する。"""
    snapshot_id = uuid.uuid4().hex
    directory = run_path / "review-snapshots"
    directory.mkdir(exist_ok=True)
    if directory.is_symlink() or not directory.resolve().is_relative_to(run_path):
        raise ValueError("スナップショットの保存先を確認してください。")
    with (directory / f"{snapshot_id}.json").open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
    return snapshot_id


def create_snapshot(config: dict[str, Any], run_id: str, *, focus: dict[str, Any] | None = None) -> str:
    """アンカー生成を伴わない通常比較用のワークスペースを保存する。"""
    return save_workspace(*capture_workspace(config, run_id, focus=focus))


def snapshot_bytes(config: dict[str, Any], run_id: str, snapshot_id: str) -> bytes:
    """比較IDから当該Runに属する保存済みJSONだけを読み出す。"""
    if re.fullmatch(r"[a-f0-9]{32}", snapshot_id) is None:
        raise ValueError("スナップショットIDが無効です。")
    run_path, _, _, _ = _load_saved_run(config, run_id)
    content = _inside_file(run_path, f"review-snapshots/{snapshot_id}.json").read_bytes()
    payload = json.loads(content)
    snapshot = payload.get("snapshot") if isinstance(payload, dict) else None
    if not isinstance(snapshot, dict) or snapshot.get("runId") != run_id:
        raise ValueError("スナップショットのRun IDが指定されたRunと一致しません。")
    return content


def review_html(config: dict[str, Any], workspace: bytes) -> bytes:
    """既存ViewerのJSON取込に比較データと初期表示位置を渡す。"""
    if not (viewer_html := config.get("diff_viewer_html")):
        raise ValueError("設定の差分ビューアーのHTMLを指定してください。")
    path = _inside_file(Path(config["project_root"]), viewer_html)
    html = path.read_bytes()
    closing = list(re.finditer(rb"</body\s*>", html, flags=re.IGNORECASE))
    if not closing or b"workspace-import-file" not in html:
        raise ValueError("設定したHTMLにはDiff Viewerのワークスペース取込機能がありません。")
    # ソースの文字列をHTMLとして実行せず、Viewerの通常のJSON取込に渡す。
    payload = workspace.replace(b"<", b"\\u003c")
    # 取込でカーソルが復元されても画面外に残るため、カード生成後にViewerの行ジャンプを使う。
    bootstrap = b"""
<script id="harness-review-workspace" type="application/json">""" + payload + b"""</script>
<script>
window.addEventListener("load", () => {
  const input = document.querySelector("#workspace-import-file");
  const payload = document.querySelector("#harness-review-workspace").textContent;
  const {rightCursor, rightActiveFile, rightSegments} = JSON.parse(payload).workspace;
  const segment = rightCursor && rightSegments?.find(item => item.fileName === rightActiveFile);
  const cards = document.querySelector("#right-file-cards");
  const gotoLine = document.querySelector("#right-goto-line-input");
  if (segment && cards && gotoLine) {
    const observer = new MutationObserver(() => {
      const card = Array.from(cards.querySelectorAll("button[data-file]"))
        .find(button => button.dataset.file === rightActiveFile);
      if (!card) return;
      observer.disconnect();
      requestAnimationFrame(() => requestAnimationFrame(() => {
        card.click();
        document.activeElement?.dispatchEvent(new KeyboardEvent("keydown", {
          key: "g", code: "KeyG", ctrlKey: true, bubbles: true, cancelable: true
        }));
        if (document.activeElement !== gotoLine) return;
        gotoLine.value = String(rightCursor.lineNumber - segment.startLine + 1);
        gotoLine.dispatchEvent(new KeyboardEvent("keydown", {
          key: "Enter", bubbles: true, cancelable: true
        }));
      }));
    });
    observer.observe(cards, {childList: true});
    setTimeout(() => observer.disconnect(), 10000);
  }
  const transfer = new DataTransfer();
  transfer.items.add(new File([payload], "review-workspace.json", {type: "application/json"}));
  input.files = transfer.files;
  input.dispatchEvent(new Event("change", {bubbles: true}));
}, {once: true});
</script>
"""
    offset = closing[-1].start()
    return html[:offset] + bootstrap + html[offset:]
