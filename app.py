
#!/usr/bin/env python3
"""
app.py

Single-page web console for the AWS SOP Agent:
  - Create a catalog (prefix "AWS-SOP-Agent-" added automatically), with dynamic
    fields (Text / Dropdown) and a rich Markdown editor for the SOP.
  - List and delete catalogs (no edit - delete and recreate).
  - Start / stop the polling engine and watch its live logs.

All files live in the project root (no templates/ folder needed): this app points
Flask's template folder at its own directory so index.html sits next to app.py.

Running behind a load balancer on a sub-path (e.g. an ALB rule for /awsagent ->
target group on port 6777)? Set URL_PREFIX="/awsagent" in .env and the whole app,
including its API calls, is served under that path.

Run:  python app.py
All config comes from environment / .env.
"""

import os
import logging
import threading
from collections import deque

from flask import Flask, Blueprint, request, jsonify, render_template

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass

import snow_client as snow
import catalog_service as catalog
import engine_core

HERE = os.path.abspath(os.path.dirname(__file__))
APP_PORT = int(os.getenv("APP_PORT", "8000"))
SOP_AGENT_PREFIX = os.getenv("SOP_AGENT_PREFIX", "AWS-SOP-Agent-")
URL_PREFIX = os.getenv("URL_PREFIX", "").rstrip("/")  # e.g. "/awsagent" behind an ALB

# --------------------------------------------------------------------------- #
# In-memory log capture for the live log view
# --------------------------------------------------------------------------- #
LOG_BUFFER = deque(maxlen=1000)


class BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:
            pass


_fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")
_buf_handler = BufferHandler()
_buf_handler.setFormatter(_fmt)
_console = logging.StreamHandler()
_console.setFormatter(_fmt)

engine_logger = logging.getLogger("sop-engine")
engine_logger.setLevel(logging.INFO)
engine_logger.addHandler(_buf_handler)
engine_logger.addHandler(_console)

# --------------------------------------------------------------------------- #
# Engine thread control
# --------------------------------------------------------------------------- #
_engine_thread = None
_engine_stop = None
_engine_lock = threading.Lock()


def engine_running():
    return _engine_thread is not None and _engine_thread.is_alive()


# index.html lives next to app.py (no templates/ folder).
app = Flask(__name__, template_folder=HERE)
app.url_map.strict_slashes = False   # so /awsagent and /awsagent/ both work

# All routes live on a blueprint so they can be mounted under URL_PREFIX.
bp = Blueprint("sop", __name__)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
@bp.route("/")
def index():
    return render_template("index.html", prefix=SOP_AGENT_PREFIX, base=URL_PREFIX)


@bp.route("/api/config")
def api_config():
    return jsonify({
        "prefix": SOP_AGENT_PREFIX,
        "base": URL_PREFIX,
        "missing_config": engine_core.missing_config(),
    })


# --------------------------------------------------------------------------- #
# Catalogs
# --------------------------------------------------------------------------- #
@bp.route("/api/catalogs", methods=["GET"])
def list_catalogs():
    miss = snow.missing_config()
    if miss:
        return jsonify({"error": f"Missing config: {', '.join(miss)}"}), 400
    try:
        return jsonify({"catalogs": catalog.list_catalogs()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@bp.route("/api/catalogs", methods=["POST"])
def create_catalog():
    miss = snow.missing_config()
    if miss:
        return jsonify({"error": f"Missing config: {', '.join(miss)}"}), 400
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Catalog name is required."}), 400
    fields = data.get("fields") or []
    if not fields:
        return jsonify({"error": "Add at least one field."}), 400
    try:
        result = catalog.create_catalog(
            short_name=name,
            short_description=data.get("short_description", ""),
            sop_markdown=data.get("sop_markdown", ""),
            fields=fields,
        )
        return jsonify(result), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@bp.route("/api/catalogs/<sys_id>", methods=["DELETE"])
def delete_catalog(sys_id):
    try:
        catalog.delete_catalog(sys_id)
        return jsonify({"deleted": sys_id})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@bp.route("/api/orders")
def list_orders():
    miss = snow.missing_config()
    if miss:
        return jsonify({"error": f"Missing config: {', '.join(miss)}"}), 400
    cat = request.args.get("cat_item") or None
    try:
        return jsonify({"orders": catalog.list_orders(cat)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@bp.route("/api/diag/choices")
def diag_choices():
    """Detect the real choices table on this instance and test read/write against it."""
    out = {}
    try:
        # Show every table with "choice" in the name, and which one we auto-detect.
        try:
            rows = snow.get("sys_db_object", "nameLIKEchoice", ["name", "label"], 100)
            out["choice_tables"] = [{"name": r.get("name"), "label": r.get("label")} for r in rows]
        except Exception as exc:  # noqa: BLE001
            out["choice_tables_error"] = str(exc)
        try:
            out["detected_choices_table"] = catalog.choices_table()
        except Exception as exc:  # noqa: BLE001
            out["detect_error"] = str(exc)
        tbl = out.get("detected_choices_table", "question_choices")

        items = snow.get("sc_cat_item", f"nameSTARTSWITH{SOP_AGENT_PREFIX}", ["sys_id", "name"], 25)
        out["catalogs_found"] = [i["name"] for i in items]
        var = None
        for it in items:
            vs = snow.get("item_option_new", f"cat_item={it['sys_id']}^type=5", ["sys_id", "name"], 1)
            if vs:
                var = vs[0]
                out["test_variable"] = f"{vs[0]['name']} (in {it['name']})"
                break
        if not var:
            out["note"] = "No Select Box (dropdown) variable found to test against."
            return jsonify(out)

        try:
            out["existing_choices"] = snow.get(
                tbl, f"question={var['sys_id']}", ["sys_id", "text", "value"], 50)
        except Exception as exc:  # noqa: BLE001
            out["READ_error"] = str(exc)

        try:
            created = snow.create(tbl, {
                "question": var["sys_id"], "text": "DIAG TEST", "value": "diag_test",
                "order": "999", "inactive": "false"})
            out["WRITE_ok"] = True
            try:
                snow.delete(tbl, created["sys_id"])
                out["cleanup"] = "test choice removed"
            except Exception as exc:  # noqa: BLE001
                out["cleanup_error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            out["WRITE_error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return jsonify(out)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
@bp.route("/api/engine/start", methods=["POST"])
def engine_start():
    global _engine_thread, _engine_stop
    miss = engine_core.missing_config()
    if miss:
        return jsonify({"error": f"Missing config: {', '.join(miss)}"}), 400
    with _engine_lock:
        if engine_running():
            return jsonify({"status": "running", "message": "Engine already running"})
        interval = int((request.get_json(silent=True) or {}).get("interval", 30))
        _engine_stop = threading.Event()
        _engine_thread = threading.Thread(
            target=engine_core.run_loop, args=(_engine_stop, interval), daemon=True
        )
        _engine_thread.start()
    return jsonify({"status": "running"})


@bp.route("/api/engine/stop", methods=["POST"])
def engine_stop():
    with _engine_lock:
        if _engine_stop:
            _engine_stop.set()
    return jsonify({"status": "stopping"})


@bp.route("/api/engine/status")
def engine_status():
    return jsonify({"running": engine_running()})


@bp.route("/api/engine/logs")
def engine_logs():
    since = int(request.args.get("since", 0))
    lines = list(LOG_BUFFER)
    return jsonify({"total": len(lines), "lines": lines[since:]})


# Mount everything under URL_PREFIX (empty string = root).
app.register_blueprint(bp, url_prefix=URL_PREFIX)


# Stable health-check path that ignores URL_PREFIX. Point your ALB target group
# health check here (e.g. /healthz) so it passes regardless of the sub-path.
@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    print(f" * Console at path: {URL_PREFIX or '/'}  (health check: /healthz)")
    app.run(host="0.0.0.0", port=APP_PORT, debug=False, threaded=True)
