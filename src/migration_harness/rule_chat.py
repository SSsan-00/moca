from __future__ import annotations

import json
import subprocess
from concurrent.futures import Future
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from . import junie_cli


class RuleChat(junie_cli._AcpSession):
    def _connection_timeout(self) -> float:
        return 60

    def __init__(self, project, model, effort):
        self.response = []
        self.size = 0
        self.request_info = None
        self.offered_skill = False
        self.turn_id = None
        self.next_turn = 10
        command = junie_cli.build_command(project, "", None, model=model, effort=effort)
        process = junie_cli._launch_process(
            junie_cli.acp_command(command), owned=True, cwd=project,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        super().__init__(process, command, project, self.show_permission, project / "unused", "", None)
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
        if not self.session_id or self.selecting_lite or self.turn_id is not None:
            raise RuntimeError("Junieの新しい入力を受け付けられません。")
        self.prompt_finished = False
        self.response = []
        self.size = 0
        self.turn_id = self.next_turn
        self.next_turn += 1
        self.request(self.turn_id, "session/prompt", sessionId=self.session_id, prompt=[{"type": "text", "text": prompt}])

    def _receive_message(self, message):
        if message.get("id") == 2 and "method" not in message:
            result = message.get("result") or {}
            session = result.get("sessionId")
            if "error" in message or not isinstance(session, str) or not session:
                raise ValueError("Junieの整理セッションを開始できません。")
            self.session_id = session
            self._select_lite_if_missing(result)
            return False
        if (message.get("id") == junie_cli._ACP_SELECT_LITE_REQUEST and "method" not in message
                and self.selecting_lite):
            self._complete_lite_selection(message)
            return False
        if self.turn_id is not None and message.get("id") == self.turn_id and "method" not in message:
            self.turn_id = None
            error = message.get("error")
            if (isinstance(error, dict) and error.get("code") == -32002
                    and str(error.get("message", "")).strip().lower() == "no models are currently available for submission"):
                raise ValueError("Junieで利用できるモデルがありません。選択したモデルと通常のJunie CLIでの利用可否を確認してください。")
            if (message.get("result") or {}).get("stopReason") != "end_turn":
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
        deadline = monotonic() + 600
        waiting = None
        while not cancelled.is_set():
            complete = self.advance()
            publish(self.request_info, "".join(self.response))
            arrived = self.session_id is not None and not self.selecting_lite if ready else complete
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
        try:
            junie_cli._close_junie(self.process)
        finally:
            self.close()


def decode_reply(text):
    cleaned = text.strip()
    if cleaned.startswith("```json\n") and cleaned.endswith("```"):
        cleaned = cleaned[8:-3].strip()
    try:
        payload = json.loads(cleaned)
    except ValueError:
        return {"message": text}
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), str):
        return {"message": text}
    return {key: payload[key] for key in ("message", "edit", "plan", "unresolved") if key in payload}
