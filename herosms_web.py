from flask import Flask, jsonify, request, render_template_string
import httpx
import threading
import time
from datetime import datetime, timedelta

API_KEY = "3f392525d7d57d7fB04f819627d29202"
BASE_URL = "https://hero-sms.com/stubs/handler_api.php"
SERVICE = "wa"
COUNTRY = "4"
MAX_PRICE = "0.3"
DEFAULT_TIMEOUT = 600
POLL_INTERVAL = 5

app = Flask(__name__)

state_lock = threading.Lock()
aktivasi = []
riwayat = []
terminal_logs = []
MAX_LOG_LINES = 300
buy_loop_thread = None
buy_loop_stop_event = threading.Event()
buy_loop_state = {
    "running": False,
    "count_per_cycle": 1,
    "interval_sec": 3,
    "started_at": None,
}
HISTORY_PARAM_CANDIDATES = [
    ("datefrom", "dateto"),
    ("dateFrom", "dateTo"),
    ("startDate", "endDate"),
    ("start_date", "end_date"),
    ("from", "to"),
]


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def add_terminal_log(level, message):
    line = f"[{now_str()}] [{level}] {message}"
    with state_lock:
        terminal_logs.append(line)
        if len(terminal_logs) > MAX_LOG_LINES:
            del terminal_logs[: len(terminal_logs) - MAX_LOG_LINES]


def _set_buy_loop_state(running=None, count_per_cycle=None, interval_sec=None, started_at=None):
    with state_lock:
        if running is not None:
            buy_loop_state["running"] = running
        if count_per_cycle is not None:
            buy_loop_state["count_per_cycle"] = count_per_cycle
        if interval_sec is not None:
            buy_loop_state["interval_sec"] = interval_sec
        if started_at is not None:
            buy_loop_state["started_at"] = started_at


def get_buy_loop_state():
    with state_lock:
        return dict(buy_loop_state)


def _find_activation(activation_id):
    for item in aktivasi:
        if item["id"] == activation_id:
            return item
    return None


def _find_history(activation_id):
    for item in riwayat:
        if item["id"] == activation_id:
            return item
    return None


def upsert_history(activation_id, nomor="-", status="SELESAI", otp="-", source="server"):
    with state_lock:
        item = _find_history(activation_id)
        if item is None:
            item = {
                "id": activation_id,
                "nomor": nomor or "-",
                "status": status or "SELESAI",
                "otp": otp or "-",
                "source": source,
                "created_at": now_str(),
                "updated_at": now_str(),
                "moved_to_history_at": now_str(),
            }
            riwayat.append(item)
            return item

        item["nomor"] = nomor or item.get("nomor", "-")
        item["status"] = status or item.get("status", "SELESAI")
        item["otp"] = otp if otp is not None else item.get("otp", "-")
        item["source"] = source
        item["updated_at"] = now_str()
        return item


def is_terminal_status(status):
    s = str(status or "").upper()
    terminal = ("SELESAI", "DIBATALKAN", "TIMEOUT", "EXPIRED")
    return any(x in s for x in terminal)


def move_to_history(activation_id, final_status=None):
    with state_lock:
        item = _find_activation(activation_id)
        if item is None:
            return None

        aktivasi.remove(item)
        item["status"] = final_status or item.get("status", "EXPIRED")
        item["updated_at"] = now_str()
        item["moved_to_history_at"] = now_str()

        old = _find_history(activation_id)
        if old:
            riwayat.remove(old)
        riwayat.append(item)
        return item


def upsert_activation(activation_id, nomor=None, status=None, otp=None, source="local"):
    with state_lock:
        item = _find_activation(activation_id)
        if item is None:
            # Kalau sudah ada di history, jangan bikin duplikat; update di history saja.
            history_item = _find_history(activation_id)
            if history_item:
                if nomor:
                    history_item["nomor"] = nomor
                if status:
                    history_item["status"] = status
                if otp is not None:
                    history_item["otp"] = otp
                history_item["source"] = source
                history_item["updated_at"] = now_str()
                return history_item

            item = {
                "id": activation_id,
                "nomor": nomor or "-",
                "status": status or "MENUNGGU OTP",
                "otp": otp or "-",
                "source": source,
                "created_at": now_str(),
                "updated_at": now_str(),
            }
            aktivasi.append(item)
            return item

        if nomor:
            item["nomor"] = nomor
        if status:
            item["status"] = status
        if otp is not None:
            item["otp"] = otp
        item["source"] = source
        item["updated_at"] = now_str()
        return item


def map_status_server(status_code):
    mapping = {
        "1": "MENUNGGU OTP",
        "2": "OTP MASUK",
        "3": "SELESAI",
        "4": "MENUNGGU SMS",
        "6": "DIBATALKAN",
        "8": "DIBATALKAN",
    }
    return mapping.get(str(status_code), f"STATUS {status_code}")


def call_api(params):
    with httpx.Client(timeout=15) as client:
        return client.get(BASE_URL, params=params)


def sync_from_server():
    try:
        r = call_api({"action": "getActiveActivations", "api_key": API_KEY})
        payload = r.json()
    except Exception as e:
        add_terminal_log("ERROR", f"SYNC gagal: {e}")
        return {"new": 0, "updated": 0, "rows": 0}

    if payload.get("status") != "success":
        add_terminal_log("WARN", f"SYNC endpoint tidak sukses: {payload}")
        return {"new": 0, "updated": 0, "rows": 0}

    rows = payload.get("data") or payload.get("activeActivations", {}).get("rows") or []
    new_count = 0
    upd_count = 0

    seen_ids = set()
    for row in rows:
        activation_id = str(row.get("activationId", "")).strip()
        if not activation_id:
            continue
        seen_ids.add(activation_id)
        nomor = str(row.get("phoneNumber", "-")).strip() or "-"
        otp = row.get("smsCode") or "-"
        status = map_status_server(row.get("activationStatus"))

        before = _find_activation(activation_id)
        upsert_activation(activation_id, nomor=nomor, status=status, otp=otp, source="server")
        if is_terminal_status(status):
            move_to_history(activation_id, final_status=status)
        if before is None:
            new_count += 1
        else:
            upd_count += 1

    # Aktivasi yang sebelumnya aktif tapi tidak ada lagi di endpoint aktif -> expired.
    with state_lock:
        expired_ids = [item["id"] for item in aktivasi if item["id"] not in seen_ids]
    for activation_id in expired_ids:
        move_to_history(activation_id, final_status="EXPIRED")
    if expired_ids:
        add_terminal_log("INFO", f"EXPIRED dipindah ke history: {len(expired_ids)} item")

    add_terminal_log("INFO", f"SYNC selesai +{new_count} baru, {upd_count} update, rows={len(rows)}")
    return {"new": new_count, "updated": upd_count, "rows": len(rows)}


def sync_history_from_server(days=7):
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days)
    date_candidates = [
        (start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")),
        (
            start_dt.strftime("%Y-%m-%d 00:00:00"),
            end_dt.strftime("%Y-%m-%d 23:59:59"),
        ),
    ]

    raw_payload = None
    used_params = None

    for k_start, k_end in HISTORY_PARAM_CANDIDATES:
        for start_val, end_val in date_candidates:
            params = {
                "action": "getHistory",
                "api_key": API_KEY,
                k_start: start_val,
                k_end: end_val,
            }
            try:
                r = call_api(params)
                payload = r.json()
            except Exception:
                continue

            if payload.get("status") == "success":
                raw_payload = payload
                used_params = params
                break
            if raw_payload:
                break
        if raw_payload:
            break

    if raw_payload is None:
        add_terminal_log("WARN", "HISTORY server belum bisa diambil (provider balas WRONG_DATE).")
        return {"new": 0, "updated": 0, "rows": 0, "ok": False}

    rows = (
        raw_payload.get("data")
        or raw_payload.get("rows")
        or raw_payload.get("history")
        or []
    )
    if isinstance(rows, dict):
        rows = [rows]

    new_count = 0
    upd_count = 0

    for row in rows:
        activation_id = str(
            row.get("activationId")
            or row.get("id")
            or row.get("orderId")
            or ""
        ).strip()
        if not activation_id:
            continue

        nomor = str(row.get("phoneNumber") or row.get("phone") or "-").strip() or "-"
        otp = row.get("smsCode") or row.get("code") or "-"
        status = map_status_server(row.get("activationStatus") or row.get("status") or "3")

        exists = _find_history(activation_id)
        upsert_history(activation_id, nomor=nomor, status=status, otp=otp, source="server")
        if exists is None:
            new_count += 1
        else:
            upd_count += 1

        if _find_activation(activation_id):
            move_to_history(activation_id, final_status=status)

    add_terminal_log(
        "INFO",
        f"HISTORY sync ok +{new_count} baru, {upd_count} update, rows={len(rows)} via {used_params}",
    )
    return {"new": new_count, "updated": upd_count, "rows": len(rows), "ok": True}


def buy_one_number():
    r = call_api(
        {
            "action": "getNumber",
            "service": SERVICE,
            "country": COUNTRY,
            "maxPrice": MAX_PRICE,
            "api_key": API_KEY,
        }
    )
    data = r.text.strip()

    if not data.startswith("ACCESS_NUMBER"):
        add_terminal_log("WARN", f"BELI gagal: {data}")
        return None, data

    parts = data.split(":")
    if len(parts) < 3:
        add_terminal_log("WARN", f"BELI response tidak valid: {data}")
        return None, data

    activation_id = parts[1]
    nomor = parts[2]
    upsert_activation(activation_id, nomor=nomor, status="MENUNGGU OTP", otp="-", source="local")
    add_terminal_log("OK", f"BELI sukses id={activation_id} nomor={nomor}")
    return {"id": activation_id, "nomor": nomor}, None


def cancel_activation(activation_id):
    r = call_api(
        {
            "action": "setStatus",
            "status": "8",
            "id": activation_id,
            "api_key": API_KEY,
        }
    )
    upsert_activation(activation_id, status="DIBATALKAN", source="local")
    move_to_history(activation_id, final_status="DIBATALKAN")
    add_terminal_log("WARN", f"CANCEL id={activation_id} -> {r.text.strip()}")
    return r.text.strip()


def poll_otp(activation_id, nomor, timeout=DEFAULT_TIMEOUT, interval=POLL_INTERVAL):
    started = time.time()
    add_terminal_log("INFO", f"POLL mulai id={activation_id} nomor={nomor}")

    while True:
        try:
            r = call_api({"action": "getStatus", "id": activation_id, "api_key": API_KEY})
            status = r.text.strip()
        except Exception as e:
            add_terminal_log("ERROR", f"POLL error id={activation_id}: {e}")
            time.sleep(interval)
            continue

        if status.startswith("STATUS_OK"):
            parts = status.split(":", 1)
            otp = parts[1] if len(parts) > 1 else "-"
            upsert_activation(activation_id, nomor=nomor, status="OTP MASUK", otp=otp, source="local")
            with open("hasil_otp.txt", "a", encoding="utf-8") as f:
                f.write(f"{nomor} | OTP: {otp}\n")
            add_terminal_log("OK", f"OTP masuk id={activation_id} nomor={nomor} otp={otp}")
            return

        if time.time() - started > timeout:
            cancel_activation(activation_id)
            upsert_activation(activation_id, nomor=nomor, status="TIMEOUT/CANCEL", otp="-", source="local")
            move_to_history(activation_id, final_status="TIMEOUT/CANCEL")
            add_terminal_log("WARN", f"POLL timeout id={activation_id} nomor={nomor}")
            return

        time.sleep(interval)


def start_poll_thread(activation_id, nomor):
    t = threading.Thread(target=poll_otp, args=(activation_id, nomor), daemon=True)
    t.start()


def buy_loop_worker(count_per_cycle, interval_sec):
    _set_buy_loop_state(
        running=True,
        count_per_cycle=count_per_cycle,
        interval_sec=interval_sec,
        started_at=now_str(),
    )
    add_terminal_log(
        "INFO",
        f"AUTO BUY started: count_per_cycle={count_per_cycle}, interval={interval_sec}s",
    )

    while not buy_loop_stop_event.is_set():
        success = 0
        fails = 0

        for _ in range(count_per_cycle):
            if buy_loop_stop_event.is_set():
                break
            result, err = buy_one_number()
            if result:
                success += 1
                start_poll_thread(result["id"], result["nomor"])
            else:
                fails += 1
                add_terminal_log("WARN", f"AUTO BUY gagal: {err or 'UNKNOWN_ERROR'}")

        add_terminal_log("INFO", f"AUTO BUY cycle: success={success}, fails={fails}")

        if buy_loop_stop_event.wait(interval_sec):
            break

    _set_buy_loop_state(running=False)
    add_terminal_log("INFO", "AUTO BUY stopped.")


@app.get("/")
def index():
    return render_template_string(HTML)


@app.get("/api/activations")
def api_activations():
    with state_lock:
        items = list(aktivasi)
    items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return jsonify({"status": "ok", "data": items, "total": len(items), "ts": now_str()})


@app.get("/api/history")
def api_history():
    with state_lock:
        items = list(riwayat)
    items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return jsonify({"status": "ok", "data": items, "total": len(items), "ts": now_str()})


@app.get("/api/logs")
def api_logs():
    limit_raw = request.args.get("limit", "120")
    try:
        limit = max(1, min(int(limit_raw), 500))
    except Exception:
        limit = 120

    with state_lock:
        data = terminal_logs[-limit:]
    return jsonify({"status": "ok", "lines": data, "total": len(data), "ts": now_str()})


@app.get("/api/balance")
def api_balance():
    r = call_api({"action": "getBalance", "api_key": API_KEY})
    return jsonify({"status": "ok", "balance": r.text.strip(), "ts": now_str()})


@app.post("/api/sync")
def api_sync():
    include_history = str(request.args.get("include_history", "0")).lower() in ("1", "true", "yes")
    info_active = sync_from_server()
    if include_history:
        info_history = sync_history_from_server(days=7)
    else:
        info_history = {"ok": False, "skipped": True, "new": 0, "updated": 0, "rows": 0}
    return jsonify({"status": "ok", "sync": info_active, "history_sync": info_history, "ts": now_str()})


@app.post("/api/buy")
def api_buy():
    payload = request.get_json(silent=True) or {}
    count = payload.get("count", 1)

    try:
        count = int(count)
    except Exception:
        return jsonify({"status": "error", "message": "count harus angka"}), 400

    if count <= 0:
        return jsonify({"status": "error", "message": "count minimal 1"}), 400

    if count > 100:
        return jsonify({"status": "error", "message": "count maksimal 100 per request"}), 400

    bought = []
    errors = []

    for _ in range(count):
        result, err = buy_one_number()
        if result:
            bought.append(result)
            start_poll_thread(result["id"], result["nomor"])
        else:
            errors.append(err or "UNKNOWN_ERROR")

    return jsonify(
        {
            "status": "ok",
            "requested": count,
            "bought": len(bought),
            "errors": errors,
            "items": bought,
            "ts": now_str(),
        }
    )


@app.post("/api/buy/start")
def api_buy_start():
    global buy_loop_thread

    payload = request.get_json(silent=True) or {}
    count = payload.get("count", 1)
    interval_sec = payload.get("interval_sec", 3)

    try:
        count = int(count)
        interval_sec = int(interval_sec)
    except Exception:
        return jsonify({"status": "error", "message": "count/interval harus angka"}), 400

    if count <= 0:
        return jsonify({"status": "error", "message": "count minimal 1"}), 400
    if interval_sec <= 0:
        return jsonify({"status": "error", "message": "interval minimal 1 detik"}), 400

    if buy_loop_thread and buy_loop_thread.is_alive():
        return jsonify({"status": "error", "message": "AUTO BUY sudah berjalan"}), 409

    buy_loop_stop_event.clear()
    buy_loop_thread = threading.Thread(
        target=buy_loop_worker,
        args=(count, interval_sec),
        daemon=True,
    )
    buy_loop_thread.start()

    return jsonify(
        {
            "status": "ok",
            "message": "AUTO BUY dimulai",
            "buy_loop": get_buy_loop_state(),
            "ts": now_str(),
        }
    )


@app.post("/api/buy/stop")
def api_buy_stop():
    buy_loop_stop_event.set()
    return jsonify(
        {
            "status": "ok",
            "message": "Permintaan stop AUTO BUY dikirim",
            "buy_loop": get_buy_loop_state(),
            "ts": now_str(),
        }
    )


@app.get("/api/buy/status")
def api_buy_status():
    running = bool(buy_loop_thread and buy_loop_thread.is_alive())
    if get_buy_loop_state().get("running") != running:
        _set_buy_loop_state(running=running)
    return jsonify({"status": "ok", "buy_loop": get_buy_loop_state(), "ts": now_str()})


@app.post("/api/cancel")
def api_cancel():
    payload = request.get_json(silent=True) or {}
    activation_id = str(payload.get("activation_id", "")).strip()
    if not activation_id:
        return jsonify({"status": "error", "message": "activation_id wajib diisi"}), 400

    resp = cancel_activation(activation_id)
    return jsonify({"status": "ok", "response": resp, "activation_id": activation_id, "ts": now_str()})


HTML = """
<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Frostack OTP</title>
  <style>
    :root {
      --bg1: #ffffff;
      --bg2: #f8fafc;
      --card: #ffffffee;
      --line: #bbf7d0;
      --text: #111827;
      --muted: #4b5563;
      --ok: #22c55e;
      --warn: #facc15;
      --err: #fb7185;
      --brand: #16a34a;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Plus Jakarta Sans", "Segoe UI", sans-serif;
      color: var(--text);
      background: radial-gradient(1200px 500px at 10% -10%, #22c55e22, transparent), linear-gradient(140deg, var(--bg1), var(--bg2));
      min-height: 100vh;
      padding: 24px;
    }
    .wrap { max-width: 1200px; margin: 0 auto; }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 16px;
      flex-wrap: wrap;
    }
    .title { font-size: 30px; font-weight: 800; letter-spacing: 0.2px; }
    .muted { color: var(--muted); font-size: 13px; }
    .grid {
      display: grid;
      grid-template-columns: 1.2fr 2fr;
      gap: 16px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 16px;
      backdrop-filter: blur(4px);
    }
    .controls { display: grid; gap: 10px; }
    .row { display: flex; gap: 8px; flex-wrap: wrap; }
    .row > * { flex: 1 1 160px; }
    input {
      background: #ffffff;
      color: var(--text);
      border: 1px solid #86efac;
      border-radius: 10px;
      padding: 10px 12px;
      min-width: 120px;
      outline: none;
    }
    button {
      border: 0;
      border-radius: 10px;
      padding: 10px 14px;
      cursor: pointer;
      color: #022c22;
      font-weight: 700;
      background: linear-gradient(135deg, #4ade80, #16a34a);
    }
    button.secondary {
      background: #f0fdf4;
      color: #166534;
      border: 1px solid #86efac;
    }
    button.danger {
      background: #fff1f2;
      color: #9f1239;
      border: 1px solid #fecdd3;
    }
    button.is-stop {
      background: #fff1f2;
      color: #9f1239;
      border: 1px solid #fecdd3;
    }
    .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 10px; }
    .stat { background: #f0fdf4; border: 1px solid var(--line); border-radius: 10px; padding: 10px; }
    .stat b { font-size: 18px; }
    .table-wrap { overflow: auto; max-height: 70vh; border-radius: 12px; border: 1px solid var(--line); }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td { padding: 10px; border-bottom: 1px solid #dcfce7; text-align: left; white-space: nowrap; }
    th { position: sticky; top: 0; background: #f0fdf4; z-index: 1; }
    .badge { padding: 4px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; }
    .s-ok { background: #14532d; color: #bbf7d0; }
    .s-wait { background: #3f6212; color: #fef08a; }
    .s-cancel { background: #4c0519; color: #fbcfe8; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .log { margin-top: 10px; font-size: 13px; color: var(--muted); min-height: 18px; }
    .terminal-title { margin: 12px 0 8px; font-weight: 700; color: #166534; }
    .terminal {
      background: #0b1a12;
      color: #86efac;
      border: 1px solid #166534;
      border-radius: 10px;
      padding: 10px;
      height: 220px;
      overflow: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .pager {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 10px;
    }
    .pager button {
      min-width: 34px;
      padding: 6px 10px;
      border-radius: 8px;
      font-size: 12px;
      background: #f0fdf4;
      color: #166534;
      border: 1px solid #86efac;
      font-weight: 700;
    }
    .pager button.active {
      background: linear-gradient(135deg, #4ade80, #16a34a);
      color: #022c22;
    }
    @media (max-width: 960px) {
      .grid { grid-template-columns: 1fr; }
      .title { font-size: 24px; }
    }
    @media (max-width: 640px) {
      body { padding: 10px; }
      .wrap { max-width: 100%; }
      .header { margin-bottom: 10px; align-items: flex-start; }
      #clock { width: 100%; text-align: left; }
      .title { font-size: 18px; line-height: 1.25; }
      .muted { font-size: 12px; }
      .card { padding: 12px; border-radius: 12px; }
      h3 { margin: 6px 0 10px; font-size: 20px; }
      .row { gap: 6px; }
      .row > * { flex: 1 1 100%; width: 100%; }
      input, button { width: 100%; min-height: 44px; }
      .stats { grid-template-columns: 1fr; }
      .table-wrap { max-height: 50vh; overflow-x: auto; -webkit-overflow-scrolling: touch; }
      table { font-size: 12px; min-width: 560px; }
      th, td { padding: 8px; }
      .terminal { height: 170px; font-size: 11px; }
      .pager { justify-content: center; }
      .pager button { min-height: 36px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <div>
        <div class="title">Frostack OTP</div>
        <div class="muted">Dashboard Monitoring OTP</div>
      </div>
      <div class="muted" id="clock">-</div>
    </div>

    <div class="grid">
      <section class="card">
        <h3>Kontrol</h3>
        <div class="controls">
          <div class="row">
            <input id="buyCount" type="number" min="1" value="1" />
            <button id="btnToggleBuy">Start Beli Auto</button>
          </div>
          <div class="row">
            <button class="secondary" id="btnSync">Sync Endpoint</button>
            <button class="secondary" id="btnBalance">Cek Saldo</button>
          </div>
          <div class="row">
            <input id="cancelId" placeholder="Activation ID" />
            <button class="secondary" id="btnCancel">Cancel ID</button>
          </div>
        </div>

        <div class="stats">
          <div class="stat"><div class="muted">Total</div><b id="stTotal">0</b></div>
          <div class="stat"><div class="muted">OTP Masuk</div><b id="stOtp">0</b></div>
          <div class="stat"><div class="muted">Menunggu</div><b id="stWait">0</b></div>
        </div>
        <div class="log" id="log"></div>
        <div class="terminal-title">Terminal</div>
        <div class="terminal" id="terminal"></div>
      </section>

      <section class="card">
        <h3>Tabel Nomor & OTP (Aktif)</h3>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>No</th>
                <th>Activation ID</th>
                <th>Nomor</th>
                <th>Status</th>
                <th>OTP</th>
                <th>Update</th>
              </tr>
            </thead>
            <tbody id="tbody"></tbody>
          </table>
        </div>
        <div class="pager" id="pagerActive"></div>
        <h3 style="margin-top:14px;">Tabel History</h3>
        <div class="table-wrap" style="max-height: 38vh;">
          <table>
            <thead>
              <tr>
                <th>No</th>
                <th>Activation ID</th>
                <th>Nomor</th>
                <th>Status Akhir</th>
                <th>OTP</th>
                <th>Update</th>
              </tr>
            </thead>
            <tbody id="tbodyHistory"></tbody>
          </table>
        </div>
        <div class="pager" id="pagerHistory"></div>
      </section>
    </div>
  </div>

<script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
<script>
const el = (id) => document.getElementById(id);
const PAGE_SIZE = 5;
let activePage = 1;
let historyPage = 1;

function statusClass(status) {
  const s = (status || "").toUpperCase();
  if (s.includes("OTP") || s.includes("SELESAI")) return "s-ok";
  if (s.includes("BATAL") || s.includes("TIMEOUT")) return "s-cancel";
  return "s-wait";
}

function log(msg) {
  el("log").textContent = msg;
}

function renderBuyStatus(info) {
  const running = !!(info && info.running);
  const btn = el("btnToggleBuy");
  if (!btn) return;
  btn.textContent = running ? "Stop Beli" : "Start Beli Auto";
  btn.classList.toggle("is-stop", running);
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

async function api(url, opts = {}) {
  const res = await fetch(url, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json();
  if (!res.ok) throw new Error(data.message || "Request gagal");
  return data;
}

async function loadTable() {
  const data = await api("/api/activations");
  const rows = data.data || [];
  const tbody = el("tbody");
  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  if (activePage > totalPages) activePage = totalPages;
  const start = (activePage - 1) * PAGE_SIZE;
  const pageRows = rows.slice(start, start + PAGE_SIZE);
  tbody.innerHTML = pageRows.map((r, i) => `
    <tr>
      <td>${start + i + 1}</td>
      <td class="mono">${r.id || "-"}</td>
      <td class="mono">${r.nomor || "-"}</td>
      <td><span class="badge ${statusClass(r.status)}">${r.status || "-"}</span></td>
      <td class="mono">${r.otp || "-"}</td>
      <td>${r.updated_at || "-"}</td>
    </tr>
  `).join("");
  renderPager("pagerActive", totalPages, activePage, (p) => { activePage = p; loadTable(); });

  const otpCount = rows.filter(x => (x.status || "").toUpperCase().includes("OTP")).length;
  const waitCount = rows.filter(x => {
    const s = (x.status || "").toUpperCase();
    return s.includes("MENUNGGU") || s.includes("STATUS 1") || s.includes("STATUS 4");
  }).length;

  el("stTotal").textContent = rows.length;
  el("stOtp").textContent = otpCount;
  el("stWait").textContent = waitCount;
  el("clock").textContent = `Update: ${data.ts || "-"}`;
}

async function loadHistory() {
  const data = await api("/api/history");
  const rows = data.data || [];
  const tbody = el("tbodyHistory");
  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  if (historyPage > totalPages) historyPage = totalPages;
  const start = (historyPage - 1) * PAGE_SIZE;
  const pageRows = rows.slice(start, start + PAGE_SIZE);
  tbody.innerHTML = pageRows.map((r, i) => `
    <tr>
      <td>${start + i + 1}</td>
      <td class="mono">${r.id || "-"}</td>
      <td class="mono">${r.nomor || "-"}</td>
      <td><span class="badge ${statusClass(r.status)}">${r.status || "-"}</span></td>
      <td class="mono">${r.otp || "-"}</td>
      <td>${r.updated_at || "-"}</td>
    </tr>
  `).join("");
  renderPager("pagerHistory", totalPages, historyPage, (p) => { historyPage = p; loadHistory(); });
}

function renderPager(targetId, totalPages, currentPage, onPageChange) {
  const wrap = el(targetId);
  if (!wrap) return;
  if (totalPages <= 1) {
    wrap.innerHTML = "";
    return;
  }

  const pages = [];
  for (let i = 1; i <= totalPages; i += 1) pages.push(i);
  wrap.innerHTML = pages.map((p) => `<button data-page="${p}" class="${p === currentPage ? "active" : ""}">${p}</button>`).join("");
  wrap.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => onPageChange(Number(btn.dataset.page)));
  });
}

async function loadTerminal() {
  const data = await api("/api/logs?limit=150");
  const term = el("terminal");
  const lines = data.lines || [];
  term.innerHTML = lines.map(line => escapeHtml(line)).join("<br>");
  term.scrollTop = term.scrollHeight;
}

async function doSync(includeHistory = false) {
  const qs = includeHistory ? "?include_history=1" : "";
  const res = await api(`/api/sync${qs}`, { method: "POST", body: "{}" });
  const h = res.history_sync || { ok: false, new: 0, updated: 0, rows: 0 };
  const hText = h.skipped
    ? "History: skip"
    : h.ok
    ? `History: +${h.new} baru, ${h.updated} update`
    : `History: belum tersedia dari provider`;
  log(`Sync aktif: +${res.sync.new} baru, ${res.sync.updated} update | ${hText}`);
}

async function doBalance() {
  const res = await api("/api/balance");
  const msg = `Saldo saat ini: ${res.balance}`;
  log(msg);
  Swal.fire({
    icon: "info",
    title: "Cek Saldo",
    text: msg,
    confirmButtonText: "OK",
    confirmButtonColor: "#16a34a"
  });
}

async function doBuy() {
  const count = Number(el("buyCount").value || 1);
  const interval_sec = 3;
  const res = await api("/api/buy/start", {
    method: "POST",
    body: JSON.stringify({ count, interval_sec }),
  });
  log(`AUTO BUY jalan: ${res.buy_loop.count_per_cycle}/siklus, interval ${res.buy_loop.interval_sec}s`);
  renderBuyStatus(res.buy_loop);
}

async function stopBuy() {
  const res = await api("/api/buy/stop", { method: "POST", body: "{}" });
  log(res.message || "Stop request terkirim");
}

async function toggleBuy() {
  const res = await api("/api/buy/status");
  const running = !!(res.buy_loop && res.buy_loop.running);
  if (running) {
    await stopBuy();
  } else {
    await doBuy();
  }
  await loadBuyStatus();
}

async function loadBuyStatus() {
  const res = await api("/api/buy/status");
  renderBuyStatus(res.buy_loop || {});
}

async function doCancel() {
  const activation_id = (el("cancelId").value || "").trim();
  if (!activation_id) {
    log("Isi Activation ID dulu");
    return;
  }
  const res = await api("/api/cancel", { method: "POST", body: JSON.stringify({ activation_id }) });
  log(`Cancel ${res.activation_id}: ${res.response}`);
}

el("btnSync").addEventListener("click", async () => { try { await doSync(true); await loadTable(); await loadHistory(); await loadTerminal(); } catch (e) { log(e.message); } });
el("btnBalance").addEventListener("click", async () => { try { await doBalance(); await loadTerminal(); } catch (e) { log(e.message); } });
el("btnToggleBuy").addEventListener("click", async () => { try { await toggleBuy(); await loadTable(); await loadHistory(); await loadTerminal(); } catch (e) { log(e.message); } });
el("btnCancel").addEventListener("click", async () => { try { await doCancel(); await loadTable(); await loadHistory(); await loadTerminal(); } catch (e) { log(e.message); } });

(async function init() {
  try {
    await doSync(true);
    await loadTable();
    await loadHistory();
    await loadTerminal();
    await loadBuyStatus();
  } catch (e) {
    log(e.message);
  }

  let tick = 0;
  setInterval(async () => {
    try {
      tick += 1;
      await doSync(tick % 12 === 0);
      await loadTable();
      await loadHistory();
      await loadTerminal();
      await loadBuyStatus();
    } catch (e) {
      log(e.message);
    }
  }, 5000);
})();
</script>
</body>
</html>
"""

add_terminal_log("INFO", "Web dashboard started.")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
