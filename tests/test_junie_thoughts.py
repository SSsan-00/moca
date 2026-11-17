import io
import json
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

from migration_harness.dashboard import Dashboard
from migration_harness.junie_thoughts import FILENAME, JunieThoughts, read_junie_thoughts


def trajectory(thought, *, agent_type="primary"):
    event = {
        "type": "com.intellij.ml.llm.matterhorn.AssistantTrajectoryElement",
        "subagent": {"agent_type": agent_type},
        "thought": thought,
        "content": "raw-content-must-not-be-exposed",
    }
    line = "2026-11-17 20:56:48.002 [worker] INFO Logger - [ANALYTICS:EVENT] type=trajectory_element properties=element="
    return (line + json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")


class JunieThoughtTests(unittest.TestCase):
    def test_only_the_launched_process_thoughts_are_appended_and_partial_lines_are_retried(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stderr(io.StringIO()):
            root = Path(temporary).resolve()
            logs = root / "logs"
            logs.mkdir()
            run_id = str(uuid.uuid4())
            (root / run_id).mkdir()
            stale = logs / "log-old-pid-100.log"
            stale.write_bytes(trajectory("前の実行"))
            unrelated = logs / "log-parallel-pid-110.log"
            unrelated.write_bytes(trajectory("別の実行"))
            source = logs / "log-new-pid-120.log"
            startup = io.BytesIO((
                f"Junie 26.11.1 (pid=100): log file: {stale}\n"
                f"Junie 26.11.1 (pid=120): log file: {source}\n"
            ).encode())
            tail = JunieThoughts(startup, logs, {stale}, root / run_id / FILENAME, "completion-token")
            tail.reader.join(timeout=1)
            second = trajectory("ポリシーを確認します。")
            source.write_bytes(trajectory("…\n", agent_type="router") + trajectory("...")
                               + trajectory("入力を確認しました。 completion-token") + second[:-6])
            tail.poll()
            first = read_junie_thoughts(root, run_id, 0)
            self.assertGreater(first["cursor"], 0)
            self.assertEqual([row["thought"] for row in first["entries"]], ["入力を確認しました。 [REDACTED]"])
            self.assertNotIn("raw-content-must-not-be-exposed", (root / run_id / FILENAME).read_text())
            self.assertEqual(read_junie_thoughts(root, run_id, first["cursor"])["entries"], [])
            with source.open("ab") as stream:
                stream.write(second[-6:])
            tail.finish()
            self.assertEqual(
                [row["thought"] for row in read_junie_thoughts(root, run_id, first["cursor"])["entries"]],
                ["ポリシーを確認します。"],
            )

    def test_dashboard_reads_the_selected_run_and_rejects_another_run_or_bad_cursor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_id, other = str(uuid.uuid4()), str(uuid.uuid4())
            for identifier, message in ((run_id, "公開対象"), (other, "別の実行")):
                (root / identifier).mkdir()
                (root / identifier / FILENAME).write_text(json.dumps({
                    "at": "2026-11-17T20:56:48+09:00", "thought": message, "extra": "private",
                }) + "\n", encoding="utf-8")
            dashboard = Dashboard()
            dashboard.publish({"run_id": run_id, "ready": False, "operation_id": "running"})
            terminal = io.StringIO()
            with redirect_stdout(terminal):
                server = dashboard.server({"run_root": str(root)}, b"", b"")
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = terminal.getvalue().splitlines()[0].removeprefix("Progress dashboard: ") + "junie-thoughts"
            try:
                def get(identifier, cursor=0):
                    with urlopen(f"{base}?run_id={identifier}&cursor={cursor}", timeout=2) as response:
                        return json.load(response)
                first = get(run_id)
                self.assertEqual(first["entries"], [{"at": "2026-11-17T20:56:48+09:00", "thought": "公開対象"}])
                self.assertTrue(first["live"])
                self.assertEqual(get(run_id, first["cursor"])["entries"], [])
                with self.assertRaises(HTTPError) as error:
                    get(other)
                with error.exception as response:
                    self.assertEqual(response.code, 404)
                with self.assertRaises(HTTPError) as error:
                    get(run_id, -1)
                with error.exception as response:
                    self.assertEqual(response.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
