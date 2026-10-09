"""KUDO Revamp Dashboard: Flask backend for Railway.

Stores every dashboard record (documents, action items, meetings, interfaces,
go-to-market items, status brief, team list) in Postgres, serves the single
page front end, proxies AI requests to the Anthropic API and keeps uploaded
interface previews.
"""
import base64
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import timedelta
from functools import wraps

import requests
from flask import (Flask, Response, abort, jsonify, redirect, request,
                   send_from_directory, session)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL_DEFAULT = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MODEL_COMPLEX = os.environ.get("ANTHROPIC_MODEL_COMPLEX", MODEL_DEFAULT)
COLLECTIONS = {"docs", "actions", "meetings", "interfaces", "gtm", "status", "settings"}
MAX_UPLOAD = 10 * 1024 * 1024

app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("RAILWAY_ENVIRONMENT") is not None,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=40 * 1024 * 1024,
)

# ---------------------------------------------------------------- database
IS_PG = DATABASE_URL.startswith("postgres")
if IS_PG:
    import psycopg

_local = threading.local()


def conn():
    c = getattr(_local, "c", None)
    if IS_PG:
        if c is None or c.closed:
            c = psycopg.connect(DATABASE_URL, autocommit=True)
            _local.c = c
    else:
        if c is None:
            c = sqlite3.connect(os.path.join(BASE_DIR, "local.db"), check_same_thread=False)
            c.isolation_level = None
            _local.c = c
    return c


def q(sql, params=(), fetch=False):
    if IS_PG:
        sql = sql.replace("?", "%s")
    try:
        cur = conn().cursor()
        cur.execute(sql, params)
    except Exception:
        # connection dropped: reconnect once
        _local.c = None
        cur = conn().cursor()
        cur.execute(sql, params)
    return cur.fetchall() if fetch else None


def init_db():
    blob = "BYTEA" if IS_PG else "BLOB"
    q("""CREATE TABLE IF NOT EXISTS records (
        collection TEXT NOT NULL, id TEXT NOT NULL, data TEXT,
        updated_at DOUBLE PRECISION NOT NULL, updated_by TEXT,
        deleted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (collection, id))""")
    q("CREATE INDEX IF NOT EXISTS records_updated ON records (updated_at)")
    q(f"""CREATE TABLE IF NOT EXISTS files (
        id TEXT PRIMARY KEY, content_type TEXT, data {blob},
        created_at DOUBLE PRECISION, created_by TEXT)""")


init_db()


def get_record(col, rid):
    rows = q("SELECT data, deleted FROM records WHERE collection=? AND id=?", (col, rid), fetch=True)
    if not rows or rows[0][1]:
        return None
    return json.loads(rows[0][0])


def put_record(col, rid, data, user):
    now = time.time()
    body = json.dumps(data, ensure_ascii=False)
    if IS_PG:
        q("""INSERT INTO records (collection,id,data,updated_at,updated_by,deleted) VALUES (?,?,?,?,?,0)
             ON CONFLICT (collection,id) DO UPDATE SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at,
             updated_by=EXCLUDED.updated_by, deleted=0""", (col, rid, body, now, user))
    else:
        q("INSERT OR REPLACE INTO records (collection,id,data,updated_at,updated_by,deleted) VALUES (?,?,?,?,?,0)",
          (col, rid, body, now, user))
    return now


def decorate(data, updated_at, updated_by):
    d = dict(data or {})
    d["_updatedAt"] = updated_at
    d["_updatedBy"] = updated_by or ""
    return d


def strip_meta(data):
    return {k: v for k, v in (data or {}).items() if not str(k).startswith("_")}


# -------------------------------------------------------------------- auth
def current_user():
    return session.get("name")


def login_required(api=False):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not current_user():
                if api:
                    return jsonify(error="Sign in required"), 401
                return redirect("/login")
            return fn(*a, **kw)
        return wrapper
    return deco


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Sign in: KUDO Revamp</title>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,760&family=Public+Sans:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--ink:#0E2F45;--signal:#F4C430}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0E2F45;font:15px/1.5 "Public Sans",system-ui,sans-serif;color:#16283A;padding:20px}
form{background:#fff;border-radius:16px;padding:32px;width:min(380px,100%)}
h1{font:760 1.6rem/1.1 "Bricolage Grotesque",system-ui,sans-serif;color:var(--ink);margin:0 0 6px}
p{margin:0 0 20px;color:#5A6B7B}label{display:block;font-weight:600;font-size:.86rem;color:#5A6B7B;margin:14px 0 4px}
input{width:100%;padding:10px 12px;border:1px solid #D3DCE3;border-radius:8px;font:inherit}
button{margin-top:22px;width:100%;padding:11px;border:0;border-radius:8px;background:var(--signal);font:600 1rem "Public Sans",sans-serif;cursor:pointer}
.err{background:#F8DEDC;color:#B83A34;padding:8px 10px;border-radius:8px;margin-bottom:10px;font-size:.9rem}
:focus-visible{outline:2.5px solid var(--signal);outline-offset:2px}
</style></head><body><form method="post" action="/login">
<h1>KUDO Revamp</h1><p>Internal program dashboard. Sign in with your name and the team password.</p>
__ERR__
<label for="n">Your name</label><input id="n" name="name" required autocomplete="name" value="__NAME__">
<label for="p">Team password</label><input id="p" name="password" type="password" required autocomplete="current-password">
<button type="submit">Sign in</button></form></body></html>"""


def login_page(err="", name=""):
    esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    html = LOGIN_HTML.replace("__ERR__", f'<div class="err">{esc(err)}</div>' if err else "")
    return html.replace("__NAME__", esc(name))


@app.get("/login")
def login_get():
    if not APP_PASSWORD:
        return login_page("The team password is not configured yet. Set APP_PASSWORD in Railway."), 503
    return login_page()


@app.post("/login")
def login_post():
    name = (request.form.get("name") or "").strip()[:60]
    pw = request.form.get("password") or ""
    if not APP_PASSWORD:
        return login_page("The team password is not configured yet. Set APP_PASSWORD in Railway."), 503
    if not name or not hmac.compare_digest(pw.encode(), APP_PASSWORD.encode()):
        time.sleep(1)
        return login_page("That password is not correct.", name), 401
    session.permanent = True
    session["name"] = name
    return redirect("/")


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ------------------------------------------------------------------- pages
@app.get("/healthz")
def health():
    return {"ok": True}


@app.get("/")
@login_required()
def index():
    resp = send_from_directory(os.path.join(BASE_DIR, "static"), "index.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/me")
@login_required(api=True)
def me():
    return jsonify(name=current_user(), ai=bool(ANTHROPIC_API_KEY))


# ---------------------------------------------------------------- records
@app.get("/api/state")
@login_required(api=True)
def state():
    now = time.time()
    since = request.args.get("since", type=float)
    if since is None:
        rows = q("SELECT collection,id,data,updated_at,updated_by,deleted FROM records WHERE deleted=0", fetch=True)
    else:
        # small overlap so a write committing during the previous poll is never missed
        rows = q("SELECT collection,id,data,updated_at,updated_by,deleted FROM records WHERE updated_at > ?",
                 (since - 2,), fetch=True)
    changes = []
    for col, rid, data, ua, ub, deleted in rows:
        changes.append({"collection": col, "id": rid, "deleted": bool(deleted),
                        "data": None if deleted else decorate(json.loads(data), ua, ub)})
    return jsonify(now=now, changes=changes)


def check_col(col):
    if col not in COLLECTIONS:
        abort(404)


@app.put("/api/c/<col>/<rid>")
@login_required(api=True)
def put(col, rid):
    check_col(col)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Body must be a JSON object"), 400
    data = strip_meta(data)
    existing = get_record(col, rid)
    if existing is None and "createdBy" not in data:
        data["createdBy"] = current_user()
    ua = put_record(col, rid, data, current_user())
    return jsonify(data=decorate(data, ua, current_user()))


@app.patch("/api/c/<col>/<rid>")
@login_required(api=True)
def patch(col, rid):
    check_col(col)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Body must be a JSON object"), 400
    existing = get_record(col, rid)
    if existing is None:
        return jsonify(error="This item no longer exists. Reload the page."), 404
    existing.update(strip_meta(data))
    ua = put_record(col, rid, existing, current_user())
    return jsonify(data=decorate(existing, ua, current_user()))


@app.delete("/api/c/<col>/<rid>")
@login_required(api=True)
def delete(col, rid):
    check_col(col)
    q("UPDATE records SET deleted=1, data=NULL, updated_at=?, updated_by=? WHERE collection=? AND id=?",
      (time.time(), current_user(), col, rid))
    return ("", 204)


# ------------------------------------------------------------------- files
@app.post("/api/files")
@login_required(api=True)
def upload():
    f = request.files.get("file")
    if not f:
        return jsonify(error="No file received"), 400
    ctype = f.mimetype or ""
    if not ctype.startswith("image/") or ctype == "image/svg+xml":
        return jsonify(error="Upload a PNG, JPG, GIF or WebP image"), 400
    raw = f.read()
    if len(raw) > MAX_UPLOAD:
        return jsonify(error="Images must be under 10 MB"), 413
    fid = uuid.uuid4().hex
    q("INSERT INTO files (id,content_type,data,created_at,created_by) VALUES (?,?,?,?,?)",
      (fid, ctype, raw, time.time(), current_user()))
    return jsonify(id=fid, url=f"/files/{fid}")


@app.get("/files/<fid>")
@login_required(api=True)
def get_file(fid):
    rows = q("SELECT content_type,data FROM files WHERE id=?", (fid,), fetch=True)
    if not rows:
        abort(404)
    resp = Response(bytes(rows[0][1]), mimetype=rows[0][0])
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


# ---------------------------------------------------------- import/export
@app.get("/api/export")
@login_required(api=True)
def export():
    out = {c: {} for c in COLLECTIONS}
    for col, rid, data in q("SELECT collection,id,data FROM records WHERE deleted=0", fetch=True):
        out.setdefault(col, {})[rid] = json.loads(data)
    out["files"] = {fid: {"contentType": ct, "b64": base64.b64encode(bytes(d)).decode()}
                    for fid, ct, d in q("SELECT id,content_type,data FROM files", fetch=True)}
    body = json.dumps(out, ensure_ascii=False)
    return Response(body, mimetype="application/json", headers={
        "Content-Disposition": f'attachment; filename="kudo-revamp-backup-{time.strftime("%Y-%m-%d")}.json"'})


@app.post("/api/import")
@login_required(api=True)
def import_data():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="The file is not a dashboard data file"), 400
    n = 0
    for fid, f in (payload.get("files") or {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fid):
            continue
        raw = base64.b64decode(f.get("b64", ""))
        q("DELETE FROM files WHERE id=?", (fid,))
        q("INSERT INTO files (id,content_type,data,created_at,created_by) VALUES (?,?,?,?,?)",
          (fid, f.get("contentType", "image/jpeg"), raw, time.time(), current_user()))
    for col in COLLECTIONS:
        for rid, data in (payload.get(col) or {}).items():
            if isinstance(data, dict):
                put_record(col, str(rid)[:200], strip_meta(data), current_user())
                n += 1
    return jsonify(imported=n)


# ---------------------------------------------------------------------- AI
def extract_json(text):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(t)
    except ValueError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            return json.loads(t[start:end + 1])
        raise


@app.post("/api/ai")
@login_required(api=True)
def ai():
    if not ANTHROPIC_API_KEY:
        return jsonify(error="AI is not set up. Add ANTHROPIC_API_KEY in Railway."), 503
    body = request.get_json(silent=True) or {}
    prompt = body.get("prompt") or ""
    if not prompt or len(prompt) > 400_000:
        return jsonify(error="The request is empty or too long"), 400
    model = MODEL_COMPLEX if body.get("tier") == "complex" else MODEL_DEFAULT
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 12000,
                  "system": "You return only one valid JSON object. No markdown fences, no commentary before or after.",
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=240)
    except requests.Timeout:
        app.logger.warning("Anthropic API timeout (prompt %s chars)", len(prompt))
        return jsonify(error="The AI service took too long to answer. Try again."), 504
    except requests.RequestException as exc:
        app.logger.warning("Anthropic API unreachable: %s", exc)
        return jsonify(error="Could not reach the AI service. Try again."), 502
    if r.status_code == 429:
        return jsonify(error="The AI service is busy. Wait a minute and try again."), 429
    if not r.ok:
        app.logger.warning("Anthropic API error %s: %s", r.status_code, r.text[:500])
        detail = ""
        try:
            detail = r.json().get("error", {}).get("message", "")
        except ValueError:
            pass
        return jsonify(error="The AI service returned an error" + (f": {detail}" if detail else ". Try again.")), 502
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if data.get("stop_reason") == "max_tokens":
        app.logger.warning("Anthropic answer cut off at max_tokens (prompt %s chars)", len(prompt))
        return jsonify(error="The AI answer was too long and got cut off. Try again with less content."), 502
    try:
        return jsonify(result=extract_json(text))
    except ValueError:
        app.logger.warning("Unreadable AI answer: %s", text[:500])
        return jsonify(error="The AI answer could not be read. Try again."), 502


@app.get("/static/<path:p>")
@login_required()
def static_files(p):
    return send_from_directory(os.path.join(BASE_DIR, "static"), p)


# ------------------------------------------------------- seed + migrations
def seed_missing():
    """Load seed/initial-data.json on startup, adding only records that have never existed.

    Items already in the database (including ones people edited or deleted) are left alone,
    so this is safe to run on every deploy.
    """
    path = os.path.join(BASE_DIR, "seed", "initial-data.json")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    existing = {(c, i) for c, i in q("SELECT collection,id FROM records", fetch=True)}
    have_files = {r[0] for r in q("SELECT id FROM files", fetch=True)}
    ignore = "ON CONFLICT (id) DO NOTHING" if IS_PG else ""
    verb = "INSERT" if IS_PG else "INSERT OR IGNORE"
    for fid, f in (payload.get("files") or {}).items():
        if fid not in have_files:
            q(f"{verb} INTO files (id,content_type,data,created_at,created_by) VALUES (?,?,?,?,?) {ignore}",
              (fid, f.get("contentType", "image/jpeg"), base64.b64decode(f.get("b64", "")), time.time(), "Initial data"))
    added = 0
    for col in COLLECTIONS:
        for rid, data in (payload.get(col) or {}).items():
            if (col, rid) not in existing and isinstance(data, dict):
                put_record(col, rid, data, "Initial data")
                added += 1
    if added:
        app.logger.warning("Seeded %s records from initial-data.json", added)
    run_migrations(payload)


# One-off data changes, each applied once and recorded in settings/migrations.
def _m_interface_sections(payload):
    for rid, old, new in (("ifc-producer", "Producer Console", "Producer Interface"),
                          ("ifc-operator", "Operator Console", "Operator Interface")):
        rec = get_record("interfaces", rid)
        if rec is not None and rec.get("category") == old:
            rec["category"] = new
            put_record("interfaces", rid, rec, "Update")


def _m_gtm_tracks(payload):
    """Split go-to-market points by release track and apply revised wording.

    Owners and statuses are kept. Wording is only replaced if nobody edited it.
    """
    revisions = payload.get("gtm_revisions") or {}
    for rid, seed in (payload.get("gtm") or {}).items():
        rec = get_record("gtm", rid)
        if rec is None:
            continue
        rec["track"] = rec.get("track") or seed.get("track")
        rec["order"] = seed.get("order", rec.get("order"))
        before = revisions.get(rid)
        if before and rec.get("question") == before["question"] and rec.get("guidance") == before["guidance"]:
            for k in ("question", "guidance", "phase", "dept"):
                rec[k] = seed[k]
        put_record("gtm", rid, rec, "Update")


def _m_ms_ls_split(payload):
    """Split the 'LS & MS Dashboard' section into Meeting Services and Language Services.

    Interfaces about operators go to Meeting Services, about interpreters to Language Services;
    everything else under the old name moves to the shared 'MS & LS Dashboard' group card.
    """
    old, group = "LS & MS Dashboard", "MS & LS Dashboard"
    rows = q("SELECT collection,id,data FROM records WHERE deleted=0 AND collection IN ('docs','actions','meetings','interfaces')", fetch=True)
    for col, rid, data in rows:
        rec = json.loads(data)
        if rec.get("category") != old:
            continue
        name = (rec.get("name") or rec.get("title") or "").lower() if col == "interfaces" else ""
        if "operator" in name:
            rec["category"] = "Meeting Services"
        elif "interpreter" in name:
            rec["category"] = "Language Services"
        else:
            rec["category"] = group
        put_record(col, rid, rec, "Update")
    st = get_record("settings", "structure") or {}
    secs = st.get("sections") or {}
    backend = [x for x in (secs.get("backend") or []) if x not in (old, group, "Meeting Services", "Language Services")]
    secs["backend"] = [group, "Meeting Services", "Language Services"] + backend
    secs.setdefault("interfaces", ["Interpreter Interface", "Operator Interface", "Participant Interface", "Viewer Interface", "Producer Interface"])
    st["sections"] = secs
    st["groups"] = {"backend": [{"name": group, "sections": ["Meeting Services", "Language Services"]}]}
    put_record("settings", "structure", st, "Update")


def _m_ops_groups(payload):
    """Ops backend = master project with a Meeting Services group and a Language Services group.

    Each backend interface becomes its own card (its category is its name) inside a group:
    operator interfaces go to Meeting Services, interpreter interfaces to Language Services,
    the rest (the shared Home dashboard) to 'Shared by MS & LS'.
    """
    legacy = {"LS & MS Dashboard", "MS & LS Dashboard"}
    ms, ls, shared = [], [], []
    home = None
    rows = q("SELECT id,data FROM records WHERE deleted=0 AND collection='interfaces'", fetch=True)
    for rid, data in rows:
        rec = json.loads(data)
        if rec.get("workstream") != "backend":
            continue
        cat, name = rec.get("category") or "", rec.get("name") or rid
        low = name.lower()
        if cat == "Meeting Services" or "operator" in low:
            ms.append(name)
        elif cat == "Language Services" or "interpreter" in low:
            ls.append(name)
        elif cat in legacy or not cat:
            shared.append(name)
            home = home or name
        else:
            continue
        if cat != name:
            rec["category"] = name
            put_record("interfaces", rid, rec, "Update")
    # documents, actions and meetings filed under the old dashboard name follow the Home dashboard
    target = home or "LS & MS Dashboard: Home"
    if not home:
        shared.append(target)
    rows = q("SELECT collection,id,data FROM records WHERE deleted=0 AND collection IN ('docs','actions','meetings')", fetch=True)
    for col, rid, data in rows:
        rec = json.loads(data)
        if rec.get("category") in legacy:
            rec["category"] = target
            put_record(col, rid, rec, "Update")
    st = get_record("settings", "structure") or {}
    secs = st.get("sections") or {}
    drop = legacy | {"Meeting Services", "Language Services"} | set(ms) | set(ls) | set(shared)
    secs["backend"] = [x for x in (secs.get("backend") or []) if x not in drop]
    secs.setdefault("interfaces", ["Interpreter Interface", "Operator Interface", "Participant Interface", "Viewer Interface", "Producer Interface"])
    st["sections"] = secs
    st["groups"] = {"backend": [
        {"name": "Meeting Services", "sections": ms},
        {"name": "Language Services", "sections": ls},
        {"name": "Shared by MS & LS", "sections": shared},
    ]}
    put_record("settings", "structure", st, "Update")


MIGRATIONS = [
    ("2026-09-interface-sections", _m_interface_sections),
    ("2026-09-gtm-tracks", _m_gtm_tracks),
    ("2026-10-ms-ls-split", _m_ms_ls_split),
    ("2026-10-ops-groups", _m_ops_groups),
]


def run_migrations(payload):
    log = get_record("settings", "migrations") or {"done": []}
    changed = False
    for name, fn in MIGRATIONS:
        if name in log["done"]:
            continue
        fn(payload)
        log["done"].append(name)
        changed = True
    if changed:
        put_record("settings", "migrations", log, "Update")


try:
    seed_missing()
except Exception as exc:  # never block startup on seeding
    app.logger.warning("Seeding skipped: %s", exc)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
