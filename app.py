import os, sqlite3, secrets, csv, io, hmac, hashlib, time, math, json, sys, re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from functools import wraps
from flask import Flask, request, session, redirect, render_template_string, jsonify, Response, abort
from werkzeug.middleware.proxy_fix import ProxyFix
from markupsafe import escape as E

app = Flask(__name__, static_folder='static')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE', '1') == '1',
    MAX_CONTENT_LENGTH=12000,
)

DB = os.getenv('DATABASE_PATH', 'entries.sqlite3')
DATABASE_URL = (os.getenv('DATABASE_URL') or '').strip()
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
ADMIN_PASSWORD_HASH = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest() if ADMIN_PASSWORD else ''
CLOSING = os.getenv('CLOSING_DATE', '2026-12-21T23:59:59+09:30')
PUBLIC_URL = os.getenv('PUBLIC_URL', '')
LOGIN_MAX_FAILS = int(os.getenv('LOGIN_MAX_FAILS', '5'))
LOGIN_LOCK_SECONDS = int(os.getenv('LOGIN_LOCK_SECONDS', '900'))
def load_hotspots():
    raw = (os.getenv('HOTSPOTS_JSON') or '').strip()
    if not raw:
        msg = 'HOTSPOTS_JSON is not set. Set it to a JSON list of [x, y] points (one point per difference) before starting the app.'
        print(msg, file=sys.stderr)
        raise SystemExit(msg)
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError('value must be a JSON list')
        spots = []
        for item in data:
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                raise ValueError('each item must be a pair of numbers')
            x, y = float(item[0]), float(item[1])
            spots.append((x, y))
        if not 1 <= len(spots) <= 30:
            raise ValueError('expected between 1 and 30 points, got %s' % len(spots))
        return spots
    except SystemExit:
        raise
    except Exception as e:
        msg = 'HOTSPOTS_JSON is invalid: %s' % e
        print(msg, file=sys.stderr)
        raise SystemExit(msg)

HOTSPOTS = load_hotspots()
NSPOTS = len(HOTSPOTS)
_login_attempts = {}

def _puzzle_size():
    from PIL import Image
    path = os.path.join(app.static_folder or 'static', 'Christmas_Spot_10_A.png')
    with Image.open(path) as im:
        return im.size
IMAGE_WIDTH, IMAGE_HEIGHT = _puzzle_size()
HIT_RADIUS = max(24, int(round(min(IMAGE_WIDTH, IMAGE_HEIGHT) * 0.09)))
# Progress in the session is tied to this exact puzzle (answers + picture size), so a
# changed puzzle never inherits stale "already found" answers from an older session.
PUZZLE_VERSION = hashlib.sha256(json.dumps([HOTSPOTS, IMAGE_WIDTH, IMAGE_HEIGHT]).encode()).hexdigest()[:16]

def puzzle_progress():
    if session.get('puzzle') != PUZZLE_VERSION:
        reset_progress()
    return set(i for i in (session.get('found_spots') or []) if isinstance(i, int) and 0 <= i < NSPOTS)

def reset_progress():
    session['puzzle'] = PUZZLE_VERSION
    session['found_spots'] = []
    session.pop('solved', None)

# When DATABASE_URL is set, entries live in Postgres (e.g. Neon). Otherwise
# the existing SQLite file at DATABASE_PATH is used unchanged.
if DATABASE_URL:
    import psycopg
    _DB_INTEGRITY = (sqlite3.IntegrityError, psycopg.IntegrityError)
else:
    _DB_INTEGRITY = sqlite3.IntegrityError

def _postgres_conninfo(url):
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q.setdefault('connect_timeout', '10')
    if 'sslmode' not in q:
        host = (parts.hostname or '').lower()
        socket = q.get('host') or ''
        local = host in ('', 'localhost', '127.0.0.1', '::1') or socket.startswith('/')
        q['sslmode'] = 'disable' if local else 'require'
    return urlunparse(parts._replace(query=urlencode(q)))

class _Row(dict):
    """Dict row that also supports numeric indexes like sqlite3.Row."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

def _pg_row_factory(cursor):
    names = [d.name for d in cursor.description] if cursor.description else []
    def make(values):
        return _Row(zip(names, values))
    return make

class _PgConn:
    """Per-request psycopg connection. Neon closes idle connections and scales to zero."""
    def __init__(self, raw):
        self._raw = raw

    @property
    def isolation_level(self):
        return None if self._raw.autocommit else ''

    @isolation_level.setter
    def isolation_level(self, value):
        self._raw.autocommit = value is None

    def execute(self, sql, params=None):
        stripped = sql.strip().upper()
        if stripped == 'BEGIN IMMEDIATE':
            sql = 'BEGIN'
        else:
            sql = sql.replace('?', '%s')
        if params is None:
            return self._raw.execute(sql)
        return self._raw.execute(sql, params)

    def close(self):
        if not self._raw.closed:
            self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if not self._raw.closed and not self._raw.autocommit:
                if exc_type is None:
                    self._raw.commit()
                else:
                    self._raw.rollback()
        finally:
            self.close()
        return False

def _pg_connect():
    conninfo = _postgres_conninfo(DATABASE_URL)
    last = None
    for attempt in range(3):
        try:
            return psycopg.connect(conninfo, row_factory=_pg_row_factory, prepare_threshold=None)
        except Exception as err:
            last = err
            if attempt < 2:
                time.sleep(2)
    raise last

def conn():
    if DATABASE_URL:
        return _PgConn(_pg_connect())
    c = sqlite3.connect(DB, timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=5000')
    return c

def init():
    with conn() as c:
        if DATABASE_URL:
            c.execute('CREATE TABLE IF NOT EXISTS entries(id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, created TEXT NOT NULL, entrant TEXT NOT NULL, guardian TEXT, contact TEXT NOT NULL, phone TEXT NOT NULL DEFAULT \'\', memory TEXT NOT NULL, consent INTEGER NOT NULL, marketing INTEGER NOT NULL DEFAULT 0, contact_hash TEXT UNIQUE NOT NULL, source TEXT NOT NULL DEFAULT \'online\')')
            c.execute('CREATE TABLE IF NOT EXISTS draws(place INTEGER PRIMARY KEY, entry_id INTEGER UNIQUE NOT NULL, drawn_at TEXT NOT NULL)')
        else:
            c.execute('CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY, created TEXT NOT NULL, entrant TEXT NOT NULL, guardian TEXT, contact TEXT NOT NULL, phone TEXT NOT NULL DEFAULT \'\', memory TEXT NOT NULL, consent INTEGER NOT NULL, marketing INTEGER NOT NULL DEFAULT 0, contact_hash TEXT UNIQUE NOT NULL, source TEXT NOT NULL DEFAULT \'online\')')
            c.execute('CREATE TABLE IF NOT EXISTS draws(place INTEGER PRIMARY KEY, entry_id INTEGER UNIQUE NOT NULL, drawn_at TEXT NOT NULL)')
        # Add missing columns only. A failed ALTER on Postgres aborts the
        # transaction, so do not run ALTER when the column already exists.
        if DATABASE_URL:
            cols = {str(r[0]).lower() for r in c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'entries'"
            ).fetchall()}
        else:
            cols = {str(r[1]).lower() for r in c.execute('PRAGMA table_info(entries)').fetchall()}
        if 'source' not in cols:
            c.execute("ALTER TABLE entries ADD COLUMN source TEXT NOT NULL DEFAULT 'online'")
        if 'phone' not in cols:
            try:
                c.execute("ALTER TABLE entries ADD COLUMN IF NOT EXISTS phone TEXT NOT NULL DEFAULT ''")
            except Exception as err:
                msg = str(err).lower()
                if DATABASE_URL:
                    try:
                        c._raw.rollback()
                    except Exception:
                        pass
                if 'duplicate column' in msg or 'already exists' in msg:
                    pass
                else:
                    # SQLite may not accept IF NOT EXISTS on ADD COLUMN
                    c.execute("ALTER TABLE entries ADD COLUMN phone TEXT NOT NULL DEFAULT ''")
init()

def csrf():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']

def checkcsrf():
    token = request.headers.get('X-CSRF-Token') or request.form.get('csrf') or ''
    if not token and request.is_json:
        token = (request.get_json(silent=True) or {}).get('csrf') or ''
    expected = session.get('csrf') or ''
    if not expected or not token or not hmac.compare_digest(str(token), str(expected)):
        abort(403)

def is_open():
    try:
        return datetime.now().astimezone() < datetime.fromisoformat(CLOSING)
    except Exception:
        return False

def client_ip():
    return request.remote_addr or '0.0.0.0'

def login_locked():
    rec = _login_attempts.get(client_ip())
    return bool(rec and rec.get('until', 0) > time.time())

def record_login_fail():
    ip = client_ip()
    rec = _login_attempts.get(ip, {'n': 0, 'until': 0})
    if rec.get('until', 0) > time.time():
        return
    rec['n'] = rec.get('n', 0) + 1
    if rec['n'] >= LOGIN_MAX_FAILS:
        rec['until'] = time.time() + LOGIN_LOCK_SECONDS
        rec['n'] = 0
    _login_attempts[ip] = rec
    # Prevent unbounded growth if many IPs are used
    if len(_login_attempts) > 5000:
        now = time.time()
        stale = [k for k, v in _login_attempts.items() if v.get('until', 0) < now and v.get('n', 0) == 0]
        for k in stale[:2500]:
            _login_attempts.pop(k, None)

def clear_login_fails():
    _login_attempts.pop(client_ip(), None)

BASE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#c8102e"><title>{{ title }} | LJ Hooker Alice Springs Christmas</title><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Fredoka:wght@500;600;700&family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet"><style>
:root{--red:#d62839;--red2:#b81d2f;--green:#1e8a4c;--green2:#e3f5e9;--gold:#f6b93b;--gold2:#fff4d6;--ink:#2b2d42;--muted:#5f6475;--sky:#eaf6ff;--line:#e3ecf5;font-family:Nunito,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:var(--ink)}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;position:relative;background:#fffdf8 linear-gradient(180deg,#eaf6ff 0,#f7fbff 420px,#fffdf8 900px) no-repeat}html{background:#fffdf8}
body:before{content:"";position:absolute;left:0;right:0;top:0;height:100%;min-height:100vh;pointer-events:none;z-index:0;opacity:.55;background-image:radial-gradient(#fff 2px,transparent 2.5px),radial-gradient(#cfe6fb 1.5px,transparent 2px);background-size:60px 60px,34px 34px;background-position:0 0,17px 23px}
header.site,main,footer.site{position:relative;z-index:1}
h1,h2,h3,.fun{font-family:Fredoka,Nunito,system-ui,sans-serif}
a{color:var(--red2)}
header.site{background:#fff;box-shadow:0 2px 14px rgba(43,45,66,.08)}header.site .in{max-width:1080px;margin:auto;padding:12px 20px;display:flex;align-items:center}
header.site a.brand{display:flex;align-items:center;gap:14px;text-decoration:none;color:var(--ink)}header.site img{height:48px;width:auto;display:block}
header.site .office{font-weight:800;font-size:18px;color:var(--ink);border-left:2px solid #e2e2e2;padding-left:14px;line-height:1.1}
.wordmark{display:inline-block;background:#ffe600;color:#000;font-weight:800;font-size:22px;padding:8px 14px;border-radius:4px;font-family:Arial,Helvetica,sans-serif;letter-spacing:-.01em}
main{max-width:1080px;margin:auto;padding:26px 18px 60px}
.hero{position:relative;overflow:hidden;background:linear-gradient(160deg,#ffffff 0%,#fff6f6 55%,#ffeef0 100%);border:3px solid #fff;box-shadow:0 10px 30px rgba(214,40,57,.14);border-radius:30px;padding:46px 28px 30px;text-align:center}
.hero:before,.hero:after{position:absolute;font-size:44px;opacity:.9}.hero:before{content:"❄️";left:22px;top:18px;transform:rotate(-12deg)}.hero:after{content:"⭐";right:24px;top:20px;transform:rotate(12deg)}
.hero .deco{font-size:28px;letter-spacing:8px;margin-top:4px;white-space:nowrap;overflow:hidden}
.eyebrow{display:inline-block;background:var(--green2);color:var(--green);border-radius:999px;padding:7px 16px;font-weight:800;font-size:14px}
.hero h1{font-size:clamp(36px,7vw,66px);line-height:1.02;margin:16px 0 10px;font-weight:700;color:var(--red)}.hero h1 em{font-style:normal;color:var(--green)}
.hero p{font-size:18px;line-height:1.55;max-width:640px;margin:10px auto 22px;color:var(--ink)}
.pill{display:inline-block;border-radius:999px;padding:12px 22px;background:var(--gold);color:#5a3a00;font-weight:800;font-size:16px;box-shadow:0 4px 0 #d99a1e;font-family:Fredoka,Nunito,sans-serif;letter-spacing:.02em}
.hero .pill{margin-bottom:22px}
.panel{background:#fff;border-radius:26px;padding:26px;margin-top:22px;box-shadow:0 8px 26px rgba(43,45,66,.08);border:2px solid #fff}
h2{margin:0 0 14px;font-size:27px;font-weight:600;color:var(--ink)}h2 .num{display:inline-flex;align-items:center;justify-content:center;width:40px;height:40px;border-radius:50%;background:var(--red);color:#fff;font-size:22px;margin-right:10px;vertical-align:3px;box-shadow:0 3px 0 var(--red2)}
h3{margin:0 0 8px;font-size:19px;font-weight:600;color:var(--green)}.muted{color:var(--muted)}p{line-height:1.6}
.prizes{display:grid;grid-template-columns:1fr 1fr;gap:12px}.prize{border-radius:20px;padding:16px;background:var(--gold2);font-size:15px;text-align:center}.prize:nth-child(2n){background:var(--green2)}.prize:nth-child(3n){background:#ffecee}.prize strong{display:block;font-family:Fredoka,Nunito,sans-serif;font-size:22px;color:var(--red);margin:6px 0 2px;font-weight:700}.prize ul{list-style:none;margin:10px 0 0;padding:0;text-align:left;font-size:14px;line-height:1.45;color:var(--ink)}.prize li{margin:0 0 4px;padding-left:1.1em;position:relative}.prize li:before{content:"•";position:absolute;left:0;color:var(--red)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.puzzle{position:relative;overflow:hidden;border:4px solid #fff;border-radius:22px;cursor:pointer;touch-action:manipulation;background:#fff;box-shadow:0 0 0 3px var(--green2),0 6px 18px rgba(43,45,66,.08)}.puzzle img{width:100%;height:auto;display:block;-webkit-user-drag:none;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none;pointer-events:auto}.puzzle{-webkit-tap-highlight-color:transparent;user-select:none;-webkit-user-select:none}
.mark{position:absolute;border:5px solid var(--green);background:rgba(30,138,76,.16);box-shadow:0 0 0 3px #fff;border-radius:50%;width:7%;aspect-ratio:1;transform:translate(-50%,-50%);pointer-events:none;animation:pop .45s cubic-bezier(.3,1.6,.5,1)}@keyframes pop{0%{transform:translate(-50%,-50%) scale(.2);opacity:0}100%{transform:translate(-50%,-50%) scale(1);opacity:1}}
.miss{position:absolute;transform:translate(-50%,-50%);pointer-events:none;font-size:22px;animation:fade .7s ease-out forwards}@keyframes fade{to{opacity:0;transform:translate(-50%,-90%)}}
.sparkle{position:absolute;pointer-events:none;font-size:20px;transform:translate(-50%,-50%);animation:fly .8s ease-out forwards}@keyframes fly{to{opacity:0;transform:translate(calc(-50% + var(--dx)),calc(-50% + var(--dy))) scale(.6) rotate(160deg)}}
.counter{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:6px 0 12px;flex-wrap:wrap}.counter b{font-family:Fredoka,Nunito,sans-serif;font-size:24px;color:var(--red)}.counter .chip{background:var(--gold2);color:#7a5200;border-radius:999px;padding:8px 14px;font-weight:800;font-size:15px}
.stars{font-size:24px;letter-spacing:3px;line-height:1.3;word-break:break-all}.stars span{filter:grayscale(1);opacity:.28;display:inline-block;transition:all .3s}.stars span.on{filter:none;opacity:1;transform:scale(1.15)}
.progress{background:#eef2f7;border-radius:999px;height:18px;overflow:hidden;border:2px solid #fff;box-shadow:inset 0 1px 3px rgba(0,0,0,.08)}.bar{height:100%;width:0%;transition:width .35s;background:repeating-linear-gradient(-45deg,var(--red) 0 12px,#fff 12px 18px,var(--green) 18px 30px,#fff 30px 36px)}
.toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,40px);opacity:0;background:#fff;color:var(--green);font-family:Fredoka,Nunito,sans-serif;font-weight:600;font-size:22px;padding:14px 26px;border-radius:999px;box-shadow:0 10px 30px rgba(43,45,66,.2);transition:all .3s;z-index:20;pointer-events:none;white-space:nowrap}.toast.show{opacity:1;transform:translate(-50%,0)}
.confetti{position:fixed;top:-30px;z-index:30;pointer-events:none;font-size:26px;animation:fall linear forwards}@keyframes fall{to{transform:translateY(110vh) rotate(540deg)}}
.celebrate{display:none;background:linear-gradient(135deg,var(--green2),#fff6d9);border-radius:20px;padding:18px;margin-top:16px;text-align:center;font-family:Fredoka,Nunito,sans-serif;font-size:24px;font-weight:600;color:var(--green)}.celebrate.show{display:block;animation:pop2 .5s cubic-bezier(.3,1.6,.5,1)}@keyframes pop2{from{transform:scale(.7);opacity:0}}
label{font-weight:800;display:block;margin:16px 0 7px;font-size:15px}input,textarea{width:100%;border:2px solid #dbe4ee;border-radius:16px;padding:14px;font:inherit;background:#fbfdff}input:focus,textarea:focus{outline:none;border-color:var(--green);box-shadow:0 0 0 4px var(--green2)}textarea{min-height:110px;resize:vertical}
.check{display:flex;gap:10px;align-items:start;margin-top:15px}.check input{width:22px;height:22px;margin-top:2px;accent-color:var(--green);flex:none}.check label{margin:0;font-weight:600}
button,.btn{display:inline-block;background:var(--red);border:0;border-radius:999px;padding:16px 30px;color:#fff;font-family:Fredoka,Nunito,sans-serif;font-size:19px;font-weight:600;cursor:pointer;text-decoration:none;box-shadow:0 5px 0 var(--red2);transition:transform .08s,box-shadow .08s,filter .15s}button:hover:not(:disabled),.btn:hover{filter:brightness(1.06)}button:active:not(:disabled),.btn:active{transform:translateY(4px);box-shadow:0 1px 0 var(--red2)}
button:disabled{background:#d9dee5;color:#7d8594;box-shadow:0 5px 0 #c3c9d2;cursor:not-allowed}.secondary{background:var(--green);box-shadow:0 5px 0 #166b3a}.secondary:active{box-shadow:0 1px 0 #166b3a}
.error{background:#ffecee;color:#8c1723;padding:14px 16px;border-radius:16px;margin:15px 0;font-weight:700}.success{background:var(--green2);color:#12603b;padding:14px;border-radius:16px}.small{font-size:14px}
footer.site{text-align:center;padding:26px 18px 34px;color:var(--muted);font-size:14px}footer.site b{color:var(--ink)}footer.site a{color:var(--red2);font-weight:700}
.stats{display:flex;gap:14px;flex-wrap:wrap}.stats div{padding:16px 22px;border-radius:18px;background:var(--green2)}.stats div:nth-child(2){background:var(--gold2)}.stats b{font-family:Fredoka,Nunito,sans-serif;font-size:26px;margin-right:4px;color:var(--ink)}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:10px;border-bottom:1px solid #eef1f5;font-size:14px}th{background:#f6f9fc;font-size:13px}.overflow{overflow:auto;border-radius:14px}
.login{max-width:460px;margin:30px auto}
@media(max-width:760px){.prizes{grid-template-columns:1fr 1fr}.grid{grid-template-columns:1fr}.hero{padding:44px 14px 22px;border-radius:24px}.hero:before,.hero:after{font-size:24px;top:10px}.hero:before{left:12px}.hero:after{right:12px}.hero .deco{font-size:20px;letter-spacing:4px}.eyebrow{font-size:12px;padding:6px 12px}.panel{padding:18px;border-radius:22px}header.site img{height:38px}header.site .office{font-size:16px}main{padding:16px 12px 40px}h2{font-size:23px}.toast{font-size:18px}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body><header class="site"><div class="in"><a class="brand" href="/"><img src="/static/ljh_logo.png" alt="LJ Hooker"><span class="office">Alice Springs</span></a></div></header><main>{{ body|safe }}</main><footer class="site"><b>LJ Hooker Alice Springs</b><br>Suite 2/52 Hartley Street, Alice Springs NT 0870 &middot; <a href="/terms">Terms</a></footer></body></html>'''

_LOGO = os.path.join(app.static_folder or 'static', 'ljh_logo.png')
LOGO_HTML = ('<img src="/static/ljh_logo.png" alt="LJ Hooker">' if os.path.exists(_LOGO)
             else '<span class="wordmark">LJ Hooker</span>')
BASE = BASE.replace('<img src="/static/ljh_logo.png" alt="LJ Hooker">', LOGO_HTML)

def page(body, title='Spot the Magic'):
    return render_template_string(BASE, title=title, body=body)

@app.get('/')
def home():
    token = csrf()
    found_now = sorted(puzzle_progress())
    found_js = json.dumps([[HOTSPOTS[i][0] / IMAGE_WIDTH, HOTSPOTS[i][1] / IMAGE_HEIGHT] for i in found_now])
    closed = not is_open()
    body = '''<section class="hero"><div class="eyebrow">🎅 A Christmas activity for kids &amp; families</div><h1>Spot the Magic<br>of <em>Christmas!</em> 🎄</h1><p>Find all NSPOTS differences, then share your favourite Christmas memory in 25 words or less to <b>enter the draw</b> for your chance to win!</p><span class="pill">🎁 PRIZES TO BE WON IN THE DRAW 🎁</span><div class="deco" aria-hidden="true">🎄 ⭐ 🎁 ❄️ 🦌 ❄️ 🎁 ⭐ 🎄</div></section>
 <section class="panel"><h2>🎁 Prizes to be won in the draw</h2><div class="prizes"><div class="prize">🥇 1st prize<strong>Ultimate Christmas Package</strong></div><div class="prize">🥈 2nd prize<strong>Family Fun Package</strong></div><div class="prize">🥉 3rd prize<strong>Christmas Treat Package</strong></div><div class="prize">📚 Lucky draw<strong>A$50 Red Kangaroo Books gift voucher</strong></div></div><p><b>How to enter the draw:</b> 1. Find all NSPOTS differences. 2. Share your favourite Christmas memory and your details. That’s it, you’re in the draw! Winners will be drawn at random after the competition closes.</p><p class="muted"><a href="/terms">Full terms</a></p></section>
 <section class="panel"><h2><span class="num">1</span>Find the NSPOTS differences</h2><p class="muted">Tap the differences in either picture. A green circle pops up each time you find one! Zoom in on your phone if needed.</p><div class="counter"><b id="count">0 / NSPOTS found</b><span class="chip" id="chip">🔍 Keep looking!</span></div><div class="stars" id="stars" aria-hidden="true">STARS</div><div class="progress"><div class="bar" id="bar"></div></div><div class="grid" style="margin-top:16px"><div><h3>Picture A</h3><div class="puzzle" data-puzzle="a"><img alt="Christmas scene A" src="/static/Christmas_Spot_10_A.png" draggable="false"></div></div><div><h3>Picture B</h3><div class="puzzle" data-puzzle="b"><img alt="Christmas scene B" src="/static/Christmas_Spot_10_B.png" draggable="false"></div></div></div><p id="puzzlemsg" class="muted small" aria-live="polite">Find all NSPOTS to unlock your entry into the draw.</p><p><button type="button" class="secondary" id="restart">🔄 Start again</button></p><div class="celebrate" id="celebrate">🎉 Hooray! You found all NSPOTS differences! Now fill in step 2 to enter the draw. 🎉</div></section><div class="toast" id="toast" role="status"></div>
 <section class="panel" id="entry"><h2><span class="num">2</span>Enter the draw: your favourite Christmas memory</h2><p>Tell us your favourite Christmas memory in <b>25 words or less</b> to enter the draw for your chance to win.</p><form method="post" action="/enter" id="entryform"><input type="hidden" name="csrf" value="TOKEN"><div style="display:none"><label>Leave this field empty<input name="website" autocomplete="off"></label></div><label for="memory">My Christmas memory</label><textarea name="memory" id="memory" maxlength="300" required placeholder="One Christmas I remember..."></textarea><div class="muted small" id="words">0 / 25 words</div><div class="grid"><div><label for="entrant">Entrant's full name</label><input id="entrant" name="entrant" maxlength="100" required></div><div><label for="guardian">Parent/guardian name (if entrant is under 18)</label><input id="guardian" name="guardian" maxlength="100"></div></div><label for="contact">Parent/guardian or adult entrant's email address</label><input type="email" name="contact" id="contact" maxlength="150" required><label for="phone">Parent/guardian contact number</label><input type="tel" name="phone" id="phone" maxlength="24" required autocomplete="tel" inputmode="tel" placeholder="04xx xxx xxx or +61"><div class="check"><input type="checkbox" name="terms" value="yes" id="terms" required><label for="terms">I am 18 or older and am entering for myself or with the permission of the child’s parent/guardian. I accept the <a href="/terms" target="_blank">competition terms and privacy notice</a>.</label></div><div class="check"><input type="checkbox" name="marketing" value="yes" id="marketing"><label for="marketing">Optional: I would like to receive LJ Hooker Alice Springs marketing emails. This is not required to enter.</label></div><p><button id="submit" disabled>Complete the puzzle to enter</button></p><p class="muted small">One entry per contact email. No purchase necessary. The draw is random; finding the differences and completing the memory are eligibility steps, not judging criteria.</p></form></section>'''.replace('TOKEN', token).replace('STARS', '<span>⭐</span>' * NSPOTS).replace('NSPOTS', str(NSPOTS))
    if closed:
        body = '<section class="hero"><h1>Entries are closed ❄️</h1><p>Thank you for joining our Christmas community giveaway.</p></section>'
    script = '''<script>
const csrfToken="TOKEN",imgW=IMG_W,imgH=IMG_H,total=NSPOTS;
const cheers=['Great spotting!','Well done!','You found one!','Super sleuth!','Ho ho ho!','Brilliant!','Amazing eyes!','Fantastic!'];
function toast(t){const el=document.querySelector('#toast');el.textContent=t;el.classList.add('show');clearTimeout(toast.t);toast.t=setTimeout(()=>el.classList.remove('show'),1400)}
function sparkle(p,lx,ly){for(let i=0;i<8;i++){const s=document.createElement('span');s.className='sparkle';s.textContent=['✨','⭐','❄️'][i%3];const a=i/8*6.283;s.style.left=lx+'%';s.style.top=ly+'%';s.style.setProperty('--dx',Math.cos(a)*60+'px');s.style.setProperty('--dy',Math.sin(a)*60+'px');p.append(s);setTimeout(()=>s.remove(),900)}}
function confetti(){const items=['🎉','⭐','❄️','🎄','🎁','✨','🔔'];for(let i=0;i<60;i++){const c=document.createElement('span');c.className='confetti';c.textContent=items[i%items.length];c.style.left=Math.random()*100+'vw';c.style.animationDuration=(2.2+Math.random()*2.2)+'s';c.style.animationDelay=Math.random()*.8+'s';document.body.append(c);setTimeout(()=>c.remove(),5500)}}
function showProgress(found,solved,celebrate){document.querySelector('#count').textContent=found+' / '+total+' found';document.querySelector('#bar').style.width=(found/total*100)+'%';const st=document.querySelectorAll('#stars span');for(let i=0;i<st.length;i++)st[i].classList.toggle('on',i<found);const left=total-found;document.querySelector('#chip').textContent=solved?'🌟 All found!':(left===1?'⭐ Just 1 to go!':'🔍 '+left+' to go!');const sub=document.querySelector('#submit');if(solved){document.querySelector('#puzzlemsg').textContent='Wonderful! You found all '+total+'. Complete your entry below to go into the draw.';document.querySelector('#celebrate').classList.add('show');sub.disabled=false;sub.textContent='🎁 Enter the Christmas draw';if(celebrate){confetti();toast('🎉 You did it!')}}else{document.querySelector('#puzzlemsg').textContent='Find all '+total+' to unlock your entry into the draw.';document.querySelector('#celebrate').classList.remove('show');sub.disabled=true;sub.textContent='Complete the puzzle to enter'}}
function addMark(fx,fy,burst){for(const q of document.querySelectorAll('.puzzle')){const m=document.createElement('span');m.className='mark';m.style.left=fx*100+'%';m.style.top=fy*100+'%';q.append(m);if(burst)sparkle(q,fx*100,fy*100)}}
let lastMiss=0;
async function tapAt(p,clientX,clientY){const img=p.querySelector('img'),r=img.getBoundingClientRect();if(!r.width||!r.height)return;const fx=(clientX-r.left)/r.width,fy=(clientY-r.top)/r.height;if(fx<0||fx>1||fy<0||fy>1)return;const slow=setTimeout(()=>toast('⏳ Waking up the elves… one moment'),1500);let res,data;try{res=await fetch('/spot',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({fx,fy})})}catch(err){clearTimeout(slow);toast('📶 Couldn’t reach the site. Please try again.');return}clearTimeout(slow);if(res.status===403){toast('🔄 Refreshing the page…');setTimeout(()=>location.reload(),900);return}if(!res.ok){toast('😕 Something went wrong. Please try again.');return}try{data=await res.json()}catch(err){toast('😕 Something went wrong. Please try again.');return}
if(!data.hit){const m=document.createElement('span');m.className='miss';m.textContent='❄️';m.style.left=fx*100+'%';m.style.top=fy*100+'%';p.append(m);setTimeout(()=>m.remove(),800);if(Date.now()-lastMiss>2500){lastMiss=Date.now();toast(data.found>=total?'🌟 You found them all already!':'👀 Not there, keep looking!')}return}
addMark(data.fx,data.fy,true);showProgress(data.found,data.solved,data.solved);if(!data.solved)toast('✨ '+cheers[Math.floor(Math.random()*cheers.length)])}
for(const p of document.querySelectorAll('.puzzle')){p.addEventListener('click',e=>tapAt(p,e.clientX,e.clientY));p.addEventListener('contextmenu',e=>e.preventDefault());p.addEventListener('dragstart',e=>e.preventDefault())}
for(const [fx,fy] of FOUND_JS)addMark(fx,fy,false);
showProgress(FOUND_JS.length,FOUND_JS.length>=total,false);
document.querySelector('#restart')?.addEventListener('click',async()=>{try{const r=await fetch('/reset',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:'{}'});if(r.status===403){location.reload();return}}catch(err){}document.querySelectorAll('.mark').forEach(m=>m.remove());showProgress(0,false,false);toast('🔄 New game, good luck!')});
const mem=document.querySelector('#memory');mem?.addEventListener('input',()=>{const n=mem.value.trim()?mem.value.trim().split(/\\s+/).length:0;document.querySelector('#words').textContent=n+' / 25 words';mem.setCustomValidity(n>25?'Please use 25 words or fewer.':'')});
</script>'''.replace('TOKEN', token).replace('IMG_W', str(IMAGE_WIDTH)).replace('IMG_H', str(IMAGE_HEIGHT)).replace('NSPOTS', str(NSPOTS)).replace('FOUND_JS', found_js)
    return page(body + ('' if closed else script))

@app.post('/spot')
def spot():
    checkcsrf()
    if not is_open():
        return jsonify(hit=False, error='closed'), 403
    data = request.get_json(silent=True) or {}
    try:
        if 'fx' in data:  # fraction of the displayed picture (0..1): independent of picture size
            x = float(data['fx']) * IMAGE_WIDTH; y = float(data['fy']) * IMAGE_HEIGHT
        else:
            x = float(data['x']); y = float(data['y'])
    except (KeyError, TypeError, ValueError):
        abort(400)
    if not (math.isfinite(x) and math.isfinite(y)):
        abort(400)
    found = puzzle_progress()
    if not (0 <= x <= IMAGE_WIDTH and 0 <= y <= IMAGE_HEIGHT):
        return jsonify(hit=False, found=len(found), total=NSPOTS)
    match = None
    best = HIT_RADIUS
    for i, (sx, sy) in enumerate(HOTSPOTS):
        if i in found:
            continue
        d = math.hypot(sx - x, sy - y)
        if d <= best:
            best = d
            match = i
    if match is None:
        return jsonify(hit=False, found=len(found), total=NSPOTS)
    found.add(match)
    session['found_spots'] = list(found)
    sx, sy = HOTSPOTS[match]
    solved = len(found) == NSPOTS
    if solved:
        session['solved'] = True
    return jsonify(hit=True, x=sx, y=sy, fx=sx / IMAGE_WIDTH, fy=sy / IMAGE_HEIGHT, found=len(found), total=NSPOTS, solved=solved)

@app.post('/reset')
def reset():
    checkcsrf()
    reset_progress()
    return jsonify(ok=True, found=0, total=NSPOTS)

@app.post('/enter')
def enter():
    checkcsrf()
    if not is_open():
        return page('<div class="error">Entries are closed.</div>')
    if request.form.get('website'):
        abort(400)
    if not (session.get('solved') and session.get('puzzle') == PUZZLE_VERSION):
        return page('<div class="error">Please complete the ' + str(NSPOTS) + ' differences before submitting. <a href="/">Try again</a>.</div>')
    ok, entrant, guardian, contact, phone, memory, marketing = _entry_fields()
    if not ok:
        return page('<div class="error">Please check your details, terms, phone number and 25-word limit. <a href="/">Try again</a>.</div>')
    digest = hashlib.sha256(contact.encode()).hexdigest()
    try:
        with conn() as c:
            c.execute('INSERT INTO entries(created,entrant,guardian,contact,phone,memory,consent,marketing,contact_hash,source) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (datetime.now().astimezone().isoformat(), entrant, guardian, contact, phone, memory, 1, int(marketing), digest, 'online'))
    except _DB_INTEGRITY:
        reset_progress()
        return page('<section class="panel"><h2>Already entered 🎄</h2><p>We have already received an entry for this email address. Only one entry per contact is allowed.</p></section>')
    reset_progress()
    return page('<section class="hero"><h1>YOU’RE IN! 🎉</h1><p>Thank you for sharing the Christmas magic. You’re now in the draw. Great family prizes to be won — see the <a href="/terms">competition terms</a> for details.</p></section><section class="panel"><p>Winners will be drawn at random after the competition closes and contacted by email. Good luck!</p></section>')

@app.get('/terms')
def terms():
    return page('''<section class="panel"><h1>Competition terms & privacy notice — draft</h1><p><b>Promoter:</b> LJ Hooker Alice Springs, Suite 2/52 Hartley Street, Alice Springs NT 0870 (confirm legal entity before launch).</p><p><b>Entry:</b> No purchase necessary. One entry per adult contact email. Entrants must find NSPOTS differences, submit a memory of no more than 25 words, and provide contact details. Under-18s must enter with parent/guardian permission; an adult must provide the email address. Paper entries may be returned to the office at Suite 2/52 Hartley Street, Alice Springs NT 0870, and are entered in the same draw.</p><p><b>Period:</b> 26 November to 21 December 2026, closing 11:59 pm ACST.</p><p><b>Prizes:</b> First prize, Ultimate Christmas Package: Alice Springs Cinema family pass, A$100 EB Games gift voucher, A$100 Toyworld gift card, and a 10-entry family swim card at the Alice Springs Aquatic &amp; Leisure Centre. Second prize, Family Fun Package: Alice Springs Cinema family pass, A$100 EB Games gift voucher, and A$100 Toyworld gift card. Third prize, Christmas Treat Package: Alice Springs Cinema family pass, A$50 EB Games gift voucher, and A$50 Toyworld gift card. Lucky-draw prize: A$50 Red Kangaroo Books gift voucher, redeemable in store only. No cash alternative unless promoter agrees.</p><p><b>Draw:</b> Four distinct eligible winners selected randomly at LJ Hooker Alice Springs, Suite 2/52 Hartley Street, Alice Springs NT 0870, on Tuesday 22 December 2026; one prize per winner. Winners will be contacted by phone/email and have 14 days from notification to collect their prize from the office. Any prize not claimed within 14 days will be redrawn from remaining eligible entries.</p><p><b>Privacy:</b> We collect names, adult/guardian contact email, adult/guardian contact phone number and submitted memory solely to administer the competition and contact winners. Optional marketing consent is separate and unchecked by default. Entrant information must be stored securely, accessible only to authorised staff and retained only as required by the approved retention policy. No public winner names or photos without appropriate consent.</p><p><b>Important:</b> These are draft rules, not a legal approval. Management must verify Northern Territory trade promotion requirements, eligibility, privacy, advertising claims and final prize details before publishing.</p></section>'''.replace('NSPOTS', str(NSPOTS)), 'Terms')

def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('admin'):
            return redirect('/admin/login')
        return fn(*args, **kwargs)
    return wrapped

@app.route('/admin/login', methods=['GET', 'POST'])
def login():
    if not ADMIN_PASSWORD:
        return page('<div class="error">Admin access is disabled until ADMIN_PASSWORD is configured on the server.</div>', 'Admin')
    err = ''
    if request.method == 'POST':
        checkcsrf()
        if login_locked():
            err = '<div class="error">Too many incorrect attempts. Please try again in 15 minutes.</div>'
        else:
            offered = hashlib.sha256(request.form.get('password', '').encode()).hexdigest()
            if hmac.compare_digest(offered, ADMIN_PASSWORD_HASH):
                session.clear()
                session['admin'] = True
                clear_login_fails()
                return redirect('/admin')
            record_login_fail()
            err = '<div class="error">Incorrect password.</div>'
            if login_locked():
                err = '<div class="error">Too many incorrect attempts. Please try again in 15 minutes.</div>'
            time.sleep(0.5)
    return page('<section class="panel login"><h2>Administrator login</h2><p class="muted small">Staff access only.</p>' + err + '<form method="post"><input type="hidden" name="csrf" value="' + csrf() + '"><label>Password</label><input type="password" name="password" required><p><button>Sign in</button></p></form></section>', 'Admin login')

@app.post('/admin/logout')
@admin_required
def logout():
    checkcsrf()
    session.clear()
    return redirect('/')

@app.get('/admin')
@admin_required
def admin():
    with conn() as c:
        entries = c.execute('SELECT * FROM entries ORDER BY id DESC').fetchall()
        draws = c.execute('SELECT d.place,e.entrant,e.contact,d.drawn_at FROM draws d JOIN entries e ON e.id=d.entry_id ORDER BY d.place').fetchall()
    rows = ''.join(f'<tr><td>{e["id"]}</td><td>{E(e["entrant"])}</td><td>{E(e["contact"])}</td><td>{E((e['phone'] if 'phone' in e.keys() else '') or '')}</td><td>{E((e['source'] if 'source' in e.keys() else 'online') or 'online')}</td><td>{E(e["memory"])}</td><td>{E(e["created"])}</td></tr>' for e in entries)
    winners = ''.join(f'<tr><td>{d["place"]}</td><td>{E(d["entrant"])}</td><td>{E(d["contact"])}</td><td>{E(d["drawn_at"])}</td></tr>' for d in draws)
    token = csrf()
    disabled = 'disabled' if is_open() or len(draws) > 0 or len(entries) < 4 else ''
    return page(f'''<section class="panel"><h2>🎄 Christmas competition administration</h2><div class="stats"><div><b>{len(entries)}</b> total entries</div><div><b>{len(draws)}</b> winners drawn</div></div><p><a class="btn secondary" href="/admin/paper-entry">Add paper entry</a> <a class="btn secondary" href="/admin/export">Export CSV</a></p><p class="muted small">Drawing is locked until the closing date. Once drawn, winners are saved and cannot be drawn again through the dashboard.</p><form method="post" action="/admin/draw"><input type="hidden" name="csrf" value="{token}"><button {disabled}>Draw four distinct winners</button></form><form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{token}"><p><button class="secondary">Log out</button></p></form></section><section class="panel"><h2>Winners</h2><div class="overflow"><table><tr><th>Place</th><th>Name</th><th>Email</th><th>Drawn</th></tr>{winners}</table></div></section><section class="panel"><h2>Entries (private)</h2><div class="overflow"><table><tr><th>ID</th><th>Name</th><th>Email</th><th>Phone</th><th>Source</th><th>Memory</th><th>Submitted</th></tr>{rows}</table></div></section>''', 'Admin')

def valid_au_phone(raw):
    compact = re.sub(r'[\s().-]+', '', (raw or '').strip())
    if compact.startswith('+61'):
        rest = compact[3:]
        if rest.startswith('0'):
            rest = rest[1:]
        return bool(re.fullmatch(r'[2-478]\d{8}', rest))
    return bool(re.fullmatch(r'0[2-478]\d{8}', compact))

def _entry_fields():
    entrant = request.form.get('entrant', '').strip()
    guardian = request.form.get('guardian', '').strip()
    contact = request.form.get('contact', '').strip().lower()
    phone = request.form.get('phone', '').strip()
    memory = request.form.get('memory', '').strip()
    terms = request.form.get('terms') == 'yes'
    marketing = request.form.get('marketing') == 'yes'
    ok = (2 <= len(entrant) <= 100 and 5 <= len(contact) <= 150 and '@' in contact and '.' in contact.split('@')[-1] and valid_au_phone(phone) and len(phone) <= 24 and 1 <= len(memory.split()) <= 25 and len(memory) <= 300 and terms)
    return ok, entrant, guardian, contact, phone, memory, marketing

def _paper_form(token, err=''):
    body = (
        '<section class="panel"><h2>Add a paper entry</h2>'
        '<p>Staff only. Use this when a completed activity sheet is returned to the office. '
        'Check that all 12 differences are marked, then enter the same details as the sheet. '
        'Paper entries go into the same draw as online entries.</p>ERR'
        '<form method="post" action="/admin/paper-entry">'
        '<input type="hidden" name="csrf" value="TOKEN">'
        '<label for="memory">Christmas memory (25 words or fewer)</label>'
        '<textarea name="memory" id="memory" maxlength="300" required></textarea>'
        '<div class="grid"><div>'
        "<label for=\"entrant\">Entrant's full name</label>"
        '<input id="entrant" name="entrant" maxlength="100" required></div><div>'
        '<label for="guardian">Parent/guardian name (if entrant is under 18)</label>'
        '<input id="guardian" name="guardian" maxlength="100"></div></div>'
        "<label for=\"contact\">Parent/guardian or adult entrant's email address</label>"
        '<input type="email" name="contact" id="contact" maxlength="150" required>'
        '<label for="phone">Parent/guardian contact number</label>'
        '<input type="tel" name="phone" id="phone" maxlength="24" required autocomplete="tel" inputmode="tel" placeholder="04xx xxx xxx or +61">'
        '<div class="check"><input type="checkbox" name="terms" value="yes" id="terms" required>'
        '<label for="terms">The sheet has the terms box ticked: the person is 18 or older and is entering for themselves or with the permission of the child\'s parent/guardian, and accepts the competition terms and privacy notice.</label></div>'
        '<div class="check"><input type="checkbox" name="marketing" value="yes" id="marketing">'
        '<label for="marketing">The optional marketing box is ticked on the sheet. Leave this unticked if it is not.</label></div>'
        '<div class="check"><input type="checkbox" name="checked" value="yes" id="checked" required>'
        '<label for="checked">I have checked this returned sheet (all differences found and details complete) and I am entering it for the office.</label></div>'
        '<p><button>Save paper entry</button> <a class="btn secondary" href="/admin">Back to admin</a></p>'
        '<p class="muted small">One entry per contact email, the same as online. A paper entry is refused if that email is already in the draw.</p>'
        '</form></section>'
    )
    return page(body.replace('TOKEN', token).replace('ERR', err), 'Add paper entry')

@app.route('/admin/paper-entry', methods=['GET', 'POST'])
@admin_required
def paper_entry():
    token = csrf()
    if request.method == 'GET':
        return _paper_form(token)
    checkcsrf()
    with conn() as c:
        drawn = c.execute('SELECT COUNT(*) FROM draws').fetchone()[0]
    if drawn:
        return page('<div class="error">The draw has already been run, so no further entries can be added. <a href="/admin">Back to admin</a>.</div>', 'Add paper entry')
    if request.form.get('checked') != 'yes':
        return _paper_form(token, '<div class="error">Please confirm you have checked the returned sheet.</div>')
    ok, entrant, guardian, contact, phone, memory, marketing = _entry_fields()
    if not ok:
        return _paper_form(token, '<div class="error">Please check the details, terms, phone number and 25-word limit.</div>')
    digest = hashlib.sha256(contact.encode()).hexdigest()
    try:
        with conn() as c:
            c.execute('INSERT INTO entries(created,entrant,guardian,contact,phone,memory,consent,marketing,contact_hash,source) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (datetime.now().astimezone().isoformat(), entrant, guardian, contact, phone, memory, 1, int(marketing), digest, 'paper'))
    except _DB_INTEGRITY:
        return _paper_form(token, '<div class="error">This email already has an entry (online or paper). Only one entry per contact email is allowed.</div>')
    return page('<section class="panel"><h2>Paper entry saved</h2><p>This entry is now in the same draw as the online entries.</p><p><a class="btn secondary" href="/admin/paper-entry">Add another paper entry</a> <a class="btn secondary" href="/admin">Back to admin</a></p></section>', 'Add paper entry')


@app.get('/admin/export')
@admin_required
def export():
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['id', 'created', 'entrant', 'guardian', 'contact', 'phone', 'memory', 'marketing_opt_in', 'source'])
    with conn() as c:
        for e in c.execute('SELECT id,created,entrant,guardian,contact,phone,memory,marketing,source FROM entries ORDER BY id'):
            writer.writerow([e[k] for k in ['id', 'created', 'entrant', 'guardian', 'contact', 'phone', 'memory', 'marketing', 'source']])
    return Response(out.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=christmas_entries.csv', 'Cache-Control': 'no-store'})

@app.post('/admin/draw')
@admin_required
def draw():
    checkcsrf()
    if is_open():
        abort(403)
    c = conn()
    c.isolation_level = None
    try:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT COUNT(*) FROM draws').fetchone()[0]:
            c.execute('ROLLBACK')
            abort(409)
        ids = [r[0] for r in c.execute('SELECT id FROM entries')]
        if len(ids) < 4:
            c.execute('ROLLBACK')
            abort(400)
        winners = secrets.SystemRandom().sample(ids, 4)
        now = datetime.now().astimezone().isoformat()
        for i, entry_id in enumerate(winners, 1):
            c.execute('INSERT INTO draws(place,entry_id,drawn_at) VALUES(?,?,?)', (i, entry_id, now))
        c.execute('COMMIT')
    except Exception:
        try:
            c.execute('ROLLBACK')
        except Exception:
            pass
        raise
    finally:
        c.close()
    return redirect('/admin')

@app.after_request
def security_headers(r):
    r.headers['X-Content-Type-Options'] = 'nosniff'
    r.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    r.headers['X-Frame-Options'] = 'DENY'
    r.headers['Cache-Control'] = 'no-store' if request.path.startswith('/admin') else 'no-cache'
    return r

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')), debug=False)
