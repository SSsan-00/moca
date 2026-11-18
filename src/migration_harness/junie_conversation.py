"""Runごとの自然文の受付、確認、Junieと検証工程への振り分け。"""

from __future__ import annotations

import re
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread

from .config import checker_command_for_unit, target_paths
from .editor_queue import editor_queue, editor_text
from .errors import format_error
from .files import safe_target
from .run_records import load_saved_run, validate_run_id
from .worktree_targets import review_target_hash

PENDING = {"received", "clarification", "waiting", "running", "steering"}
REBUILD = {"build", "build_repair", "checker", "checker_repair"}
UNCONFIRMED_BUILD = "先行ビルドの終了を確認できません。ログと保存版を確認して指示を送信し直してください。"
QUERY_INTENTS = {"status", "build_status", "checker_status"}
BUILD_PHASES = {"building", "build_succeeded", "build_failed"}
BUILD_TERM = r"(?:ビルド|\b(?:re)?build\b)"
CHECKER_TERM = r"(?:チェッカー|\bchecker\b|チェック(?=まで|を(?:再|もう|実行)))"
REQUEST_ENDING = r"(?:ください|(?:もらえ|くれ|いただけ)ますか\s*\??)?"
VERIFICATION_REQUEST = (
    rf"(?:{BUILD_TERM}|{CHECKER_TERM})(?:を|は)?\s*"
    r"(?:(?:だけ|のみ|まで|も)(?:を)?\s*)?"
    rf"(?:して{REQUEST_ENDING}|(?:再)?実行して{REQUEST_ENDING}|やり直して{REQUEST_ENDING}|"
    r"お願い(?:します)?|(?:もう一(?:度|回)|再度)(?:実行して(?:ください)?|お願い(?:します)?)?)"
    r"(?=[ \t]*[、,。.!！\n]|\s*$)"
    r"|(?:^|[、,。.!！])\s*(?:please\s+)?(?:rebuild|build|(?:run|rerun)\s+(?:the\s+)?checker)"
    r"(?:\s+again)?\s*(?=[、,。.!！]|$)"
)
CONDITIONAL_VERIFICATION = re.compile(
    r"(?:^|[。.!?！\n])\s*(?P<condition>[^。.!?、,\n]+?(?:たら|なら|れば|場合(?:は|に)?|次第|てから|(?:た|の|終了|完了)後(?:に)?))"
    r"[、,\s]*(?P<action>.+)", re.DOTALL,
)
BUILD_COMPLETION_CONDITION = re.compile(
    rf"(?:もし\s*)?(?:(?:今|現在)の|(?:現在)?実行中の)?\s*{BUILD_TERM}(?:が|の|は)?\s*"
    r"(?:終わったら|終わってから|(?:終了|完了)(?:したら|してから|(?:した)?後(?:に)?|次第)|終わった後(?:に)?)",
)
STOP_REQUEST = re.compile(
    r"\s*(?:今すぐ\s*|すぐ\s*|現在の(?:処理|作業)を\s*|処理を\s*)?"
    r"(?:止めて|停止して|中止して)(?:ください)?[。.!！\s]*|\s*(?:please\s+)?stop(?:\s+now)?[。.!！\s]*",
)
FORBID_REPAIR = re.compile(
    r"(?:修正|変更|編集)(?:は|を)?\s*(?:してはいけ|してはなら|してほしく|しない|せず|するな|禁止)"
    r"|\b(?:do not|don't|without|never)\s+(?:fix|edit|modify|repair)\b",
)
REPAIR_REQUEST = re.compile(r"(?<!やり)直して|修正して|合わせて|同じ.*(?:にして|使って)|既定値にして|\bfix\b")
CHECKER_FINDINGS = re.compile(r"指摘|チェッカーの(?:出力|結果|エラー)|checker (?:findings|output|results?)")
BUILD_FAILURE_REPAIR = re.compile(
    r"(?:失敗|エラー|通らなかっ|失敗し|エラーがあ)(?:したら|たら|なら|れば|ったら|の場合|場合)"
    r"[^。.!！\n]*(?:直して|修正して)|\bif (?:it|the build) fails[,]?\s+(?:fix|repair)\b",
)
PHASES = {
    "queued": "準備を開始しています", "preparing": "準備中", "cancelled": "待機取消済み",
    "preparation_failed": "準備失敗", "preparation_interrupted": "準備中断",
    "prepared": "準備完了", "junie_running": "移行中", "editor_followup": "追加指示を反映中",
    "editor_checkpoint": "編集の確認待ち", "repairing": "自動修正中", "building": "ビルド中",
    "checking": "チェッカー実行中", "build_succeeded": "ビルド成功", "build_failed": "ビルド失敗",
    "checker_succeeded": "チェッカー成功", "checker_accepted": "チェッカー確認済み",
    "checker_needs_review": "チェッカー指摘の確認待ち", "checker_failed": "チェッカー失敗",
    "interrupted": "停止中", "junie_finished": "移行完了", "junie_failed": "移行の確認待ち",
    "script_running": "設定済みスクリプトの実行中", "script_failed": "設定済みスクリプトの失敗",
}


def _answer_kind(text):
    """利用者の文面から回答種別を判定する。"""
    value = unicodedata.normalize("NFKC", text).lower()
    if re.fullmatch(r"\s*(?:チェッカー(?:まで|も)?|チェックまで|checker)[。.!！\s]*", value):
        return "checker"
    if re.fullmatch(r"\s*(?:ビルド(?:だけ|のみ)?|build)[。.!！\s]*", value):
        return "build"
    if re.fullmatch(r"\s*(?:取消|取り消し|キャンセル|cancel)[。.!！\s]*", value):
        return "cancel"
    return None


def _clarification(history):
    """確認質問への回答内容を抽出する。"""
    return next((entry for entry in reversed(history) if entry["status"] == "clarification"), None)


def _junie_option(request, text):
    """Junieの確認選択肢に対応する回答を抽出する。"""
    return next((choice for choice in request["options"] if text in (choice["label"], choice["id"])), None)


def _conditional_verification(value):
    """先行ビルド完了を条件とする検証依頼を判定する。"""
    match = CONDITIONAL_VERIFICATION.search(value)
    if match and re.search(rf"{BUILD_TERM}|{CHECKER_TERM}", match[0]):
        return match
    return None


def _status_intent(value):
    """保存済み状態の照会依頼か判定する。"""
    if (_conditional_verification(value) or STOP_REQUEST.fullmatch(value)
            or re.search(VERIFICATION_REQUEST, value)):
        return None
    checker = re.search(CHECKER_TERM, value)
    if (checker or re.search(BUILD_TERM, value)) and (
        re.search(r"なぜ|どうして|原因|結果|ログ|状態|状況|進捗|\b(?:why|status|results?|logs?)\b", value)
        or re.search(r"終わった|終えた|落ちた|通った|済み|実行中|かどうか|(?:は|って)(?:どう|まだ)\s*\?"
                     r"|(?:ビルド|チェッカー)(?:は|が|を)?\s*(?:した|しました|回った)(?:の|か)?\s*[?。]*$", value)
        or re.search(r"(?:[?]|のですか|ましたか|したの\s*$|したか\s*$)", value)
        and re.search(r"成功|失敗|完了|終了|実行した|停止|終わ|\b(?:ran|done|finish\w*|pass\w*|fail\w*|succeed\w*|running)\b", value)
    ):
        return "checker_status" if checker else "build_status"
    if re.search(r"(?:今|現在).*(?:何して|何をして|状態|状況|工程|進捗)|^(?:状態|状況|進捗)[は?。\s]*$|what (?:are you doing|is happening)", value):
        return "status"
    return None


def _stage_resume_options(text):
    """開始工程と区間を明示した依頼だけを再開計画の引数へ変換する。"""
    value = unicodedata.normalize("NFKC", text).strip().rstrip("。.!！")
    if re.fullmatch(r"手修正したので[、 ]*ビルド(?:だけ|のみ)(?:を)?(?:再実行|実行|再開)?(?:して|してください)?", value):
        return {"stage": "build"}
    if re.fullmatch(r"チェッカー(?:のみ|だけ|から)(?:を)?(?:再開|再実行|再確認)(?:して|してください)?", value):
        return {"stage": "checker"}
    match = re.fullmatch(
        r"(?:区間|カプセル)\s*([1-9][0-9]{0,5})\s*(だけ|のみ|から)"
        r"(?:を)?(?:やり直|再移行)(?:す|して|してください)?", value,
    )
    if match:
        index = int(match[1])
        return {"stage": "migration", "rewind_to": index,
                "through_capsule": None if match[2] == "から" else index}
    match = re.fullmatch(r"移行の続きから(?:区間|カプセル)\s*([1-9][0-9]{0,5})\s*まで(?:再開)?", value)
    if match:
        return {"stage": "migration", "through_capsule": int(match[1])}
    if re.fullmatch(r"移行の続きから(?:再開(?:して|してください)?)?", value):
        return {"stage": "migration"}
    return None


def _intent(text, phase):
    """会話文を処理可能な操作意図へ分類する。"""
    value = unicodedata.normalize("NFKC", text).lower()
    if status := _status_intent(value):
        return status
    if re.search(r"(?:シェル|ターミナル|shell|powershell|業務php|sql|データベース|\bdb\b).*(?:を実行|に接続|実行して|run |execute )", value):
        return "unsupported"
    if STOP_REQUEST.fullmatch(value):
        return "stop"
    if not _conditional_verification(value) and re.search(
        r"差分.*(?:だけ|のみ).*(?:確認|見る)|(?:残り|他).*(?:触らず|触らない).*差分", value,
    ):
        if not re.search(VERIFICATION_REQUEST, value) and not re.search(r"(?:止めて|停止して|中止して)", value):
            return "diff"
    if re.search(r"(?:ビルド|チェッカー|停止|build|checker).*(?:しないで|しない|せず|しなくて|するな)|\b(?:don't|do not|without) (?:build|run|stop)\b", value):
        return "clarify"
    repair_on_failure = bool(BUILD_FAILURE_REPAIR.search(value))
    if (condition := _conditional_verification(value)) and not repair_on_failure:
        action = condition["action"]
        if BUILD_COMPLETION_CONDITION.fullmatch(condition["condition"]) and not _conditional_verification(action):
            intent = _intent(action, phase)
            if intent in REBUILD:
                return f"after_build_{intent}"
        if (re.search(CHECKER_TERM, condition["condition"]) and re.search(REPAIR_REQUEST, condition["condition"])
                and _intent(action, phase) == "checker" and not FORBID_REPAIR.search(value)):
            return "checker_repair"
        return "clarify"
    stop = bool(re.search(r"止め|停止|中止|\bstop\b", value))
    retry = bool(re.search(r"やり直|再実行|(?:もう|再度|もう一度).*?(?:一回|一度|実行|お願い)|rebuild|rerun", value))
    build = bool(re.search(BUILD_TERM, value))
    checker = bool(re.search(CHECKER_TERM, value))
    forbidden = bool(FORBID_REPAIR.search(value))
    repair = not forbidden and bool(REPAIR_REQUEST.search(value))
    if (build or checker or stop or retry) and re.search(
        r"\?|(?:ですか|ますか|でしょうか|していい)[。\s]*$", re.sub(VERIFICATION_REQUEST, "", value),
    ):
        return "clarify"
    if stop:
        if phase in BUILD_PHASES and not checker and (
            re.search(r"(?:止めて|(?:停止|中止)して).*(?:やり直して|再ビルドして|再実行して)", value)
            or re.fullmatch(r"(?:please )?stop (?:the )?(?:current )?build and rebuild[.!]?", value)
        ):
            return ("stop_build_repair" if repair_on_failure else "clarify") if repair else "stop_build"
        return "clarify"
    if build or checker:
        if re.search(r"\b(?:if|unless|when|after)\b", value) and not repair_on_failure:
            return "clarify"
        requests = re.findall(VERIFICATION_REQUEST, value)
        if not requests:
            return "clarify"
        if any(re.search(CHECKER_TERM, request) for request in requests):
            if repair:
                return "checker_repair" if CHECKER_FINDINGS.search(value) and not repair_on_failure else "clarify"
            return "checker"
        if repair:
            return "build_repair" if repair_on_failure else "clarify"
        return "build"
    if retry or re.fullmatch(r"\s*(?:もう一回|もう一度|再度)(?:お願い|お願いします)?[。.!！\s]*", value):
        return "clarify"
    return "junie"


def target_versions(worktree, unit):
    """編集依頼が対象としたファイル版を返す。"""
    return {path.as_posix(): review_target_hash(worktree, path) for path in target_paths(unit)}


def saved_edits(queue):
    """保存済みの利用者編集を対象別にまとめる。"""
    return {name: edit["id"] for name, edit in queue["edits"].items()}


def _finish(entry, status, message):
    """会話依頼の結果を履歴へ確定する。"""
    entry.update(status=status, message=message)
    entry["finished_at"] = datetime.now(timezone.utc).isoformat()


def _queue_steering(queue, entry, timing):
    """保存した会話を同じトランザクションでJunieへの配信待ちにする。"""
    queue["prompts"].append({"id": entry["id"], "text": entry["text"], "status": "queued"})
    message = {
        "immediate": "実行中の Junie に追加指示を渡します。",
        "repair_restart": "修正を区切り、保存した編集と指示を合流します。",
        "checkpoint": "現在の作業区間の終了後に Junie に渡します。",
        "resume": "同じ Run を再開したときに Junie に渡します。",
    }[timing]
    entry.update(status="steering", intent="junie", message=message, plan=message)


def _phase_message(state, phase):
    """工程状態に対応する利用者向け説明を返す。"""
    current = "実行待ち" if state.get("queued") else PHASES.get(phase, "状況を確認中")
    if phase == "script_running" and state.get("script_phase") == "before_prepare":
        current = "準備中（準備前スクリプトを実行中）"
    return f"現在の工程は「{current}」です。"


def _preparation_reply(entry):
    """準備中に受け付けた照会へ返信する。"""
    phase = entry["phase"]
    intent = _intent(entry["text"], phase)
    if intent not in QUERY_INTENTS:
        return {**entry, "status": "failed", "message": "準備前や準備中は状況の確認ができます。作業の指示は準備完了後に送ってください。"}
    message = entry["phase_message"]
    if intent != "status":
        message += " 保存済みの結果はありません。" if not entry["has_worktree"] else " エディタが表示されたら、結果をもう一度問い合わせてください。"
    return {**entry, "status": "done", "message": message}


def invalidate_queued_steering(queue):
    """後から保存した版に、まだ渡していない指示を黙って適用しない。"""
    for entry in queue.get("conversation", []):
        if entry["status"] != "steering":
            continue
        prompt = next((p for p in queue["prompts"] if p["id"] == entry["id"] and p["status"] == "queued"), None)
        if prompt is not None:
            queue["prompts"].remove(prompt)
            _finish(entry, "failed", "保存版が更新されました。エディタで確認して指示を送信し直してください。")


def _diagnostic(run_path, kind):
    """工程結果から会話表示用の診断を作る。"""
    from .workflow import _review_log

    text, truncated = _review_log(run_path, f"{kind}.log")
    return {"kind": kind, "text": text[-8_000:], "truncated": truncated or len(text) > 8_000} if text else None



def _existing_receipt(history, identifier, text):
    """同じ指示の再送を返し、同一IDへの別の本文を拒否する。"""
    for entry in history:
        if entry["id"] == identifier:
            if entry["text"] != text:
                raise ValueError("同じ指示 ID がすでに使われています。")
            return entry
    return None


class JunieConversations:
    def __init__(self, manager):
        """RunManagerに結び付く会話履歴とスレッド状態を初期化する。"""
        self.manager = manager
        self.wake = Event()
        self.runs = set()
        self.cancelled = {}
        self.thread = None

    def start(self):
        """会話履歴の監視・処理スレッドを開始する。"""
        root = self.manager.root
        if root and self.thread is None:
            self.scan()
            self.thread = Thread(target=self.dispatch, name="junie-conversations", daemon=True)
            self.thread.start()

    def scan(self):
        """保存済み履歴から未処理依頼を見つける。"""
        self.runs.update(path.parent.name for path in self.manager.queue_path.parent.glob("*/editor.json"))
        self.runs.update(self.manager.blocked_conversations)
        self.wake.set()

    @contextmanager
    def _preparation_history(self, run_id, state, *, create=False):
        """作業ディレクトリを準備する処理から独立して、工程照会の履歴を保存する。"""
        validate_run_id(run_id)
        root = Path(self.manager.config["run_root"]).resolve()
        path = safe_target(root, Path(".preparation-conversations") / (run_id + ".json"))
        if not create and not path.is_file():
            yield []
            return
        safe_target(root, path.with_suffix(".lock").relative_to(root))
        path.parent.mkdir(parents=True, exist_ok=True)
        with editor_queue(path.parent, name=path.name) as queue:
            owner = [self.manager.config["project_root"], state["unit_id"]]
            if queue.get("owner", owner) != owner:
                raise ValueError("照会履歴が現在のプロジェクトと一致しません。")
            history = queue.get("conversation", [])
            yield history
            if create and history:
                queue.update(owner=owner, conversation=history)

    def preparation_replies(self, run_id, state):
        """準備中に保存した照会の回答を読み出す。"""
        with self.manager.condition, self._preparation_history(run_id, state) as history:
            return [_preparation_reply(entry) for entry in history]

    def submit(self, run_id, action):
        """受理内容を永続化して返し、HTTP応答後に呼び出し元が実行を通知できるようにする。"""
        from .workflow import plan_stage_resume

        identifier = action.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("指示 ID が無効です。画面から送信し直してください。")
        text = editor_text(action.get("text"), 4_000).strip()
        if not text:
            raise ValueError("指示を入力してください。")
        with self.manager.condition:
            state = self.manager.get(run_id).state
            early = not state.get("worktree_path") or action.get("status_only") is True
            with self._preparation_history(run_id, state, create=early) as history:
                if (existing := _existing_receipt(history, identifier, text)) is not None:
                    return existing
                if early:
                    phase = state["status"]
                    entry = {
                        "id": identifier, "text": text, "status": "received", "status_only": True,
                        "received_at": datetime.now(timezone.utc).isoformat(), "phase": phase,
                        "phase_message": _phase_message(state, phase), "has_worktree": bool(state.get("worktree_path")),
                    }
                    history.append(entry)
                    return entry
        run_path, worktree, _, unit = load_saved_run(self.manager.config, run_id)
        stage_plan, stage_error = None, None
        if options := _stage_resume_options(text):
            try:
                if not self.manager.get(run_id).state.get("ready"):
                    raise ValueError("処理が終わってから再開方法を指定してください。")
                stage_plan = plan_stage_resume(self.manager.config, run_id, **options)
            except (OSError, ValueError) as exc:
                stage_error = str(exc)
        with editor_queue(run_path) as queue, self.manager.condition:
            return self._submit_saved(run_id, run_path, worktree, unit, queue, identifier, text,
                                      options, stage_plan, stage_error)

    def _submit_saved(self, run_id, run_path, worktree, unit, queue, identifier, text,
                      options, stage_plan, stage_error):
        """キューとRunManagerのロック内で照合し、対象版を記録して指示を受け付ける。"""
        from .dashboard import _prompt_timing

        history = queue.setdefault("conversation", [])
        if (existing := _existing_receipt(history, identifier, text)) is not None:
            return existing
        entry = {
            "id": identifier, "text": text, "status": "received",
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        value = unicodedata.normalize("NFKC", text).lower()
        if query := _status_intent(value):
            entry["query_intent"] = query
        else:
            if run_id in self.manager.blocked_conversations:
                raise ValueError(self.manager.blocked_conversations[run_id])
            view = self.manager.get(run_id)
            state = view.state
            _, _, record, _ = load_saved_run(self.manager.config, run_id)
            intent = _intent(text, record["status"])
            if options:
                if not stage_plan or not state.get("ready") or state.get("junie_request"):
                    _finish(entry, "failed", stage_error or "処理が終わってから再開方法を指定してください。")
                else:
                    labels = {"build": "ビルドのみ", "checker": "チェッカーのみ", "migration": "移行"}
                    span = stage_plan.get("capsule_range")
                    scope = f" 区間 {span[0]}〜{span[1]}" if span else ""
                    repair = " 検証時に自動修正あり。" if stage_plan["repair"] else " 自動修正なし。"
                    entry.update(status="waiting", intent="resume_stage", stage_request=stage_plan,
                                 plan=f"{labels[stage_plan['start_stage']]}{scope} · 保存版 {stage_plan['version'][:12]}。{repair}")
                history.append(entry)
                return entry
            if re.search(r"(?:区間|カプセル).*(?:再移行|やり直|再開)|チェッカーから.*再開", value):
                _finish(entry, "failed", "開始区間と終了区間はどこですか？ 区間番号を添えて送るか、実行一覧の「再開方法…」で指定してください。")
                history.append(entry)
                return entry
            if intent == "stop":
                if view.request_stop({"operation_id": state.get("operation_id")}):
                    for pending in history:
                        if pending["status"] not in {"received", "waiting"} or pending.get("query_intent"):
                            continue
                        prior = pending.get("intent") or _intent(pending["text"], record["status"])
                        if prior.removeprefix("stop_").removeprefix("after_build_") in REBUILD or prior == "resume_stage":
                            _finish(pending, "paused", "停止要求により検証を保留しました。実行する場合は改めて指示してください。")
                    _finish(entry, "done", "停止を要求しました。処理の終了は進捗で確認できます。")
                else:
                    _finish(entry, "failed", "停止できる実行中の処理がありません。")
                history.append(entry)
                return entry
            if _conditional_verification(value):
                entry["predecessor_operation_id"] = self.manager.jobs.get(run_id, {}).get("operation_id")
            request = state.get("junie_request")
            if request:
                entry["junie_question"] = request["id"]
            if request and (option := _junie_option(request, text)) is not None:
                entry["junie_option"] = option["id"]
            elif _answer_kind(text) and (question := _clarification(history)):
                entry["clarification_id"] = question["id"]
            elif sum(old["status"] in PENDING and not (
                "query_intent" in old or "junie_option" in old or "clarification_id" in old
            ) for old in history) >= 16:
                raise ValueError("処理待ちの指示が最大件数に達しました。先に確認待ちの指示に回答してください。")
            entry.update(edit_revision=queue.get("edit_revision"), edits=saved_edits(queue),
                         targets=target_versions(worktree, unit))
        history.append(entry)
        receipt = entry.copy()
        if ("query_intent" not in entry and not request and not _clarification(history)
                and intent == "junie" and record["status"] in {"junie_running", "editor_checkpoint", "editor_followup"}
                and not state.get("ready")):
            timing = _prompt_timing(self.manager.config, state, run_path, record,
                                    pending_edits=bool(queue["edits"]),
                                    live=bool(view.control and view.control.junie_steering))
            if timing in {"checkpoint", "immediate"}:
                if sum(prompt["status"] != "sent" for prompt in queue["prompts"]) >= 16:
                    raise ValueError("送信待ちの指示は最大16件です。")
                _queue_steering(queue, entry, timing)
        return receipt

    def notify(self, run_id):
        """Run更新を会話処理スレッドへ通知する。"""
        with self.manager.condition:
            if self.manager.closing:
                return
            self.runs.add(run_id)
            self.start()
            self.wake.set()

    def dispatch(self):
        """保留依頼の保存とRunManagerへの登録を行う。"""
        while not self.manager.closing:
            self.wake.wait(.2)
            self.wake.clear()
            self.manager.retry_queue()
            with self.manager.condition:
                run_ids = tuple(self.runs)
                config = self.manager.config.copy()
            for run_id in run_ids:
                try:
                    pending = self.advance(config, run_id)
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    continue
                if not pending:
                    # 受付直後の通知との競合を避け、Runの次回操作でも再検査する。
                    with self.manager.condition:
                        if not self.wake.is_set():
                            self.runs.discard(run_id)

    def advance(self, config, run_id):
        """キュー、RunManagerの順にロックし、履歴の反映と待機中依頼の登録を進める。"""
        with self.manager.condition:
            if self.manager.closing or config["run_root"] != self.manager.root:
                return False
            blocked = self.manager.blocked_conversations.get(run_id)
            if blocked and run_id in self.manager.jobs:
                return True
        if blocked:
            fail_pending_after_unconfirmed_stop(config, run_id, blocked)
            with self.manager.condition:
                if self.manager.closing:
                    return False
                if config["run_root"] == self.manager.root and self.manager.blocked_conversations.get(run_id) == blocked:
                    del self.manager.blocked_conversations[run_id]
                    try:
                        self.manager.persist()
                    except OSError:
                        self.manager.blocked_conversations[run_id] = blocked
                        raise
                    self.manager.start_pending()
                    self.manager.condition.notify_all()
            return True

        run_path, worktree, _, unit = load_saved_run(config, run_id)
        with editor_queue(run_path) as queue, self.manager.condition:
            if self.manager.closing or config["run_root"] != self.manager.root:
                return False
            if run_id in self.manager.blocked_conversations:
                return True
            history = queue.get("conversation", [])
            cancelled = self.cancelled.get(run_id)
            for entry in history:
                if entry["id"] == cancelled and entry["status"] == "waiting":
                    _finish(entry, "cancelled", "実行待ちの検証を取り消しました。")
                    # 履歴を保存した後の巡回で、再起動用の取消記録を除去する。
                    return True
            if cancelled:
                self.cancelled.pop(run_id)
                self.manager.persist()
            if not any(entry["status"] in PENDING for entry in history):
                return False
            view = self.manager.get(run_id)
            _, _, record, _ = load_saved_run(config, run_id)
            job = self.manager.jobs.get(run_id, {})
            state = view.state
            self._reconcile_dispatched_entries(queue, history, job, state)
            self._interpret_received_entries(run_path, history, view, state, record)
            self._record_predecessor_build(history, job, state, record)
            return self._advance_waiting_entry(config, run_path, worktree, unit, queue, history, view, state, record, job)

    def _reconcile_dispatched_entries(self, queue, history, job, state):
        """既に配信・開始した指示を、ジョブと保存済みプロンプトの状態に合わせる。"""
        for entry in history:
            status = entry["status"]
            if status == "running" and job.get("conversation_id") != entry["id"]:
                _finish(entry, "unknown", "前回は処理が開始されました。結果をログで確認し、必要なら新しい指示を送ってください。")
            if status == "steering":
                prompt = next((p for p in queue["prompts"] if p["id"] == entry["id"]), {})
                if prompt.get("status") == "sent":
                    _finish(entry, "done", "Junie の指示への対応が移行先に反映されました。")
                elif not job and state.get("ready"):
                    if prompt.get("status") in {"steering", "applying", "sending"}:
                        _finish(entry, "unknown", "Junie への送信は始まりました。結果をエディタとログで確認してください。")
                    else:
                        entry["message"] = "指示は保留中です。同じ Run を再開したときに Junie に渡します。"

    def _interpret_received_entries(self, run_path, history, view, state, record):
        """受け付けた指示の照会・確認回答を処理し、未着手の実行を待機状態にする。"""
        phase = record["status"]
        for entry in history:
            if entry["status"] != "received":
                continue
            intent = _intent(entry["text"], phase)
            if entry.get("query_intent") and intent not in QUERY_INTENTS:
                _finish(entry, "failed", "受付時に保存版を記録していません。エディタで確認して指示を送信し直してください。")
                continue
            if intent in QUERY_INTENTS:
                message = _phase_message(state, phase)
                if intent != "status":
                    kind = intent.removesuffix("_status")
                    code = record.get(f"{kind}_exit_code")
                    prior = record.get("last_diagnostics", {}).get(kind, {}) if code is None else {}
                    code = prior.get("code", code)
                    log = prior.get("log") or (_diagnostic(run_path, kind) if code is not None else None)
                    entry["logs"] = [log] if log else []
                    if code is not None:
                        message += f" 直近の終了コードは {code} です。"
                    detail = prior.get("error") or (record.get("error") if (phase in BUILD_PHASES) == (kind == "build") else None)
                    if detail:
                        message += f" {detail}"
                    if code is None and not log:
                        message += " 保存済みの結果はありません。"
                _finish(entry, "done", message)
                continue
            clarification_entry = _clarification(history)
            request = state.get("junie_request")
            if entry.get("junie_question") and (not request or entry["junie_question"] != request["id"]):
                _finish(entry, "failed", "Junie の確認は更新されました。現在の質問を確認してください。")
                continue
            if request and entry.get("junie_question"):
                option = _junie_option(request, entry["text"])
                if option is not None:
                    entry["question"] = request["title"]
                    accepted = view.answer_junie({"request_id": request["id"], "option_id": option["id"],
                                                  "operation_id": state["operation_id"]})
                    _finish(entry, "done" if accepted else "failed", "Junie に回答しました。" if accepted
                            else "確認が更新されました。現在の質問を確認してください。")
                    continue
                if clarification_entry is None:
                    entry["question"] = request["title"]
                    _finish(entry, "failed", "回答は表示されている選択肢の文言で入力してください。")
                    continue
            if reply_to := entry.get("clarification_id"):
                if clarification_entry is None or reply_to != clarification_entry["id"]:
                    _finish(entry, "failed", "確認待ちの指示が更新されました。現在の質問を確認してください。")
                    continue
                answer = _answer_kind(entry["text"])
                if answer is None:
                    _finish(entry, "failed", "表示されている確認への回答を送信し直してください。")
                elif answer == "cancel":
                    _finish(clarification_entry, "cancelled", "指示を取り消しました。")
                    _finish(entry, "done", "確認待ちの指示を取り消しました。")
                else:
                    # 回答時点で選んだ保存版を使う。
                    clarification_entry.update(intent=answer, status="waiting", answer=entry["text"],
                                    edit_revision=entry["edit_revision"], edits=entry["edits"], targets=entry["targets"])
                    _finish(entry, "done", "確認への回答を受け付けました。")
                continue
            if clarification_entry:
                _finish(entry, "failed", "「ビルドだけ」「チェッカーまで」「キャンセル」のいずれかで回答してください。")
                continue
            if intent in {"clarify", "stop"}:
                clarification_text = "ビルドだけですか？ チェッカーまで実行しますか？"
                value = unicodedata.normalize("NFKC", entry["text"]).lower()
                if _conditional_verification(value) or re.search(r"\b(?:if|unless|when|after)\b", value):
                    clarification_text = "実行条件を確定できません。条件を付けずに検証する場合は「ビルドだけ」か「チェッカーまで」、取り消す場合は「キャンセル」と答えてください。"
                elif REPAIR_REQUEST.search(value):
                    clarification_text = "編集と検証の順序を確認できません。編集なしで検証する場合は「ビルドだけ」か「チェッカーまで」、指示を出し直す場合は「キャンセル」と答えてください。"
                entry.update(status="clarification", message=clarification_text, question=clarification_text)
            elif intent == "unsupported":
                _finish(entry, "failed", "この欄では移行先の編集指示と設定済みのビルド・チェッカーを受け付けます。")
            else:
                entry.update(status="waiting", intent=intent)

    def _record_predecessor_build(self, history, job, state, record):
        """受付時に結び付けた先行ビルドの通常終了だけを履歴に反映する。"""
        if not job and state.get("ready"):
            code = record.get("build_exit_code")
            for waiting in history:
                if (waiting["status"] != "waiting" or not waiting["intent"].startswith("after_build_")
                        or waiting.get("predecessor_build_finished")):
                    continue
                operation_id = waiting.get("predecessor_operation_id")
                if operation_id and operation_id == record.get("build_operation_id") and type(code) is int and code >= 0:
                    waiting["predecessor_build_finished"] = True
                else:
                    _finish(waiting, "failed", UNCONFIRMED_BUILD)

    def _advance_waiting_entry(self, config, run_path, worktree, unit, queue, history, view, state, record, job):
        """最初の待機依頼を評価し、必要ならRunManagerのキューへ登録・保存する。"""
        from .dashboard import _prompt_timing

        phase = record["status"]
        entry = next((item for item in history if item["status"] == "waiting"), None)
        if entry is None:
            return any(item["status"] in PENDING for item in history)
        intent = entry["intent"]
        if intent == "junie":
            if entry["edit_revision"] != queue.get("edit_revision"):
                _finish(entry, "failed", "保存版が更新されました。エディタで確認して指示を送信し直してください。")
                return True
            timing = _prompt_timing(config, state, run_path, record, pending_edits=bool(queue["edits"]),
                                    live=bool(view.control and view.control.junie_steering))
            if timing is None:
                entry["message"] = "現在の工程が終わってから、Junie に渡せるか確認します。"
                if state.get("ready") and not job:
                    _finish(entry, "failed", "この工程から Junie に指示を渡せません。進捗を確認してください。")
            elif sum(prompt["status"] != "sent" for prompt in queue["prompts"]) < 16:
                _queue_steering(queue, entry, timing)
            return True
        if job and intent != "diff":
            if job.get("conversation_id") == entry["id"]:
                return True
            if intent.startswith("stop_build") and not entry.get("stop_checked"):
                entry["stop_checked"] = True
                if phase == "building" and view.control and view.control.request_build_stop():
                    view.request_stop({"operation_id": state["operation_id"]})
                    entry["plan"] = entry["message"] = "現在のビルドの停止を待ち、保存した版で再ビルドします。"
            entry.setdefault("message", "現在の処理の終了を待ちます。")
            return True
        if intent == "diff":
            agent = run_path / "agent-workspace"
            current = target_versions(worktree, unit)
            baseline = (record.get("verification") or {}).get("target_hashes", record.get("target_hashes", {}))
            drafts = target_versions(agent, unit) if agent.is_dir() and not agent.is_symlink() else {}
            changed = [name for name, digest in current.items()
                       if name in queue["edits"] or name in baseline and baseline[name] != digest
                       or drafts.get(name) not in (None, digest)]
            entry["review_targets"] = changed
            message = "確認範囲を現在の移行先に絞りました。"
            _finish(entry, "done", message + ("エディタの「差分」で表示された対象を確認してください。" if changed
                                              else "保存記録と照合できる差分はありませんでした。"))
            return True
        if intent == "resume_stage":
            if state.get("ready") and not any(other["unit_id"] == record["unit_id"]
                                               for other in self.manager.jobs.values()):
                self.manager.enqueue(view, {"action": "conversation", "conversation_id": entry["id"]})
                self.manager.save_enqueued()
                entry["message"] = "保存した再開計画を実行待ちに登録しました。"
            return True
        mode = intent.removeprefix("stop_").removeprefix("after_build_")
        if mode not in REBUILD or not state.get("ready"):
            return True
        if mode.startswith("checker") and checker_command_for_unit(config, unit) is None:
            _finish(entry, "failed", "チェッカーが設定されていません。設定タブでコマンドを確認してください。")
            return True
        if any(other["unit_id"] == record["unit_id"] for other in self.manager.jobs.values()):
            entry["message"] = "同じ移行単位の別の Run が終わるまで待機します。"
            return True
        self.manager.enqueue(view, {"action": "conversation", "conversation_id": entry["id"]})
        self.manager.save_enqueued()
        operation = {"build": "保存版をビルドします。", "build_repair": "保存版をビルドし、失敗した場合は Junie に修正を依頼します。",
                     "checker": "保存版をビルドし、成功後にチェッカーを実行します。",
                     "checker_repair": "Junie に指摘の修正を依頼し、ビルドとチェッカーを実行します。"}[mode]
        entry.setdefault("plan", operation)
        entry["message"] = "実行枠が空き次第、処理を開始します。"
        return True


def run_instruction(config, run_id, identifier):
    """保存済み検証依頼をworkflowへ渡して結果を記録する。"""
    from .workflow import resume_with_junie

    run_path, _, _, _ = load_saved_run(config, run_id)
    with editor_queue(run_path) as queue:
        entry = next(item for item in queue["conversation"] if item["id"] == identifier)
        if entry["status"] != "waiting":
            raise ValueError("指示は開始済みか終了済みです。履歴を確認してください。")
        # 旧版で永続化された実行待ちも、照合なしに再開しない。
        if entry["intent"].startswith("after_build_") and not entry.get("predecessor_build_finished"):
            raise ValueError(UNCONFIRMED_BUILD)
        entry.update(status="running", message="保存版を照合して検証を開始します。", started_at=datetime.now(timezone.utc).isoformat())
        if stage_request := entry.get("stage_request"):
            instruction = {**stage_request, "id": identifier}
        else:
            instruction = entry.copy()
            instruction["intent"] = instruction["intent"].removeprefix("after_build_")
    return resume_with_junie(config, run_id, verification_request=instruction)


def fail_pending_after_unconfirmed_stop(config, run_id, message):
    """終了未確認時の同一Runの待機依頼を失敗として保存する。"""
    validate_run_id(run_id)
    try:
        (Path(config["run_root"]).resolve() / run_id / "editor.json").lstat()
    except FileNotFoundError:
        # A started preparation can have been interrupted before conversation storage existed.
        return
    run_path, _, _, _ = load_saved_run(config, run_id)
    with editor_queue(run_path) as queue:
        for entry in queue.get("conversation", []):
            if entry["status"] in {"received", "waiting"} and not entry.get("query_intent"):
                _finish(entry, "failed", message)


def finish_instruction(config, run_id, identifier, result, error=None):
    """検証依頼の結果と対象版を履歴へ確定する。"""
    run_path, _, record, _ = load_saved_run(config, run_id)
    with editor_queue(run_path) as queue:
        entry = next((item for item in queue.get("conversation", []) if item["id"] == identifier), None)
        if entry is None or entry["status"] not in {"waiting", "running"}:
            return
        if result and result.get("request_id") == identifier:
            entry["result_status"] = result["status"]
            if verification := result.get("verification"):
                entry["built_targets"] = verification["target_hashes"]
            entry["logs"] = [log for kind in ("build", "checker")
                             if result.get(f"{kind}_log_path")
                             if (log := _diagnostic(run_path, kind))]
            detail = result.get("error") or record.get("error")
            message = f"結果は「{PHASES.get(result['status'], '確認待ち')}」です。"
            _finish(entry, "done", f"{message} {detail}" if detail else message)
        elif isinstance(error, KeyboardInterrupt):
            _finish(entry, "cancelled", "処理を停止しました。進捗と保存した編集を確認してください。")
        else:
            _finish(entry, "failed", format_error(error) if error else "検証結果を確認できませんでした。進捗を確認してください。")
