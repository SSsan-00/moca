import json
from socket import SHUT_RDWR
import sys
from urllib.parse import parse_qs, urlsplit

from benchmark import Workload, main


class HistoryWindow(Workload):
    def __init__(self, config):
        super().__init__(config)
        self.stops = []
        for index, view in enumerate(self.views.values(), 1):
            view.begin_operation(f"operation-{index}")

    def advance_while_disconnected(self, run_id, count):
        view = self.views[run_id]
        with view.condition:
            before = view.revision
            assert view.control.cancelled.is_set() and view.state["stop_requested"]
            with self.lock:
                connection = self.sockets[run_id]
            connection.shutdown(SHUT_RDWR)
            # Publish under the condition lock so any reconnect only sees the completed buffer.
            for index in range(count - (count > 100)):
                view.activity({"kind": "command", "at": 1_700_000_000 + before + index,
                               "message": f"合成ログ {before + index + 1}"})
            if count > 100:
                view.begin_operation("operation-2-new")
            assert view.revision - before == count
            first, last = view.events[0][0], view.events[-1][0]
            return {"before": before, "first": first, "last": last, "retained": len(view.events)}

    def handler(self, base):
        workload = self

        class WindowHandler(super().handler(base)):
            def read_post_action(self, route):
                action = super().read_post_action(route)
                if route == "control" and action.get("action") == "stop":
                    with workload.lock:
                        workload.stops.append({key: action[key] for key in ("run_id", "operation_id", "revision")})
                return action

            def send_get_route(self):
                url = urlsplit(self.path)
                route = url.path.removeprefix(self.server.prefix)
                args = parse_qs(url.query)
                if route == "bench-window":
                    count = int(args["count"][0])
                    assert count in {20, 100, 137}
                    data = workload.advance_while_disconnected(args["run"][0], count)
                elif route == "bench-window-controls":
                    with workload.lock:
                        data = {"stops": list(workload.stops)}
                    data["cancelled"] = {run: view.control.cancelled.is_set() for run, view in workload.views.items()}
                else:
                    return super().send_get_route()
                self.send_body(200, "application/json", json.dumps(data).encode())

        return WindowHandler


if __name__ == "__main__":
    sys.exit(main("history-window.mjs", HistoryWindow))
