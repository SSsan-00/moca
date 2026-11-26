from __future__ import annotations

import subprocess
from concurrent.futures import Future
from contextlib import suppress
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from ..junie import junie_cli
from ..junie.reply_text import DuplicateJsonKey, load_unique_json, unwrap_json_fence


class RuleChat(junie_cli._AcpSession):
    def __init__(self, project, model, effort):
        self.response = []
        self.size = 0
        self.request_info = None
        self.offered_skill = False
        self.turn_id = None
        self.next_turn = 10
        command = junie_cli.build_command(project, "", None, model=model, effort=effort)
        self.project = project
        self.command = command
        self.process = None

    def _connect(self):
        # ワークスペースに登録した後で起動し、初期化の書込みが失敗しても所有権を失わない。
        self.process = junie_cli._launch_process(
            junie_cli.acp_command(self.command), owned=True, cwd=self.project,
            kill_on_close=True, retain_job_on_stop_error=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        super().__init__(self.process, self.command, self.project, self.show_permission, self.project / "unused", "", None)
        self.request(1, "initialize", protocolVersion=1, clientCapabilities={})

    def show_permission(self, request):
        self.request_info = {**request, "id": uuid4().hex}
        return Future()

    def answer(self, request, option):
        if not self.pending or not self.request_info or request != self.request_info["id"]:
            raise ValueError("確認が更新されました。")
        if option not in {item["id"] for item in self.request_info["options"]} or self.pending[1].done():
            raise ValueError("この回答は選べません。")
        self.pending[1].set_result(option)
        self.request_info = None

    def begin(self, prompt):
        if not self.session_ready or self.turn_id is not None:
            raise RuntimeError("Junieの新しい入力を受け付けられません。")
        self.prompt_finished = False
        self.response = []
        self.size = 0
        self.turn_id = self.next_turn
        self.next_turn += 1
        self.request(self.turn_id, "session/prompt", sessionId=self.session_id, prompt=[{"type": "text", "text": prompt}])

    def _on_session_ready(self) -> None:
        pass

    def _receive_message(self, message):
        if self.turn_id is not None and message.get("id") == self.turn_id and "method" not in message:
            self.turn_id = None
            error = message.get("error")
            if (isinstance(error, dict) and error.get("code") == -32002
                    and str(error.get("message", "")).strip().lower() == "no models are currently available for submission"):
                raise ValueError("Junieで利用できるモデルがありません。選択したモデルと通常のJunie CLIでの利用可否を確認してください。")
            stop_reason = (message.get("result") or {}).get("stopReason")
            if stop_reason == "cancelled":
                raise ValueError(
                    "Junie側で整理の応答が中断されました。停止操作をしていない場合は、"
                    "選んだプロバイダーの認証・利用資格を確認してから再試行してください。"
                )
            if stop_reason != "end_turn":
                raise ValueError("Junieが整理の応答を完了しませんでした。")
            self.prompt_finished = True
            return True
        if message.get("method") == "session/update" and message.get("params", {}).get("sessionId") == self.session_id:
            update = message["params"].get("update") or {}
            if update.get("sessionUpdate") == "available_commands_update":
                self.offered_skill = any(
                    isinstance(item, dict) and item.get("name") == "harness-optimize-rules"
                    for item in update.get("availableCommands", [])
                )
            if self.turn_id is not None and update.get("sessionUpdate") == "agent_message_chunk":
                content = update.get("content", {})
                part = content.get("text")
                if content.get("type") == "text" and isinstance(part, str):
                    self.size += len(part.encode("utf-8"))
                    if self.size > 131_072:
                        raise ValueError("Junieの整理応答が上限を超えました。")
                    self.response.append(part)
        return super()._receive_message(message)

    def wait(self, *, ready=False, cancelled, publish):
        if cancelled.is_set():
            raise InterruptedError("Junieとの整理を中断しました。")
        if ready and self.process is None:
            self._connect()
        deadline = monotonic() + 600
        waiting = None
        while not cancelled.is_set():
            complete = self.advance()
            publish(self.request_info, "".join(self.response))
            arrived = self.session_ready if ready else complete
            if arrived:
                if ready:
                    # 接続成立と同時に届くコマンド一覧も取り込む。
                    self.advance()
                return "".join(self.response)
            now = monotonic()
            if self.pending:
                waiting = waiting or now
            elif waiting is not None:
                deadline += now - waiting
                waiting = None
            if (not ready and waiting is None and now >= deadline) or self.process.poll() is not None:
                raise ValueError("Junieの整理セッションが応答しません。")
            sleep(0.05)
        self.cancel()
        raise InterruptedError("Junieとの整理を中断しました。")

    def shutdown(self):
        process = self.process
        if process is None:
            return
        # 終了が未確認ならJobとACP接続を保持し、再試行では同じプロセスだけを確認する。
        junie_cli._close_junie(process)
        with suppress(OSError, ValueError, RuntimeError, AttributeError):
            self.close()
        for stream in (process.stdin, process.stdout):
            with suppress(OSError, ValueError):
                stream.close()
        self.process = None


def decode_reply(text):
    cleaned = text.strip().removeprefix("\ufeff").strip()
    try:
        payload = load_unique_json(unwrap_json_fence(cleaned).strip())
    except DuplicateJsonKey:
        raise
    except ValueError:
        return {"message": text}
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), str):
        return {"message": text}
    return {key: payload[key] for key in ("message", "edit", "plan", "unresolved") if key in payload}
