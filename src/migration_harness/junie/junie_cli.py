"""Junie のモード選択・再試行と、起動から終了確認までの実行資源の管理。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from typing import TYPE_CHECKING, Any

from ..execution import (
    HEARTBEAT_SECONDS,
    ExecutionControl,
    OperationCancelled,
    check_cancelled,
    is_controlled,
    junie_brave_requested,
    junie_dashboard_control,
    report_activity,
)
from ..files import has_completion_record, has_symlink_component
from .acp_session import (
    AcpSession, acp_stop_observer, permission_observer, reference_read_scope, steering_input_observer,
)
from .junie_launch import (
    JunieAcpModelUnavailable,
    JunieLaunchError,
    JunieSessionError,
    _launch_process,
    _task_command,
    acp_command,
    build_command,
    launch_diagnostics,
    require_interactive_cli,
)
from .junie_process import JunieProcess
from .junie_thoughts import JunieThoughts
from .session_events import SessionEvents
from .usage import USAGE_FIELDS, UsageDelta

if TYPE_CHECKING:
    from ..runs.editor_queue import LivePrompts


MODEL_LIST_TIMEOUT = 10
DASHBOARD_PROGRESS_INSTRUCTION = (
    "進捗と最終回答は日本語で書いてください。重要な確認や変更に進むときは、"
    "確認できた事実、判断の理由、次に行うことを日本語で1〜2文にまとめて利用者に伝えてください。"
)


def dashboard_reply_style(config, target: str) -> str:
    language = config.get("junie_log_language", "ja")
    tone = config.get("junie_log_tone", "neutral")
    if (language, tone) == ("ja", "neutral"):
        return ""
    lang = "英語" if language == "en" else "日本語"
    style = {
        "neutral": "標準的に",
        "polite": "丁寧に",
        "concise": "要点だけ簡潔に",
        "friendly": "フレンドリーで読みやすく",
        "custom": config.get("junie_log_tone_instruction", ""),
    }[tone]
    return (
        f"今回の{target}だけを{lang}で書いてください。利用者向けの文章には次の文体指定を適用してください。"
        f" {json.dumps(style, ensure_ascii=False)}\n"
        "指定された言い回しや口調に従い、挨拶や書き出しの文言が指定されていれば、そのとおりに使ってください。"
        "文体指定を理由に、作業内容、事実、判断、引用、入力資料に関する制約、機械向けの出力スキーマを変えないでください。"
    )


def dashboard_progress_style(config):
    style = dashboard_reply_style(config, "利用者向けの進捗説明と最終報告")
    if not style:
        return DASHBOARD_PROGRESS_INSTRUCTION
    return (
        style + "進捗説明と最終報告は1〜2文にまとめてください。"
        "コード、検証、入力資料、完了記録、構造化データには文体設定を適用しないでください。"
    )


class JunieEditHandoff(KeyboardInterrupt):
    """修正セッションを閉じ、保存した編集と指示を自動で引き継ぐ。"""


class JunieConnectionError(OSError):
    """Junieがモデルサービスとの通信を再試行しても続行できなかった。"""


class JunieStopError(OSError):
    """終了を確認できなかったため、編集の反映や次の起動へ進めない。"""


def unconfirmed_junie_stop(error: BaseException) -> JunieStopError | None:
    """診断の保存などで別の例外が出ても、未確認の終了を見落とさない。"""
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if isinstance(current, JunieStopError):
            return current
        if id(current) not in seen:
            seen.add(id(current))
            pending.extend(link for link in (current.__context__, current.__cause__) if link is not None)
    return None


def list_models() -> list[dict[str, Any]]:
    """インストール済みCLIのモデル候補を、タスクを開始せずに取得する。"""
    # 親が先に終了しても、継承されたパイプのEOFを待たず所有プロセス群を閉じる。
    with TemporaryFile() as output:
        process = _launch_process(["junie", "list-models"], owned=True, stdin=subprocess.DEVNULL,
                                  stdout=output, stderr=subprocess.DEVNULL)
        try:
            process.wait(timeout=MODEL_LIST_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ValueError("Junieのモデル一覧の取得がタイムアウトしました。") from None
        finally:
            _close_junie(process)
        if process.returncode:
            raise ValueError("このJunie CLIではモデル一覧を取得できませんでした。")
        output.seek(0)
        data = output.read()
    try:
        models = json.loads(data)["models"]
        if not isinstance(models, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
            or not isinstance(item.get("displayName"), str) for item in models
        ):
            raise ValueError
        if any("efforts" in item and (not isinstance(item["efforts"], list)
                                     or any(not isinstance(effort, str) for effort in item["efforts"])) for item in models):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ValueError("Junieのモデル一覧を読み取れませんでした。") from None
    return [{"id": item["id"], "name": item["displayName"],
             **({"efforts": item["efforts"]} if "efforts" in item else {})} for item in models]


def _close_junie(process: subprocess.Popen) -> int:
    if isinstance(process, JunieProcess):
        return process.stop()
    if process.poll() is not None:
        return process.returncode
    try:
        # Windowsのランチャーが起動した子プロセスも閉じてから次へ進む。
        taskkill = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
        subprocess.run(
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            check=True, capture_output=True, timeout=10,
        )
        return process.wait(timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        if process.poll() is not None:
            return process.returncode
        raise OSError("Could not close Junie. Close the current Junie session before resuming.") from exc


def _confirm_dashboard_launch(
    dashboard: ExecutionControl, title: str, detail: str, option: str, label: str,
) -> bool:
    reply = dashboard.on_junie_request({
        "title": title, "detail": detail,
        "options": [
            {"id": "stop", "label": "停止", "kind": "reject_once"},
            {"id": option, "label": label, "kind": "allow_once"},
        ],
    })
    check_cancelled()
    if reply is None:
        return False
    while not reply.done():
        dashboard.cancelled.wait(0.25)
        check_cancelled()
    check_cancelled()
    return reply.result() == option


def _supports_task(executable: str) -> bool:
    process = _launch_process(
        [executable, "--help"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    with process:
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            return False
        finally:
            if process.poll() is None:
                _close_junie(process)
            check_cancelled()
    return process.returncode == 0 and re.search(rb"(?m)^[ \t]*--task(?:[= \t]|\r?$)", output) is not None


launch_input_observer: ContextVar[Callable[[str], None] | None] = ContextVar("launch_input_observer", default=None)
session_closed_observer: ContextVar[Callable[[], None] | None] = ContextVar("session_closed_observer", default=None)


def run_interactive(
    command: list[str], completion_path: Path, token: str, targets: tuple[Path, ...],
    *, report_usage: Callable[[list[int]], None] | None = None,
    accept_result: Callable[[str], None] | None = None,
    prefer_task: bool = False,
    prompts: LivePrompts | None = None,
) -> int:
    dashboard = junie_dashboard_control()
    brave_requested = junie_brave_requested()
    if accept_result is None and dashboard is not None:
        style = dashboard.junie_progress_style or DASHBOARD_PROGRESS_INSTRUCTION
        command = [*command[:-1], f"{command[-1]}\n\n{style}"]
    # 終了コードだけでは移行完了を保証しない。呼び出し元でも完了記録と成果物を検証する。
    task_supported = None
    if prompts is not None:
        prompts.recover()
    if prefer_task and not brave_requested:
        task_supported = _supports_task(command[0])
        if task_supported:
            command = _task_command(command)
            report_activity("Junie を非対話モードで起動します。")
        else:
            require_interactive_cli()
            report_activity("Junie の --task を確認できないため、従来のモードで起動します。")
    delays = (1, 3)
    for attempt in range(len(delays) + 1):
        check_cancelled()
        try:
            invocation = command
            if attempt:
                # 作業場所と完了トークンを引き継ぎ、既に保存された編集の続きを促す。
                invocation = [*command[:-1], command[-1] + (
                    " A model service connection failed in the previous session. Review the edits already in the "
                    "workspace and continue the remaining work without duplicating completed changes."
                )]
            if observe := launch_input_observer.get():
                observe(invocation[-1])
            try:
                return _run_session(invocation, completion_path, token, targets,
                                    report_usage=report_usage, accept_result=accept_result, prompts=prompts)
            except JunieAcpModelUnavailable as exc:
                if unconfirmed_junie_stop(exc) is not None:
                    raise
                if brave_requested:
                    raise JunieSessionError(
                        command, 1, "Brave モードで使える Junie ACP のモデルがありません。モデルを確認するか、"
                        "Brave モードを選択せずに再開してください。非対話モードには切り替えていません。",
                    ) from exc
                # 非対話モードでは操作ごとの確認がなくなるため、自動では切り替えない。
                dashboard = junie_dashboard_control()
                if dashboard is None or task_supported is False:
                    raise
                if task_supported is None and not _supports_task(command[0]):
                    raise
                if not _confirm_dashboard_launch(
                    dashboard, "Junie を非対話モードで再試行",
                    f"{exc.details}\n同じ作業フォルダー、指示、モデル・推論設定で再試行できます。\n"
                    "非対話モードでは、Junie の操作ごとの許可確認はダッシュボードに表示されません。\n\n"
                    f"{completion_path.parent.resolve()}",
                    "headless", "今回のみ非対話モードで再試行",
                ):
                    raise
                report_activity("Junie を非対話モードで再試行します。")
                command = _task_command(command)
            if observe is not None:
                observe(invocation[-1])
            return _run_session([*command[:-1], invocation[-1]], completion_path, token, targets,
                                report_usage=report_usage, accept_result=accept_result, prompts=prompts)
        except JunieConnectionError as exc:
            if unconfirmed_junie_stop(exc) is not None:
                raise
            if attempt == len(delays):
                raise JunieConnectionError(
                    "Junie could not reach the model service after 2 retries. Resume this Run after the connection recovers."
                ) from exc
            print(f"Retrying Junie after a connection failure ({attempt + 1}/{len(delays)})", flush=True)
            report_activity("Junieの通信に失敗しました。再試行を待っています。")
            check_cancelled()
            if is_controlled():
                # 待機時間は維持しつつ、ダッシュボードの停止要求を短い間隔で確認する。
                for _ in range(delays[attempt] * 4):
                    time.sleep(0.25)
                    check_cancelled()
            else:
                time.sleep(delays[attempt])
            check_cancelled()


def _launch_session_process(command, *, headless, dashboard, thought_path, brave_config=None):
    streams = {"stderr": subprocess.PIPE} if thought_path is not None else {}
    if headless:
        return _launch_process(command, owned=True, stdin=subprocess.DEVNULL, **streams)
    if dashboard is None:
        # 対話入力をJunieへ直接渡すため、端末の標準入出力を引き継ぐ。
        return _launch_process(command, owned=True)
    # ACPの指示は初期化後に送るため、起動時の引数から外す。
    invocation = acp_command(command)
    if brave_config is not None:
        invocation.extend(["--config-location", str(brave_config), "--skip-update-check"])
        streams["kill_on_close"] = True
    return _launch_process(invocation, owned=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, **streams)


def _run_session(
    command: list[str], completion_path: Path, token: str, targets: tuple[Path, ...],
    *, report_usage: Callable[[list[int]], None] | None = None,
    accept_result: Callable[[str], None] | None = None,
    prompts: LivePrompts | None = None,
) -> int:
    """起動から子プロセスの終了確認、使用量回収、端末の復元までを担当する。

    対話・ACPは完了記録と対象を確認して早期終了できる。非対話は自然終了を待つ。
    子プロセス群の終了後に終了observerへ通知し、終了時に残った使用量を回収する。
    """
    check_cancelled()
    root = completion_path.parent
    dashboard = junie_dashboard_control()
    if observe := acp_stop_observer.get():
        observe(None)
    headless = command[-2:-1] == ["--task"]
    brave_requested = junie_brave_requested()
    brave = brave_requested and accept_result is None
    if brave_requested and headless:
        raise JunieSessionError(command, 1, "Brave モードを選択した実行は Junie ACP を使用します。")
    restore_terminal = _save_terminal_mode() if dashboard is None and not headless else None
    process = None
    acp = None
    steering_style = None
    thoughts = None
    result_ready = False
    stopped = False
    brave_directory = None

    def stop() -> int:
        nonlocal stopped
        if not stopped:
            try:
                code = _close_junie(process)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                raise JunieStopError(
                    "Junieの終了を確認できませんでした。起動した端末で終了してから再開してください。"
                ) from exc
            stopped = True
            if observe := session_closed_observer.get():
                observe()
            return code
        return process.returncode

    def result_files_ready() -> bool:
        return (
            has_completion_record(completion_path, token)
            and (acp is None or acp.steering_complete())
            and all(
                path.is_file() and not has_symlink_component(root, path.relative_to(root))
                and path.resolve().is_relative_to(root.resolve()) for path in targets
            )
        )

    def collect_session_events(*, cancellable: bool = True) -> bool:
        if thoughts is not None:
            collect_thoughts(thoughts.poll)
        failed = events.read_new_events(cancellable=cancellable)
        if report_usage is not None and any(events.usage):
            # 読み取った差分を渡したら集計を空にし、次回や終了時に二重加算しない。
            delta, events.usage = events.usage, UsageDelta()
            report_usage(delta)
        # 明示した読み取り専用タスクだけ、最終回答を呼び出し元で検証・保存する。
        if cancellable and accept_result is not None and events.result is not None:
            result, events.result = events.result, None
            stop()
            check_cancelled()
            accept_result(result)
        return failed

    def collect_thoughts(read: Callable[[], None]) -> None:
        nonlocal thoughts
        try:
            read()
        except (OSError, ValueError):
            thoughts = None
            report_activity("Junie の詳細ログを保存できませんでした。")

    try:
        if dashboard is not None:
            project = str(root.resolve())
            if project not in dashboard.trusted_junie_workspaces:
                # ACPはプロジェクトの設定を信頼確認なしで読み込むため、起動前に確認する。
                if not _confirm_dashboard_launch(
                    dashboard, "Junie の作業フォルダーを確認",
                    f"このフォルダーの設定を読み込んで Junie を実行します。\n\n{project}",
                    "trust", "信頼して続行",
                ):
                    raise OperationCancelled("Junieの作業フォルダーの確認を取り消しました。")
                dashboard.trusted_junie_workspaces.add(project)
        events = SessionEvents(token, headless_project=root.resolve() if headless else None)
        next_poll = time.monotonic()
        thought_path = dashboard.junie_thoughts_path if dashboard is not None else None
        launch_options = {}
        if brave:
            brave_directory = TemporaryDirectory(prefix="migration-junie-brave-")
            brave_config = Path(brave_directory.name) / "config.json"
            brave_config.write_text('{"brave":true}\n', encoding="ascii")
            launch_options["brave_config"] = brave_config
        process = _launch_session_process(command, headless=headless, dashboard=dashboard,
                                          thought_path=thought_path, **launch_options)
        if dashboard is not None and not headless:
            if accept_result is None and dashboard.junie_progress_style != DASHBOARD_PROGRESS_INSTRUCTION:
                steering_style = dashboard.junie_progress_style
            acp = AcpSession(process, command, Path(project), dashboard.on_junie_request,
                              completion_path, token, prompts, require_brave=brave)
            acp.initialize()
        if thought_path is not None:
            thoughts = JunieThoughts(process.stderr, events.logs, events.existing_logs, thought_path, token)
            if acp is not None and accept_result is None:
                def write_progress(message: str) -> None:
                    if thoughts is not None:
                        collect_thoughts(lambda: thoughts.append_progress(message))

                acp.progress_sink = write_progress
        started = time.monotonic()
        report_activity("Junieのプロセスを起動しました。")
        next_heartbeat = started + HEARTBEAT_SECONDS
        if report_usage is not None:
            report_usage([0] * len(USAGE_FIELDS))
        while True:
            check_cancelled()
            if dashboard is not None and prompts is not None and prompts.needs_handoff():
                report_activity("保存した編集と追加指示を反映するため、現在のJunie修正を切り替えます。")
                raise JunieEditHandoff()
            try:
                exit_code = process.wait(timeout=0.25)
                if isinstance(process, JunieProcess):
                    exit_code = stop()
            except subprocess.TimeoutExpired:
                check_cancelled()
                # 完了記録での早期終了は対話・ACPだけ。非対話モードはプロセスの自然終了を待つ。
                if not headless and result_files_ready():
                    exit_code = stop()
                    check_cancelled()
                    result_ready = result_files_ready()
                    return exit_code
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + 0.5
                    if collect_session_events():
                        stop()
                        raise JunieConnectionError("Junie model service connection failed")
                    if stopped:
                        result_ready = result_files_ready()
                        return 0 if acp is not None else process.returncode
                if acp is not None:
                    try:
                        prompt_finished = acp.advance()
                    except (JunieSessionError, KeyError, TypeError, AttributeError) as exc:
                        if collect_session_events() and not has_completion_record(completion_path, token):
                            raise JunieConnectionError("Junie model service connection failed") from exc
                        if isinstance(exc, JunieSessionError):
                            raise
                        raise JunieSessionError(command, 1, "JunieのACP応答に必要な項目がありません。") from None
                    if prompt_finished:
                        acp.check_steering_turns(events, exited=True)
                        stop()
                        check_cancelled()
                        if collect_session_events() and not has_completion_record(completion_path, token):
                            raise JunieConnectionError("Junie model service connection failed")
                        result_ready = result_files_ready()
                        return 0
                    acp.check_steering_turns(events)
                    acp.steer(steering_style)
                    events.steering_tokens.update(request.token for request in acp.steering_requests.values())
                    dashboard.junie_steering = bool(prompts is not None and acp.steering_supported and acp.session_id
                                                   and not acp.pending and not acp.prompt_finished)
                now = time.monotonic()
                if now >= next_heartbeat:
                    elapsed = int(now - (events.last_event_at if events.last_event_at is not None else started))
                    detail = (
                        f"最後のイベントから{elapsed}秒" if events.last_event_at is not None
                        else f"起動から{elapsed}秒、セッションのイベントはまだ確認できていません"
                    )
                    report_activity(f"Junieのプロセスは起動中です（{detail}）。", kind="heartbeat")
                    next_heartbeat = now + HEARTBEAT_SECONDS
            else:
                check_cancelled()
                if acp is not None and acp.steering_requests:
                    # プロセスが先に終了しても、直前に届いた受信確認を取りこぼさない。
                    acp.reader.join(timeout=1)
                    acp.advance()
                if collect_session_events() and not has_completion_record(completion_path, token):
                    raise JunieConnectionError("Junie model service connection failed")
                if acp is not None:
                    acp.check_steering_turns(events, exited=True)
                # 終了コード0でも、対応するセッションも完了記録もなければ起動失敗として診断する。
                if events.path is None and not has_completion_record(completion_path, token):
                    diagnostics = events.startup_diagnostics(process.pid, command)
                    if brave and acp is not None and not acp.session_ready:
                        diagnostics += ("\nJunie ACP の Brave モードを確認できませんでした。対応する Junie CLI を"
                                        "使用するか、Brave モードを選択せずに再開してください。")
                    raise JunieSessionError(command, exit_code, diagnostics)
                if headless and thoughts is not None and accept_result is None and events.result:
                    collect_thoughts(lambda: thoughts.append_progress(events.result))
                result_ready = result_files_ready()
                return exit_code
    except JunieStopError:
        raise
    except BaseException as exc:
        if process is not None and not stopped and (isinstance(process, JunieProcess) or process.poll() is None):
            if acp is not None and isinstance(exc, KeyboardInterrupt):
                try:
                    acp.cancel()
                except OSError:
                    pass
            stop()
        raise
    finally:
        try:
            if acp is not None:
                acp.flush_progress()
            if thoughts is not None:
                collect_thoughts(thoughts.finish)
            if dashboard is not None:
                dashboard.junie_steering = False
                dashboard.on_junie_request(None)
            if acp is not None:
                try:
                    acp.finish_prompts(result_ready)
                finally:
                    acp.close()
            if process is not None and report_usage is not None:
                # 停止要求が出ていても最後の使用量を回収する。最終回答の採用処理は行わない。
                collect_session_events(cancellable=False)
        finally:
            try:
                if brave_directory is not None:
                    try:
                        brave_directory.cleanup()
                    except OSError:
                        report_activity("Junie の一時 Brave 設定を削除できませんでした。共通設定には適用されていません。")
            finally:
                if restore_terminal is not None and (process is None or process.poll() is not None):
                    restore_terminal()


def _save_terminal_mode() -> Callable[[], None]:
    """強制終了で端末設定が残る場合に備えて、起動前の復元処理を作る。"""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.windll.kernel32
    modes = []
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream.isatty():
            handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
            mode = wintypes.DWORD()
            if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                modes.append((handle, mode))

    def restore() -> None:
        """Junieを起動する前の端末設定に戻す。"""
        for handle, mode in modes:
            kernel.SetConsoleMode(handle, mode)

    return restore
