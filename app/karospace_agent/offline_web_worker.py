"""Serve the shared UI protocol over pipes inside the verified offline worker."""
import contextlib
import json
import re
import sys

from .offline_session import OfflineSession
from .offline_ui import local_preview, opening


def run(config):
    output = sys.stdout

    def emit(value):
        output.write(json.dumps(value) + "\n")
        output.flush()

    def reply(identifier, body, status=200):
        emit({"id": identifier, "status": status, "body": body})

    def progress(stream, line):
        preview = local_preview(line, config["workspace"]) if stream == "stdout" else None
        if preview is not None:
            emit({"type": "image", "src": "data:image/png;base64," + preview.decode("ascii"), "group": "", "pieces": "local"})
        else:
            emit({"type": "progress", "stream": stream, "line": line.rstrip()})
            tool = re.fullmatch(r"Running (\w+) locally\.\s*", line)
            if stream == "status" and tool:
                emit({"type": "tool", "text": tool[1] + "()"})

    with contextlib.redirect_stdout(sys.stderr):
        # main() reaches this module only after current/child OS checks pass.
        emit({"worker_verified": True})
        emit({"type": "status", "state": "loading local model…"})
        try:
            session = OfflineSession(config, on_event=lambda text: emit({"type": "assistant", "text": text}), on_progress=progress)
        except Exception:
            emit({"type": "error", "text": "Local model initialization failed. No cloud fallback was started."})
            raise
        emit({"type": "status", "state": "idle"})
        for line in sys.stdin:
            identifier = None
            responded = False
            try:
                request = json.loads(line)
                if not isinstance(request, dict) or set(request) != {"id", "path", "body"}:
                    raise ValueError("invalid_request")
                identifier, path, body = request["id"], request["path"], request["body"]
                if type(identifier) is not int or not isinstance(body, dict):
                    raise ValueError("invalid_request")
                if path == "/auth":
                    reply(identifier, {"label": "offline", "detail": "local model · network blocked", "ok": True, "provider": "offline"})
                elif path == "/opening":
                    reply(identifier, {"text": opening(config)})
                elif path == "/preview":
                    text = body.get("text")
                    if not isinstance(text, str) or not text.strip() or len(text) > 65536:
                        raise ValueError("invalid_request")
                    reply(identifier, session.boundary.preview(text.strip()))
                elif path == "/send":
                    draft_id = body.get("draft_id")
                    if not isinstance(draft_id, str):
                        raise ValueError("invalid_request")
                    if draft_id in session.boundary._recovery_checks:
                        from .recovery import revalidate
                        revalidate(session.boundary, draft_id)
                    text = session.boundary.consume(session.boundary.approve(draft_id))
                    reply(identifier, {"queued": True}, 202)
                    responded = True
                    emit({"type": "user", "text": session.boundary.display(text)})
                    emit({"type": "status", "state": "working"})
                    session.send(text)
                    emit({"type": "status", "state": "idle"})
                elif path == "/history":
                    history = session.boundary.history
                    reply(identifier, {"records": history.recent(), "directory": str(history.root), "warning": history.error})
                elif path == "/recovery/preview":
                    from .recovery import prepare
                    record = session.boundary.history.get(body["session_id"], body["run_id"])
                    reply(identifier, prepare(session.boundary, record))
                else:
                    reply(identifier, {"error": "This action is unavailable offline."}, 400)
            except Exception:
                # Exception values can contain paths or dataset identifiers.
                if not responded:
                    reply(identifier, {"error": "The local request could not be completed. Review its parameters and try again."}, 400)
                else:
                    emit({"type": "error", "text": "The local turn failed. No cloud fallback was started."})
                    emit({"type": "status", "state": "idle"})
