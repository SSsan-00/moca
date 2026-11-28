"""ダッシュボードの ACP 通信、許可の応答、追加指示とモデル設定の確認。"""

from __future__ import annotations

import codecs
import json
import subprocess
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import TYPE_CHECKING, Any

from ..execution import check_cancelled, report_activity
from ..files import has_completion_record, has_symlink_component, safe_target
from .acp_model import ModelChange, ModelMismatch, ModelSelectionError, RequestedModel, acp_effort_option
from .acp_permissions import PinnedReferenceReads, ReadRefused, metrics_kind
from .junie_launch import JunieAcpModelUnavailable, JunieSessionError

if TYPE_CHECKING:
    from ..runs.editor_queue import LivePrompts
    from .session_events import SessionEvents


ACP_TIMEOUT = 30
ACP_STARTUP_TIMEOUT = 90
ACP_SELECT_MODEL_REQUEST = -1
ACP_SET_EFFORT_REQUEST = -2
ACP_RETRY_EFFORT_REQUEST = -3


_ACP_INCOMPLETE_REASONS = {"cancelled", "max_tokens", "max_turn_requests", "refusal"}

steering_input_observer: ContextVar[Callable[[str], bool] | None] = ContextVar("steering_input_observer", default=None)
acp_stop_observer: ContextVar[Callable[[str | None], None] | None] = ContextVar("acp_stop_observer", default=None)
reference_read_scope: ContextVar[PinnedReferenceReads | None] = ContextVar("reference_read_scope", default=None)
permission_observer: ContextVar[Callable[[str, str], None] | None] = ContextVar("permission_observer", default=None)


@dataclass
class _SteeringRequest:
    identifiers: list[str]
    receipt: Path
    token: str
    accepted: bool = False
    new_turn: bool = False


def acp_confirms_effort(option: dict | None, effort: str, model: str | None = None) -> bool:
    if option is not None:
        return option.get("currentValue") == effort
    return effort == "none" and model == "junie-lite"


def require_reported_selection(info: dict, model: RequestedModel, effort: str | None) -> None:
    model.check(info)
    if effort and (option := acp_effort_option(info)) and option.get("currentValue") != effort:
        raise ModelSelectionError(f"Junie ACP の推論レベルが指定した {effort} と一致しません。")


def acp_confirms_brave(session: dict) -> bool:
    options = session.get("configOptions")
    if not isinstance(options, list):
        return False
    brave = [option for option in options if isinstance(option, dict) and option.get("id") == "brave_mode"]
    if len(brave) != 1:
        return False
    mode = brave[0]
    if mode.get("type") == "boolean":
        return mode.get("currentValue") is True
    values = mode.get("options")
    return (mode.get("type") == "select" and mode.get("currentValue") == "on" and isinstance(values, list)
            and any(isinstance(choice, dict) and choice.get("value") == "on" for choice in values))


def _acp_message(line: bytes):
    try:
        return json.loads(line)
    except UnicodeDecodeError:
        if line.startswith(codecs.BOM_UTF8):
            raise
        message = json.loads(line.decode("cp932"))
        params = message.get("params") if isinstance(message, dict) else None
        update = params.get("update") if isinstance(params, dict) else None
        content = update.get("content") if isinstance(update, dict) else None
        if (not isinstance(content, dict) or content.get("type") != "text" or not isinstance(content.get("text"), str)
                or "id" in message or message.get("jsonrpc") != "2.0" or message.get("method") != "session/update"
                or update.get("sessionUpdate") != "agent_message_chunk" or not isinstance(params.get("sessionId"), str)):
            raise ValueError("Non-UTF-8 ACP control frame")
        return message


class AcpSession:
    """ダッシュボード実行をACPの標準入出力につなぐ。"""

    require_brave = False
    agent_version = None

    def _connection_timeout(self) -> float:
        return ACP_STARTUP_TIMEOUT

    def __init__(
        self, process: subprocess.Popen, command: list[str], project: Path, show_request: Callable,
        completion_path: Path, token: str, prompts: LivePrompts | None, *, require_brave: bool = False,
    ) -> None:
        """ACPセッションと入出力・完了状態を初期化する。"""
        self.process = process
        self.command = command
        self.project = project
        self.show_request = show_request
        self.reference_reads = reference_read_scope.get()
        self.observe_permission = permission_observer.get()
        self.messages: Queue[bytes] = Queue()
        self.closing = Event()
        self.outgoing: Queue[tuple[bytes, Future[None]] | None] = Queue()
        self.writes: deque[tuple[float, Future[None], Callable[[], None] | None]] = deque()
        self.session_id = None
        self.tool_calls: dict[str, dict] = {}
        self.pending: tuple[int | str, Future[str]] | None = None
        self.startup_deadline = time.monotonic() + self._connection_timeout()
        self.model_change: ModelChange | None = None
        self.pending_effort: tuple[str, str, int] | None = None
        self.session_ready = False
        self.require_brave = require_brave
        self.permission_started = 0.0
        self.prompts = prompts
        self.completion_path = completion_path
        self.token = token
        self.steering_supported = False
        self.steering_requests: dict[int, _SteeringRequest] = {}
        self.steering_waiting: int | None = None
        self.steering_deadline = 0.0
        self.next_request_id = 4
        self.prompt_finished = False
        self.steering_new_turn = False
        self.progress_sink: Callable[[str], None] | None = None
        self.progress_text = ""
        # stdoutの行待ちを別スレッドに任せ、監視側は停止要求や許可への回答を確認し続ける。
        self.reader = Thread(target=self._read, daemon=True)
        self.writer = Thread(target=self._write, daemon=True)
        self.reader.start()
        self.writer.start()

    def _read(self) -> None:
        """標準出力から次のACPメッセージを読み取る。"""
        try:
            for line in self.process.stdout:
                self.messages.put(line)
        finally:
            # キューが一時的に空の状態と、接続が終了した状態を区別するための印。
            self.messages.put(b"")

    def send(self, *, after_flush: Callable[[], None] | None = None, **message: Any) -> None:
        """監視を止めず、送信順にACPメッセージを登録する。"""
        data = (json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode("utf-8")
        written: Future[None] = Future()
        self.writes.append((time.monotonic() + ACP_TIMEOUT, written, after_flush))
        self.outgoing.put((data, written))

    def _write(self) -> None:
        """ブロックし得る書込みとパイプのクローズを送信側だけで行う。"""
        try:
            while (item := self.outgoing.get()) is not None and not self.closing.is_set():
                data, written = item
                try:
                    self.process.stdin.write(data)
                    self.process.stdin.flush()
                except OSError as exc:
                    written.set_exception(exc)
                    return
                written.set_result(None)
        finally:
            try:
                self.process.stdin.close()
            except OSError:
                pass

    def request(self, request_id: int, method: str, *, after_flush: Callable[[], None] | None = None,
                **params: Any) -> None:
        self.send(id=request_id, method=method, params=params, after_flush=after_flush)

    def steer(self, progress_style: str | None = None) -> None:
        if (self.prompts is None or not self.steering_supported or self.session_id is None
                or self.pending or self.steering_waiting is not None or self.prompt_finished
                or has_completion_record(self.completion_path, self.token)):
            return
        if not self.session_ready:
            return
        token = uuid.uuid4().hex
        receipt = safe_target(self.project, Path(".migration-scratch") / f"steering-{token}.txt")
        receipt.parent.mkdir(exist_ok=True)
        identifiers, text = self.prompts.take()
        if not identifiers:
            return
        # 元の完了記録との競合を避け、追加指示にも今回専用の完了記録を要求する。
        instruction = (
            "Operator follow-up for the current task. Apply this instruction now, within the original "
            "workspace and target constraints, preserving human edits and completed work:\n"
            f"{text}\n\nContinue the original assignment as well. After fully applying this follow-up, "
            f"write exactly {token} to `.migration-scratch/{receipt.name}`. "
            "Do not write this receipt while blocked or awaiting an operator answer. "
            f"After all work is complete, write exactly {self.token} to `{self.completion_path.name}` "
            "as the final file action, even if that file already exists."
        )
        if progress_style:
            instruction += "\n\n" + progress_style
        try:
            observer = steering_input_observer.get()
            accepted = observer is None or observer(instruction)
        except BaseException:
            self.prompts.finish(identifiers, False)
            raise
        if not accepted:
            self.prompts.finish(identifiers, False)
            self.steering_supported = False
            return
        request_id = self.next_request_id
        self.next_request_id += 1
        self.steering_requests[request_id] = _SteeringRequest(identifiers, receipt, token)
        self.steering_waiting = request_id
        self.steering_deadline = time.monotonic() + ACP_TIMEOUT
        self.request(request_id, "_session/steering", sessionId=self.session_id,
                     prompt=[{"type": "text", "text": instruction}])
        report_activity("実行中のJunieへ追加指示を送っています。")

    def steering_complete(self) -> bool:
        return self.steering_waiting is None and all(
            request.accepted and not has_symlink_component(self.project, request.receipt.relative_to(self.project))
            and has_completion_record(request.receipt, request.token)
            for request in self.steering_requests.values()
        )

    def check_steering_turns(self, events: SessionEvents, *, exited: bool = False) -> None:
        for request in self.steering_requests.values():
            if request.new_turn and (exited or request.token in events.finished_steering):
                if (has_symlink_component(self.project, request.receipt.relative_to(self.project))
                        or not has_completion_record(request.receipt, request.token)):
                    raise JunieSessionError(
                        self.command, 1, "追加ターンが完了記録を残さず終了しました。指示を送信待ちに戻します。",
                    )
                request.new_turn = False
        self.steering_new_turn = any(request.new_turn for request in self.steering_requests.values())

    def finish_prompts(self, success: bool) -> None:
        if self.prompts is not None:
            for request in self.steering_requests.values():
                self.prompts.finish(request.identifiers, success and request.accepted)

    def permission(self, request_id: int | str, params: dict) -> None:
        tool = params["toolCall"]
        # 許可要求だけでは省略される情報を、先に受け取ったツール更新から補う。
        details = {**self.tool_calls.get(tool["toolCallId"], {}), **tool}
        content = []
        for item in details.get("content") or []:
            block = item.get("content", {})
            if item.get("type") == "content" and block.get("type") == "text":
                content.append(block["text"])
        for location in details.get("locations") or []:
            content.append(location["path"])
        if details.get("rawInput") is not None:
            content.append(json.dumps(details["rawInput"], ensure_ascii=False, indent=2))
        options = [
            {"id": option["optionId"], "label": option["name"], "kind": option["kind"]}
            for option in params["options"] if isinstance(option.get("optionId"), str)
        ]
        if self.observe_permission is not None:
            kind = metrics_kind(details.get("kind"))
            reason = "agent_owned_unverified" if kind == "read" else "operator_decision"
            self.observe_permission(kind, reason if options else "no_permission_options")
        if not options:
            self.send(id=request_id, error={"code": -32602, "message": "No permission options"})
            return
        reply = self.show_request({
            "title": "Junie の操作を確認",
            "detail": "\n\n".join([details.get("title") or "Junie が操作の許可を求めています。", *content]),
            "options": options,
        })
        if reply is None:
            check_cancelled()
            self.send(id=request_id, result={"outcome": {"outcome": "cancelled"}})
        else:
            self.pending = request_id, reply
            self.permission_started = time.monotonic()

    def initialize(self) -> None:
        caps = {"fs": {"readTextFile": True}} if self.reference_reads and self.reference_reads.pins else {}
        if self.require_brave:
            caps["session"] = {"configOptions": {"boolean": {}}}
        self.request(1, "initialize", protocolVersion=1, clientCapabilities=caps)

    def read_reference(self, request_id: int | str, params: dict) -> None:
        if not self.reference_reads or not self.reference_reads.pins:
            if self.observe_permission is not None:
                self.observe_permission("read", "client_read_unavailable")
            self.send(id=request_id, error={"code": -32601, "message": "Method not supported"})
            return
        try:
            content = self.reference_reads.read(params)
        except ReadRefused as exc:
            if self.observe_permission is not None:
                self.observe_permission("read", str(exc))
            self.send(id=request_id, error={"code": -32602, "message": "Only unchanged, pinned Run references are readable. Use a bounded line range."})
        else:
            reads = self.reference_reads
            receipt = reads.delivery_metadata(params, content)

            def delivered():
                reads.provided_bytes += receipt["utf8_bytes"]
                reads.last_delivery = receipt
                if self.observe_permission is not None:
                    self.observe_permission("read", "pinned_reference_read")

            self.send(id=request_id, result={"content": content}, after_flush=delivered)

    def _check_writes(self, *, closing: bool = False) -> None:
        while self.writes and self.writes[0][1].done():
            _, written, after_flush = self.writes.popleft()
            if written.exception() is not None:
                if not closing:
                    raise JunieSessionError(self.command, 1, "JunieへのACP送信に失敗しました。") from written.exception()
            elif after_flush is not None:
                after_flush()
        if not closing and self.writes and time.monotonic() >= self.writes[0][0]:
            raise JunieSessionError(self.command, 1, "JunieへのACP送信がタイムアウトしました。")

    def _accept_steering_response(self, request_id, message) -> None:
        """追加指示の受理を記録し、未受理ならキューに戻す。"""
        request = self.steering_requests[request_id]
        result = message.get("result")
        outcome = result.get("outcome") if isinstance(result, dict) else None
        self.steering_waiting = None
        if "error" not in message and outcome in ("injected", "startedNewTurn"):
            request.accepted = True
            request.new_turn = outcome == "startedNewTurn"
            self.steering_new_turn |= request.new_turn
            report_activity("Junieが追加指示を受け取りました。処理の完了を待っています。")
        else:
            self.steering_supported = False
            self.prompts.finish(request.identifiers, False)
            del self.steering_requests[request_id]
            report_activity("追加指示を送信待ちに戻しました。画面に表示されたタイミングで渡します。")

    def _requested_option(self, flag: str) -> str | None:
        flags = self.command[:-2]
        return flags[flags.index(flag) + 1] if flag in flags else None

    def _effort_not_confirmed(self, effort: str) -> JunieSessionError:
        return JunieSessionError(
            self.command, 1,
            f"Junie ACP で推論レベル {effort} を確認できなかったため、指示を送信していません。"
            "Junie CLI の更新、またはモデルと推論レベルの設定を確認してください。",
        )

    def _configure_effort(self, session: dict) -> None:
        effort = self._requested_option("--effort")
        if effort is None:
            self._mark_session_ready()
            return
        option = acp_effort_option(session)
        if acp_confirms_effort(option, effort, self._requested_option("--model")):
            self._mark_session_ready()
            return
        if option is None or not isinstance(option.get("id"), str) or not option["id"]:
            raise self._effort_not_confirmed(effort)
        self._send_effort_request(option["id"], effort, ACP_SET_EFFORT_REQUEST)

    def _send_effort_request(self, config_id: str, value: str, request_id: int) -> None:
        self.pending_effort = config_id, value, request_id
        self.startup_deadline = time.monotonic() + self._connection_timeout()
        self.request(request_id, "session/set_config_option",
                     sessionId=self.session_id, configId=config_id, value=value)

    def _retry_effort_configuration(self, error: dict | None = None) -> None:
        assert self.pending_effort is not None
        config_id, effort, request_id = self.pending_effort
        if request_id == ACP_RETRY_EFFORT_REQUEST or (error or {}).get("code") in (-32601, -32602):
            raise self._effort_not_confirmed(effort)
        self._send_effort_request(config_id, effort, ACP_RETRY_EFFORT_REQUEST)

    def _complete_effort_configuration(self, message: dict) -> None:
        assert self.pending_effort is not None
        config_id, effort, _ = self.pending_effort
        result = message.get("result")
        option = acp_effort_option(result) if isinstance(result, dict) else None
        error = message.get("error")
        if isinstance(result, dict):
            self.requested_model.check(result)
        if "error" in message or not acp_confirms_effort(option, effort) or option.get("id") != config_id:
            self._retry_effort_configuration(error if isinstance(error, dict) else None)
            return
        self._require_brave_option(result)
        self.pending_effort = None
        self._mark_session_ready()

    def _mark_session_ready(self) -> None:
        self.session_ready = True
        if self.require_brave:
            report_activity("Junie の Brave モード: ON（今回の実行のみ）", kind="phase")
        self._on_session_ready()

    def _require_brave_option(self, session: dict) -> None:
        if self.require_brave and not acp_confirms_brave(session):
            action = "この実行を停止します。" if self.session_ready else "指示を送信していません。"
            raise JunieSessionError(
                self.command, 1, "Junie ACP で Brave モードがオンになったことを確認できなかったため、"
                f"{action}対応する Junie CLI を使用するか、Brave モードを選択せずに再開してください。",
            )

    def _on_session_ready(self) -> None:
        self.request(3, "session/prompt", sessionId=self.session_id,
                     prompt=[{"type": "text", "text": self.command[-1]}])

    def _advance_prompt(self, request_id, message) -> bool:
        # 要求IDは1=初期化、2=セッション作成、3=プロンプト実行。応答を待って次へ進む。
        if "error" in message:
            error = message["error"]
            code = error.get("code")
            if request_id == 3 and code == -32002 and (error.get("data") or {}).get("reason") == "model_not_available":
                raise JunieAcpModelUnavailable(self.command, 1, "Junie の ACP で利用できるモデルがありません。")
            detail = ("Junie CLIでログインしてから再開してください。" if code == -32000 else
                      f"JunieのACP要求に失敗しました（要求 {request_id}、コード {code}）。")
            raise JunieSessionError(self.command, 1, detail)
        result = message.get("result", {})
        if request_id == 1 and result.get("protocolVersion") == 1:
            # バージョン番号では推測せず、接続先が宣言する対応能力で切り替える。
            self.agent_version = (result.get("agentInfo") or {}).get("version")
            metadata = result.get("_meta")
            steering = metadata.get("steering") if isinstance(metadata, dict) else None
            self.steering_supported = isinstance(steering, dict) and steering.get("supported") is True
            self.request(2, "session/new", cwd=str(self.project), mcpServers=[])
        elif request_id == 2 and result.get("sessionId"):
            self._require_brave_option(result)
            self.session_id = result["sessionId"]
            self.requested_model = RequestedModel(self._requested_option("--model"), self.agent_version)
            try:
                self.model_change = self.requested_model.change_for(result, self._requested_option("--effort"))
            except ModelMismatch as exc:
                raise JunieAcpModelUnavailable(self.command, 1, str(exc)) from exc
            if self.model_change:
                self.startup_deadline = time.monotonic() + self._connection_timeout()
                self.request(ACP_SELECT_MODEL_REQUEST, self.model_change.method, sessionId=self.session_id,
                             **self.model_change.params)
            else:
                self._configure_effort(result)
        elif request_id == 3:
            reason = result.get("stopReason")
            if isinstance(reason, str) and reason in _ACP_INCOMPLETE_REASONS:
                if observe := acp_stop_observer.get():
                    observe(reason)
            self.prompt_finished = True
            if self.steering_waiting is None and not self.steering_new_turn:
                return True
        else:
            raise JunieSessionError(self.command, 1, "JunieのACP初期化応答が無効です。")
        return False

    def _receive_message(self, message) -> bool:
        """ACP応答を振り分け、プロンプトの終了を返す。"""
        try:
            return self._dispatch_message(message)
        except ModelSelectionError as exc:
            self.session_ready = False
            raise JunieSessionError(self.command, 1, str(exc)) from exc

    def _dispatch_message(self, message) -> bool:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {})
        if not method and request_id == self.steering_waiting and request_id is not None:
            self._accept_steering_response(request_id, message)
        elif method and request_id is not None:
            if (method == "session/request_permission" and self.session_id is not None
                    and params.get("sessionId") == self.session_id):
                self.permission(request_id, params)
            elif (method == "fs/read_text_file" and self.session_id is not None
                  and params.get("sessionId") == self.session_id):
                self.read_reference(request_id, params)
            else:
                self.send(id=request_id, error={"code": -32601, "message": "Method not supported"})
        elif method == "session/update" and params.get("sessionId") == self.session_id:
            update = params.get("update", {})
            kind = update.get("sessionUpdate")
            if kind == "config_option_update":
                self._require_brave_option(update)
                if self.model_change:
                    self.model_change.session.update(update)
                else:
                    require_reported_selection(update, self.requested_model,
                                               self._requested_option("--effort") if self.session_ready else None)
            elif kind in ("tool_call", "tool_call_update") and "toolCallId" in update:
                self.flush_progress()
                self.tool_calls.setdefault(update["toolCallId"], {}).update(update)
            elif self.progress_sink is not None and kind == "agent_message_chunk":
                content = update.get("content")
                if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
                    room = 2000 - len(self.progress_text)
                    if room > 0:
                        self.progress_text += content["text"][:room]
        elif not method and request_id == ACP_SELECT_MODEL_REQUEST and self.model_change:
            change, self.model_change = self.model_change, None
            info = change.confirmed(message)
            self._require_brave_option(info)
            self._configure_effort(info)
        elif not method and self.pending_effort is not None and request_id == self.pending_effort[2]:
            self._complete_effort_configuration(message)
        elif not method and request_id in (1, 2, 3):
            return self._advance_prompt(request_id, message)
        return False

    def flush_progress(self) -> None:
        if self.progress_sink is not None and self.progress_text:
            text, self.progress_text = self.progress_text, ""
            self.progress_sink(text)

    def advance(self) -> bool:
        self._check_writes()
        if self.pending:
            # 回答待ちではブロックせず監視側へ戻し、停止要求を受け付けられるようにする。
            request_id, reply = self.pending
            if not reply.done():
                return False
            check_cancelled()
            self.send(id=request_id, result={"outcome": {"outcome": "selected", "optionId": reply.result()}})
            self.pending = None
            self.steering_deadline += time.monotonic() - self.permission_started
        # 更新が続いても、プロセス監視や完了記録の確認へ定期的に制御を戻す。
        for _ in range(128):
            check_cancelled()
            try:
                line = self.messages.get_nowait()
            except Empty:
                break
            if not line:
                if self.prompt_finished and self.steering_waiting is None:
                    return True
                raise JunieSessionError(self.command, self.process.returncode or 1, "JunieのACP接続が終了しました。")
            try:
                message = _acp_message(line)
                if not isinstance(message, dict):
                    raise ValueError
            except (UnicodeError, ValueError):
                raise JunieSessionError(self.command, 1, "JunieのACP応答を読み取れませんでした。") from None
            if self._receive_message(message):
                return True
            if self.pending:
                return False
        if not self.session_ready and time.monotonic() >= self.startup_deadline:
            if self.pending_effort is not None:
                self._retry_effort_configuration()
            else:
                raise JunieSessionError(self.command, 1, "JunieのACP接続がタイムアウトしました。Junie CLIのバージョンとログを確認してください。")
        if self.steering_waiting is not None and time.monotonic() >= self.steering_deadline:
            raise JunieSessionError(self.command, 1, "追加指示の受信を確認できませんでした。指示を送信待ちに戻します。実行を再開してください。")
        return self.prompt_finished and self.steering_waiting is None and not self.steering_new_turn

    def cancel(self) -> None:
        """実行中のACPリクエストを取り消す。"""
        if self.pending:
            self.send(id=self.pending[0], result={"outcome": {"outcome": "cancelled"}})
        if self.session_id:
            self.send(method="session/cancel", params={"sessionId": self.session_id})

    def close(self) -> None:
        """ACP標準入出力とセッション資源を閉じる。"""
        self.closing.set()
        self.outgoing.put(None)
        self.writer.join(timeout=1)
        self._check_writes(closing=True)
        self.reader.join(timeout=1)
        if not self.reader.is_alive():
            self.process.stdout.close()
