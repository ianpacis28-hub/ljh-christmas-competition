import os, sqlite3, secrets, csv, io, hmac, hashlib, time, math, json, sys
from datetime import datetime
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
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
ADMIN_PASSWORD_HASH = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest() if ADMIN_PASSWORD else ''
CLOSING = os.getenv('CLOSING_DATE', '2026-12-17T23:59:59+09:30')
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
        if len(spots) != 10:
            raise ValueError('expected 10 points, got %s' % len(spots))
        return spots
    except SystemExit:
        raise
    except Exception as e:
        msg = 'HOTSPOTS_JSON is invalid: %s' % e
        print(msg, file=sys.stderr)
        raise SystemExit(msg)

HOTSPOTS = load_hotspots()
_login_attempts = {}

def _puzzle_size():
    from PIL import Image
    path = os.path.join(app.static_folder or 'static', 'Christmas_Spot_10_A.png')
    with Image.open(path) as im:
        return im.size
IMAGE_WIDTH, IMAGE_HEIGHT = _puzzle_size()
HIT_RADIUS = max(48, int(round(min(IMAGE_WIDTH, IMAGE_HEIGHT) * 0.07)))

def conn():
    c = sqlite3.connect(DB, timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=5000')
    return c

def init():
    with conn() as c:
        c.execute('CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY, created TEXT NOT NULL, entrant TEXT NOT NULL, guardian TEXT, contact TEXT NOT NULL, memory TEXT NOT NULL, consent INTEGER NOT NULL, marketing INTEGER NOT NULL DEFAULT 0, contact_hash TEXT UNIQUE NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS draws(place INTEGER PRIMARY KEY, entry_id INTEGER UNIQUE NOT NULL, drawn_at TEXT NOT NULL)')
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

BASE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }} | LJ Hooker Alice Springs Christmas</title><style>
:root{font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;color:#262329;background:#fbf7f3}*{box-sizing:border-box}body{margin:0}header{background:#a41e2b;color:white;padding:16px 5%;display:flex;justify-content:space-between;align-items:center}header b{letter-spacing:.1em;font-size:15px}header span{font-size:12px;opacity:.9}main{max-width:1020px;margin:auto;padding:30px 18px 80px}.hero{background:linear-gradient(125deg,#a41e2b,#641823);color:white;border-radius:24px;padding:40px 32px;text-align:center}.eyebrow{text-transform:uppercase;letter-spacing:.18em;font-size:12px;font-weight:700}.hero h1{font-size:clamp(34px,6vw,60px);line-height:1.05;margin:12px 0}.hero p{font-size:17px;opacity:.94}.pill{display:inline-block;border-radius:30px;padding:9px 17px;background:#fff2d3;color:#6b3210;font-weight:800}.panel{background:white;border:1px solid #ebdfd9;box-shadow:0 6px 30px #3d1e1910;border-radius:20px;padding:25px;margin-top:22px}h2{margin:0 0 14px;font-size:24px}h3{margin:0 0 8px}.muted{color:#716c72}.prizes{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.prize{border:1px solid #eadfdc;border-radius:14px;padding:15px;background:#fffaf7}.prize strong{display:block;font-size:21px;color:#a41e2b;margin-top:8px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.puzzle{position:relative;overflow:hidden;border:2px solid #e5d9d4;border-radius:14px;cursor:crosshair;touch-action:manipulation}.puzzle img{width:100%;display:block}.mark{position:absolute;border:4px solid #11a26a;background:#11a26a36;border-radius:50%;width:7%;aspect-ratio:1;transform:translate(-50%,-50%);pointer-events:none}label{font-weight:700;display:block;margin:15px 0 7px}input,textarea{width:100%;border:1px solid #cfc3c0;border-radius:10px;padding:13px;font:inherit}textarea{min-height:110px;resize:vertical}.check{display:flex;gap:10px;align-items:start;margin-top:15px}.check input{width:auto;margin-top:5px}.check label{margin:0;font-weight:400}button,.btn{display:inline-block;background:#a41e2b;border:0;border-radius:12px;padding:15px 22px;color:white;font:inherit;font-weight:800;cursor:pointer;text-decoration:none}button:disabled{background:#a9a3a5;cursor:not-allowed}.secondary{background:#302c30}.progress{background:#f1e7e4;border-radius:20px;height:13px;overflow:hidden}.bar{background:#168c62;height:100%;width:0%;transition:width .2s}.error{background:#ffebea;color:#8c1723;padding:12px;border-radius:10px;margin:15px 0}.success{background:#e6f7ee;color:#12603b;padding:14px;border-radius:10px}.small{font-size:13px}footer{text-align:center;padding:25px;color:#766b6b;font-size:12px}.adminlink{color:#eee;font-size:12px}.stats{display:flex;gap:15px;flex-wrap:wrap}.stats div{padding:18px;border-radius:14px;background:#fff5ee}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:10px;border-bottom:1px solid #eee;font-size:13px} .overflow{overflow:auto}@media(max-width:700px){.prizes{grid-template-columns:1fr 1fr}.grid{grid-template-columns:1fr}.hero{padding:28px 16px}.panel{padding:18px}} </style></head><body><header><b>LJ HOOKER <span>ALICE SPRINGS</span></b><span>CHRISTMAS 2026</span></header><main>{{ body|safe }}</main><footer>Community competition concept · LJ Hooker Alice Springs · Subject to final terms and management approval</footer></body></html>'''

def page(body, title='Spot the Magic'):
    return render_template_string(BASE, title=title, body=body)

@app.get('/')
def home():
    token = csrf()
    closed = not is_open()
    body = '''<section class="hero"><div class="eyebrow">A little Christmas magic for Alice Springs</div><h1>SPOT THE MAGIC<br>OF CHRISTMAS 🎄</h1><p>Find 10 differences. Share your favourite Christmas memory in 25 words or less.<br>Enter for a chance to win one of five prizes!</p><span class="pill">5 PRIZES • 5 WINNERS</span></section>
 <section class="panel"><h2>🎁 Five chances to win</h2><div class="prizes"><div class="prize">🥇 First prize<strong>$500</strong>Ultimate Christmas Package</div><div class="prize">🥈 Second prize<strong>$300</strong>Family Fun Package</div><div class="prize">🥉 Third prize<strong>$150</strong>Christmas Treat Package</div><div class="prize">🎬 Fourth prize<strong>Cinema</strong>Family-of-four voucher</div><div class="prize">🍿 Fifth prize<strong>Cinema</strong>Family-of-four voucher</div></div><p class="muted small">Cinema voucher values and conditions to be confirmed. Prizes are proposed pending approval.</p></section>
 <section class="panel"><h2>1. Find the 10 differences</h2><p class="muted">Tap the differences in either picture. Green circles show what you have found. Zoom in on your phone if needed.</p><p><b id="count">0 / 10 found</b></p><div class="progress"><div class="bar" id="bar"></div></div><div class="grid" style="margin-top:16px"><div><h3>Picture A</h3><div class="puzzle" data-puzzle="a"><img alt="Christmas scene A" src="/static/Christmas_Spot_10_A.png"></div></div><div><h3>Picture B</h3><div class="puzzle" data-puzzle="b"><img alt="Christmas scene B" src="/static/Christmas_Spot_10_B.png"></div></div></div><p id="puzzlemsg" class="muted small" aria-live="polite">Find all 10 to unlock the entry form.</p></section>
 <section class="panel" id="entry"><h2>2. Your favourite Christmas memory</h2><p>Tell us your favourite Christmas memory in <b>25 words or less</b>.</p><form method="post" action="/enter" id="entryform"><input type="hidden" name="csrf" value="TOKEN"><div style="display:none"><label>Leave this field empty<input name="website" autocomplete="off"></label></div><label for="memory">My Christmas memory</label><textarea name="memory" id="memory" maxlength="300" required placeholder="One Christmas I remember..."></textarea><div class="muted small" id="words">0 / 25 words</div><div class="grid"><div><label for="entrant">Entrant's full name</label><input id="entrant" name="entrant" maxlength="100" required></div><div><label for="guardian">Parent/guardian name (if entrant is under 18)</label><input id="guardian" name="guardian" maxlength="100"></div></div><label for="contact">Parent/guardian or adult entrant's email address</label><input type="email" name="contact" id="contact" maxlength="150" required><div class="check"><input type="checkbox" name="terms" value="yes" id="terms" required><label for="terms">I am 18 or older and am entering for myself or with the permission of the child’s parent/guardian. I accept the <a href="/terms" target="_blank">competition terms and privacy notice</a>.</label></div><div class="check"><input type="checkbox" name="marketing" value="yes" id="marketing"><label for="marketing">Optional: I would like to receive LJ Hooker Alice Springs marketing emails. This is not required to enter.</label></div><p><button id="submit" disabled>Complete the puzzle to enter</button></p><p class="muted small">One entry per contact email. No purchase necessary. The draw is random; finding the differences and completing the memory are eligibility steps, not judging criteria.</p></form></section>'''.replace('TOKEN', token)
    if closed:
        body = '<section class="hero"><h1>Entries are closed</h1><p>Thank you for joining our Christmas community giveaway.</p></section>'
    script = '''<script>
const csrfToken="TOKEN",imgW=IMG_W,imgH=IMG_H;
for(const p of document.querySelectorAll('.puzzle'))p.addEventListener('click',async e=>{const r=p.getBoundingClientRect(),x=(e.clientX-r.left)/r.width*imgW,y=(e.clientY-r.top)/r.height*imgH;let data;try{const res=await fetch('/spot',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({x,y})});if(!res.ok)return;data=await res.json()}catch(err){return}if(!data.hit)return;for(const q of document.querySelectorAll('.puzzle')){const m=document.createElement('span');m.className='mark';m.style.left=(data.x/imgW*100)+'%';m.style.top=(data.y/imgH*100)+'%';q.append(m)}document.querySelector('#count').textContent=data.found+' / 10 found';document.querySelector('#bar').style.width=data.found*10+'%';if(data.solved){document.querySelector('#puzzlemsg').textContent='Wonderful! You found all 10. Complete your entry below.';document.querySelector('#submit').disabled=false;document.querySelector('#submit').textContent='Enter the Christmas draw'}});
const mem=document.querySelector('#memory');mem?.addEventListener('input',()=>{const n=mem.value.trim()?mem.value.trim().split(/\\s+/).length:0;document.querySelector('#words').textContent=n+' / 25 words';mem.setCustomValidity(n>25?'Please use 25 words or fewer.':'')});
</script>'''.replace('TOKEN', token).replace('IMG_W', str(IMAGE_WIDTH)).replace('IMG_H', str(IMAGE_HEIGHT))
    return page(body + ('' if closed else script))

@app.post('/spot')
def spot():
    checkcsrf()
    if not is_open():
        return jsonify(hit=False, error='closed'), 403
    data = request.get_json(silent=True) or {}
    try:
        x = float(data['x']); y = float(data['y'])
    except (KeyError, TypeError, ValueError):
        abort(400)
    if not (0 <= x <= IMAGE_WIDTH and 0 <= y <= IMAGE_HEIGHT):
        return jsonify(hit=False, found=len(session.get('found_spots') or []), total=10)
    found = set(session.get('found_spots') or [])
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
        return jsonify(hit=False, found=len(found), total=10)
    found.add(match)
    session['found_spots'] = list(found)
    sx, sy = HOTSPOTS[match]
    solved = len(found) == 10
    if solved:
        session['solved'] = True
    return jsonify(hit=True, x=sx, y=sy, found=len(found), total=10, solved=solved)

@app.post('/enter')
def enter():
    checkcsrf()
    if not is_open():
        return page('<div class="error">Entries are closed.</div>')
    if request.form.get('website'):
        abort(400)
    if not session.get('solved'):
        return page('<div class="error">Please complete the 10 differences before submitting. <a href="/">Try again</a>.</div>')
    entrant = request.form.get('entrant', '').strip()
    guardian = request.form.get('guardian', '').strip()
    contact = request.form.get('contact', '').strip().lower()
    memory = request.form.get('memory', '').strip()
    terms = request.form.get('terms') == 'yes'
    marketing = request.form.get('marketing') == 'yes'
    if not (2 <= len(entrant) <= 100 and 5 <= len(contact) <= 150 and '@' in contact and '.' in contact.split('@')[-1] and 1 <= len(memory.split()) <= 25 and len(memory) <= 300 and terms):
        return page('<div class="error">Please check your details, terms and 25-word limit. <a href="/">Try again</a>.</div>')
    digest = hashlib.sha256(contact.encode()).hexdigest()
    try:
        with conn() as c:
            c.execute('INSERT INTO entries(created,entrant,guardian,contact,memory,consent,marketing,contact_hash) VALUES(?,?,?,?,?,?,?,?)',
                      (datetime.now().astimezone().isoformat(), entrant, guardian, contact, memory, 1, int(marketing), digest))
    except sqlite3.IntegrityError:
        return page('<section class="panel"><h2>Already entered 🎄</h2><p>We have already received an entry for this email address. Only one entry per contact is allowed.</p></section>')
    return page('<section class="hero"><h1>YOU’RE IN! 🎉</h1><p>Thank you for sharing the Christmas magic. Your entry has been received for the five-prize random draw.</p></section><section class="panel"><p>Keep an eye on your email for the winner notification. Good luck!</p></section>')

@app.get('/terms')
def terms():
    return page('''<section class="panel"><h1>Competition terms & privacy notice — draft</h1><p><b>Promoter:</b> LJ Hooker Alice Springs (confirm legal entity and address before launch).</p><p><b>Entry:</b> No purchase necessary. One entry per adult contact email. Entrants must find 10 differences, submit a memory of no more than 25 words, and provide contact details. Under-18s must enter with parent/guardian permission; an adult must provide the email address.</p><p><b>Period:</b> Proposed 23 November to 17 December 2026, closing 11:59 pm ACST. Confirm dates before publication.</p><p><b>Prizes:</b> First $500 Christmas package, second $300 family package, third $150 treats package, fourth and fifth family-of-four cinema vouchers (cinema, value and ticket conditions TBC). No cash alternative unless promoter agrees.</p><p><b>Draw:</b> Five distinct eligible winners selected randomly after closing; one prize per winner. Winners contacted using submitted email. Draw date, location, redraw procedure and prize collection deadline must be finalised before launch.</p><p><b>Privacy:</b> We collect names, adult/guardian contact email and submitted memory solely to administer the competition and contact winners. Optional marketing consent is separate and unchecked by default. Entrant information must be stored securely, accessible only to authorised staff and retained only as required by the approved retention policy. No public winner names or photos without appropriate consent.</p><p><b>Important:</b> These are draft rules, not a legal approval. Management must verify Northern Territory trade promotion requirements, eligibility, privacy, advertising claims and final prize details before publishing.</p></section>''', 'Terms')

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
    return page('<section class="panel"><h2>Secure administrator login</h2>' + err + '<form method="post"><input type="hidden" name="csrf" value="' + csrf() + '"><label>Password</label><input type="password" name="password" required><p><button>Sign in</button></p></form></section>', 'Admin login')

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
    rows = ''.join(f'<tr><td>{e["id"]}</td><td>{E(e["entrant"])}</td><td>{E(e["contact"])}</td><td>{E(e["memory"])}</td><td>{E(e["created"])}</td></tr>' for e in entries)
    winners = ''.join(f'<tr><td>{d["place"]}</td><td>{E(d["entrant"])}</td><td>{E(d["contact"])}</td><td>{E(d["drawn_at"])}</td></tr>' for d in draws)
    token = csrf()
    disabled = 'disabled' if is_open() or len(draws) > 0 or len(entries) < 5 else ''
    return page(f'''<section class="panel"><h2>Christmas raffle administration</h2><div class="stats"><div><b>{len(entries)}</b> total entries</div><div><b>{len(draws)}</b> winners drawn</div></div><p><a class="btn secondary" href="/admin/export">Export CSV</a></p><p class="muted small">Drawing is locked until the closing date. Once drawn, winners are saved and cannot be drawn again through the dashboard.</p><form method="post" action="/admin/draw"><input type="hidden" name="csrf" value="{token}"><button {disabled}>Draw five distinct winners</button></form><form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{token}"><p><button class="secondary">Log out</button></p></form></section><section class="panel"><h2>Winners</h2><div class="overflow"><table><tr><th>Place</th><th>Name</th><th>Email</th><th>Drawn</th></tr>{winners}</table></div></section><section class="panel"><h2>Entries (private)</h2><div class="overflow"><table><tr><th>ID</th><th>Name</th><th>Email</th><th>Memory</th><th>Submitted</th></tr>{rows}</table></div></section>''', 'Admin')

@app.get('/admin/export')
@admin_required
def export():
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['id', 'created', 'entrant', 'guardian', 'contact', 'memory', 'marketing_opt_in'])
    with conn() as c:
        for e in c.execute('SELECT id,created,entrant,guardian,contact,memory,marketing FROM entries ORDER BY id'):
            writer.writerow([e[k] for k in ['id', 'created', 'entrant', 'guardian', 'contact', 'memory', 'marketing']])
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
        if len(ids) < 5:
            c.execute('ROLLBACK')
            abort(400)
        winners = secrets.SystemRandom().sample(ids, 5)
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
