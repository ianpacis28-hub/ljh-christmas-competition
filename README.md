# LJ Hooker Alice Springs — Christmas Competition 2026

A working Flask website with a mobile-first public competition, 10 tappable differences (using the two supplied illustrations), 25-word memory validation, five prize listings, a database-backed entry form, and password-protected raffle administration.

Difference answers are not stored in this repository. They must be provided at runtime as the `HOTSPOTS_JSON` environment variable (a JSON list of `[x, y]` points, one point per difference, in the pixel space of the puzzle images). Keep that value only in the host environment or a local file that is never committed.

## Run locally

```bash
python -m pip install -r requirements.txt
export SECRET_KEY="$(python -c 'import secrets;print(secrets.token_hex(32))')"
export ADMIN_PASSWORD="choose-a-long-unique-password"
export COOKIE_SECURE=0
export HOTSPOTS_JSON='<the JSON list of 10 points; do not commit this value>'
python app.py
```

The app listens on `PORT` if set, otherwise port 8080. Open http://127.0.0.1:8080 and admin at http://127.0.0.1:8080/admin/login . On Windows PowerShell use `$env:ADMIN_PASSWORD="..."`, `$env:COOKIE_SECURE="0"`, `$env:HOTSPOTS_JSON="..."`, etc.

## Deploy for testing (Render)

This copy is set up so it can run on Render's free plan for testing. That plan has **no persistent disk**, so `DATABASE_PATH` defaults to a local `entries.sqlite3` file. Entries and draw results will be lost when the service restarts, sleeps, or is redeployed. That is acceptable for a test deploy only. Do not run a live competition on the free plan.

Use a production WSGI start command such as:

```bash
waitress-serve --listen=0.0.0.0:${PORT:-8080} app:app
```

Set these environment variables on the host (do not put them in the repository):

- `SECRET_KEY` — a long random value
- `ADMIN_PASSWORD` — a long unique password
- `COOKIE_SECURE=1`
- `HOTSPOTS_JSON` — the JSON list of 10 `[x, y]` points
- `CLOSING_DATE` — ISO-8601 with the ACST offset, once dates are confirmed
- `PUBLIC_URL` — the live https URL, once you have it
- `DATABASE_PATH` — optional; defaults to `entries.sqlite3` in the service's ephemeral filesystem

Enable HTTPS (Render provides this), platform rate limiting if available, and access logging without personal data. The admin dashboard is password protected but should additionally be placed behind identity-aware access control/MFA or restricted to staff IPs for anything beyond a private test. **Do not deploy a public competition without a security review, management approval, and legally reviewed terms.**

## QR code

Once you have the real public HTTPS URL, run:

```bash
python make_qr.py https://your-real-competition-domain.example/
```

This creates `competition_qr.png`. A shared QR code is simply a link; anyone with the link can participate. A sample local QR code is deliberately not included because it would not work on visitors' phones.

## Important before launch

- Obtain management approval and confirm the prize descriptions, cinema vouchers, promotion dates and draw rules.
- Replace the draft terms with legally reviewed Northern Territory competition terms and privacy notice; add legal promoter details, contact, privacy retention period, draw location and date, redraw process, winner notification and any permit/registration obligations.
- The code currently allows one entry per email, **not** necessarily one per individual. Verify eligibility and duplicates before drawing.
- Parent/guardian permission is attested but not independently verified. Confirm child-safety and consent process.
- Entry form currently requires all ten differences. A wrong tap does nothing; no solution is displayed until all ten are found.
- Winners are selected by cryptographic system randomness and persisted. On a plan with only ephemeral storage, back up is not available — use persistent storage or managed Postgres before a real draw.
- There is no email sending or automatic winner notification; staff contact winners manually.
- Artwork is provided from the existing proposal. Validate the ten hotspot positions on all devices before launch.

## Changes in this copy

The public look and competition rules are unchanged. The following reliability and security fixes were applied after a local review:

- CSRF checks now reject missing or empty tokens (the original accepted a request with no token and no session).
- Difference answers are not in the repository or the page JavaScript. Clicks are checked on the server using `HOTSPOTS_JSON`; the entry form only unlocks after all 10 are found in that session.
- Admin login hashes the password before a constant-time compare, and locks an IP for 15 minutes after 5 failed attempts.
- SQLite uses WAL mode, a busy timeout, and an immediate transaction around the raffle draw so two clicks cannot draw twice.
- `ProxyFix` is enabled so HTTPS and client IPs work correctly behind Render or another proxy.

Do not deploy a public competition without a security review, management approval, and legally reviewed terms.
