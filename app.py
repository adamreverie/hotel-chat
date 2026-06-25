import os
import re
import json
import time
import base64
import secrets
import urllib.request
import urllib.error
import resend
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, Response
import anthropic
from pywebpush import webpush, WebPushException

try:
    import psycopg2
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

# ── APP & CLIENTS ─────────────────────────────────────────────────────────────

app    = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 6 * 1024 * 1024  # 6 MB cap on uploads
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
resend.api_key = os.environ.get("RESEND_API_KEY")

VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY")
VAPID_EMAIL       = os.environ.get("VAPID_EMAIL", "mailto:hello@favvi.ai")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
CRON_SECRET    = os.environ.get("CRON_SECRET", "")

# ── DATABASE ──────────────────────────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL")
USE_POSTGRES = bool(DATABASE_URL and HAS_POSTGRES)

def get_conn():
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect('feedback.db')

def ph():
    """Return the right placeholder for the active DB driver."""
    return "%s" if USE_POSTGRES else "?"


def init_db():
    conn = get_conn(); c = conn.cursor()
    p = ph()
    if USE_POSTGRES:
        c.execute('''CREATE TABLE IF NOT EXISTS feedback (
                     id SERIAL PRIMARY KEY,
                     guest_name TEXT, room_number TEXT,
                     overall INTEGER, cleanliness INTEGER, staff INTEGER,
                     dining INTEGER, wifi INTEGER,
                     comment TEXT, date TEXT, hotel_slug TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS requests (
                     id SERIAL PRIMARY KEY,
                     room_number TEXT, department TEXT, details TEXT,
                     status TEXT DEFAULT 'new', claimed_by TEXT, date TEXT,
                     hotel_slug TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS hotels (
                     id SERIAL PRIMARY KEY,
                     name TEXT, slug TEXT UNIQUE, email TEXT, password TEXT,
                     system_prompt TEXT, staff_password TEXT, manager_password TEXT,
                     date_created TEXT,
                     hotel_info TEXT, current_offers TEXT, manager_email TEXT,
                     staff_knowledge TEXT,
                     trial_ends_at TIMESTAMP, subscription_status TEXT,
                     lemon_customer_id TEXT, lemon_subscription_id TEXT,
                     menu_content TEXT, menu_filename TEXT, menu_pdf TEXT,
                     menu_notes TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions (
                     id SERIAL PRIMARY KEY,
                     hotel_slug TEXT, staff_name TEXT, department TEXT,
                     subscription TEXT, created_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS password_resets (
                     id SERIAL PRIMARY KEY,
                     email TEXT, slug TEXT, token TEXT UNIQUE,
                     expires_at TEXT, used INTEGER DEFAULT 0)''')
        # Safe migrations for existing Postgres databases (no-op if column exists)
        for sql in [
            "ALTER TABLE hotels ADD COLUMN IF NOT EXISTS menu_content TEXT",
            "ALTER TABLE hotels ADD COLUMN IF NOT EXISTS menu_filename TEXT",
            "ALTER TABLE hotels ADD COLUMN IF NOT EXISTS menu_pdf TEXT",
            "ALTER TABLE hotels ADD COLUMN IF NOT EXISTS menu_notes TEXT",
        ]:
            try: c.execute(sql)
            except Exception: pass
    else:
        c.execute('''CREATE TABLE IF NOT EXISTS feedback
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     guest_name TEXT, room_number TEXT,
                     overall INTEGER, cleanliness INTEGER, staff INTEGER,
                     dining INTEGER, wifi INTEGER,
                     comment TEXT, date TEXT, hotel_slug TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS requests
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     room_number TEXT, department TEXT, details TEXT,
                     status TEXT DEFAULT 'new', claimed_by TEXT, date TEXT,
                     hotel_slug TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS hotels
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     name TEXT, slug TEXT UNIQUE, email TEXT, password TEXT,
                     system_prompt TEXT, staff_password TEXT, manager_password TEXT,
                     date_created TEXT,
                     hotel_info TEXT, current_offers TEXT, manager_email TEXT,
                     staff_knowledge TEXT,
                     menu_content TEXT, menu_filename TEXT, menu_pdf TEXT,
                     menu_notes TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     hotel_slug TEXT, staff_name TEXT, department TEXT,
                     subscription TEXT, created_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS password_resets
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     email TEXT, slug TEXT, token TEXT UNIQUE,
                     expires_at TEXT, used INTEGER DEFAULT 0)''')
        # Safe migrations for older SQLite databases
        for sql in [
            "ALTER TABLE hotels   ADD COLUMN hotel_info TEXT",
            "ALTER TABLE hotels   ADD COLUMN current_offers TEXT",
            "ALTER TABLE hotels   ADD COLUMN manager_email TEXT",
            "ALTER TABLE hotels   ADD COLUMN staff_knowledge TEXT",
            "ALTER TABLE hotels   ADD COLUMN menu_content TEXT",
            "ALTER TABLE hotels   ADD COLUMN menu_filename TEXT",
            "ALTER TABLE hotels   ADD COLUMN menu_pdf TEXT",
            "ALTER TABLE hotels   ADD COLUMN menu_notes TEXT",
            "ALTER TABLE requests ADD COLUMN hotel_slug TEXT",
            "ALTER TABLE feedback ADD COLUMN hotel_slug TEXT",
        ]:
            try: c.execute(sql)
            except Exception: pass

    conn.commit(); conn.close()

init_db()


# ── AUTH HELPERS ──────────────────────────────────────────────────────────────

def verify_hotel_password(slug, password, role):
    """Server-side password check — never exposes passwords to clients."""
    if not slug or not password:
        return False
    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT staff_password, manager_password FROM hotels WHERE slug = {ph()}', (slug,))
    row = c.fetchone(); conn.close()
    if not row:
        return False
    staff_pw, manager_pw = row[0], row[1]
    if role == 'manager':
        return password == manager_pw
    # staff: either staff OR manager password is accepted
    return password in (staff_pw, manager_pw)


def is_admin():
    """Check admin credentials from body or query string. Never crashes on GET."""
    body     = request.get_json(silent=True) or {}
    supplied = (body.get('admin_password') or body.get('password')
                or request.args.get('admin_password', '')
                or request.args.get('password', ''))
    return bool(ADMIN_PASSWORD and supplied == ADMIN_PASSWORD)


@app.route('/verify-password', methods=['POST'])
def verify_password():
    data  = request.json or {}
    valid = verify_hotel_password(data.get('slug'), data.get('password'), data.get('role', 'staff'))
    if not valid:
        time.sleep(0.4)   # slow brute-force attempts
    return jsonify({"valid": valid})


# ── STATIC & PAGE ROUTES ──────────────────────────────────────────────────────

@app.route('/')
def home(): return send_from_directory('.', 'landing.html')

@app.route('/signup')
def signup_page(): return send_from_directory('.', 'signup.html')

@app.route('/login')
def login_page(): return send_from_directory('.', 'login.html')

@app.route('/signup-success')
def signup_success_page(): return send_from_directory('.', 'signup-success.html')

@app.route('/guide/notifications')
def notifications_guide(): return send_from_directory('.', 'notifications.html')

@app.route('/terms')
def terms(): return send_from_directory('.', 'terms.html')

@app.route('/privacy')
def privacy(): return send_from_directory('.', 'privacy.html')

@app.route('/feedback')
def feedback_page(): return send_from_directory('.', 'feedback.html')

@app.route('/sw.js')
def service_worker():
    return send_from_directory('.', 'sw.js', mimetype='application/javascript')

@app.route('/manifest.json')
def manifest():
    return send_from_directory('.', 'manifest.json', mimetype='application/json')

@app.route('/portal/<slug>')
def portal(slug):
    # Always serve the portal shell — portal.html checks if the hotel exists
    # and, if deleted, clears saved login and redirects to /login.
    return send_from_directory('.', 'portal.html')

@app.route('/portal/<slug>/chat')
def portal_chat(slug):       return send_from_directory('.', 'index.html')

@app.route('/portal/<slug>/staff')
def portal_staff(slug):      return send_from_directory('.', 'staff.html')

@app.route('/portal/<slug>/staffchat')
def portal_staffchat(slug):  return send_from_directory('.', 'staffchat.html')

@app.route('/portal/<slug>/feedback')
def portal_feedback(slug):   return send_from_directory('.', 'feedback.html')

@app.route('/portal/<slug>/dashboard')
def portal_dashboard(slug):  return send_from_directory('.', 'dashboard.html')

@app.route('/portal/<slug>/settings')
def portal_settings(slug):   return send_from_directory('.', 'settings.html')

@app.route('/portal/<slug>/links')
def portal_links(slug):      return send_from_directory('.', 'links.html')


# ── HOTEL CONFIG ──────────────────────────────────────────────────────────────

@app.route('/get-hotel/<slug>')
def get_hotel(slug):
    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT * FROM hotels WHERE slug = {ph()}', (slug,))
    hotel = c.fetchone(); conn.close()
    if not hotel:
        return jsonify({"error": "Not found"}), 404

    # Check trial status — block if expired
    if USE_POSTGRES and len(hotel) > 14 and hotel[14]:
        sub_status = hotel[14]  # subscription_status column
        trial_ends = hotel[13]  # trial_ends_at column
        if sub_status == 'on_trial' and trial_ends:
            try:
                now = datetime.now()
                # Strip tzinfo so naive/aware datetimes can be compared safely
                if getattr(trial_ends, 'tzinfo', None) is not None:
                    trial_ends = trial_ends.replace(tzinfo=None)
                if now > trial_ends:
                    return jsonify({"error": "Trial expired"}), 403
            except Exception as e:
                print(f"Trial check skipped for {slug}: {e}")

    # Menu fields fetched explicitly by name (robust to column ordering)
    menu_filename = ""
    menu_notes    = ""
    try:
        conn2 = get_conn(); c2 = conn2.cursor()
        c2.execute(f'SELECT menu_filename, menu_notes FROM hotels WHERE slug = {ph()}', (hotel[2],))
        mrow = c2.fetchone(); conn2.close()
        if mrow:
            menu_filename = mrow[0] or ""
            menu_notes    = mrow[1] or ""
    except Exception:
        pass

    return jsonify({
        "id":           hotel[0],
        "name":         hotel[1],
        "slug":         hotel[2],
        "email":        hotel[3],
        # Passwords are NOT returned — login now goes through /verify-password
        "hotel_info":      hotel[9]  if len(hotel) > 9  and hotel[9]  else "",
        "current_offers":  hotel[10] if len(hotel) > 10 and hotel[10] else "",
        "manager_email":   hotel[11] if len(hotel) > 11 and hotel[11] else hotel[3],
        "staff_knowledge": hotel[12] if len(hotel) > 12 and hotel[12] else "",
        "menu_filename":   menu_filename,
        "menu_notes":      menu_notes,
    })


@app.route('/hotel-login', methods=['POST'])
def hotel_login():
    """Portal login (email + account password)."""
    data     = request.json or {}
    email    = data.get('email', '').strip()
    password = data.get('password', '').strip()
    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT slug, name FROM hotels WHERE email = {ph()} AND password = {ph()}',
              (email, password))
    row = c.fetchone(); conn.close()
    if row:
        return jsonify({"success": True, "slug": row[0], "name": row[1]})
    return jsonify({"success": False})


# ── SIGNUP ────────────────────────────────────────────────────────────────────

def send_welcome_email(hotel_name, slug, email):
    """Premium welcome email — no passwords, professional design."""
    try:
        resend.Emails.send({
            "from": "Favvi <hello@favvi.ai>",
            "to": email,
            "subject": "Welcome to Favvi",
            "html": f"""
<div style="background:#f0efe9;padding:40px 20px;font-family:Georgia,'Times New Roman',serif;">
  <div style="max-width:540px;margin:0 auto;background:#ffffff;border-top:3px solid #c9a84c;border-radius:4px;padding:44px 40px;">
    <div style="font-size:40px;color:#c9a84c;line-height:1;margin-bottom:20px;">&#10022;</div>
    <h1 style="font-size:32px;color:#1a1a2e;font-weight:600;margin:0 0 12px;letter-spacing:0.5px;">Welcome to Favvi</h1>
    <p style="font-size:15px;color:#555;line-height:1.7;margin:0 0 28px;">
      Your portal for <strong style="color:#1a1a2e;">{hotel_name}</strong> is ready, and your
      14-day free trial has begun. Your AI concierge is waiting to meet its first guests.</p>
    <a href="https://favvi.ai/portal/{slug}"
       style="display:inline-block;background:#1a1a2e;color:#f8f4ee;padding:15px 36px;
              text-decoration:none;border-radius:2px;font-family:Arial,sans-serif;
              font-size:13px;font-weight:bold;letter-spacing:0.5px;margin-bottom:8px;">Open Your Portal</a>
    <p style="font-size:12px;color:#999;font-family:Arial,sans-serif;margin:8px 0 32px;">
      Sign in with the email and password you chose at signup.</p>
    <div style="border-top:1px solid #ece9e3;padding-top:26px;">
      <p style="font-size:11px;font-weight:bold;letter-spacing:1.5px;text-transform:uppercase;
                color:#8a6e2a;font-family:Arial,sans-serif;margin:0 0 16px;">Your first steps</p>
      <table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif;">
        <tr><td style="padding:8px 12px 8px 0;vertical-align:top;color:#c9a84c;font-size:14px;font-weight:bold;">1.</td>
          <td style="padding:8px 0;font-size:13px;color:#555;line-height:1.6;">
            <strong style="color:#1a1a2e;">Tell the AI about your hotel</strong> — WiFi, breakfast
            times, facilities. Five minutes in Settings and your concierge knows your hotel.</td></tr>
        <tr><td style="padding:8px 12px 8px 0;vertical-align:top;color:#c9a84c;font-size:14px;font-weight:bold;">2.</td>
          <td style="padding:8px 0;font-size:13px;color:#555;line-height:1.6;">
            <strong style="color:#1a1a2e;">Put Favvi on your team's phones</strong> — staff get instant
            alerts when guests need something.
            <a href="https://favvi.ai/guide/notifications" style="color:#8a6e2a;">Setup guide&nbsp;&rarr;</a></td></tr>
        <tr><td style="padding:8px 12px 8px 0;vertical-align:top;color:#c9a84c;font-size:14px;font-weight:bold;">3.</td>
          <td style="padding:8px 0;font-size:13px;color:#555;line-height:1.6;">
            <strong style="color:#1a1a2e;">Print your QR cards</strong> — branded and ready in your
            portal. One on every desk and you're live.</td></tr>
      </table>
    </div>
    <p style="font-size:12px;color:#aaa;font-family:Arial,sans-serif;line-height:1.6;
              margin:32px 0 0;border-top:1px solid #ece9e3;padding-top:20px;">
      Questions? Just reply to this email — we read every one.<br>
      Favvi &middot; AI Concierge for Boutique Hotels</p>
  </div>
</div>"""
        })
    except Exception as e:
        print(f"Welcome email failed: {e}")


@app.route('/signup', methods=['POST'])
def signup():
    data       = request.json or {}
    hotel_name = data.get('name', '').strip()
    email      = data.get('email', '').strip()
    password   = data.get('password', '').strip()
    staff_pw   = data.get('staff_password', '').strip() or 'staff2024'
    manager_pw = data.get('manager_password', '').strip() or 'manager2024'

    if not hotel_name or not email or not password:
        return jsonify({"success": False, "error": "Please fill in all fields"})

    slug = re.sub(r'[^a-z0-9]+', '-', hotel_name.lower()).strip('-')

    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT id FROM hotels WHERE slug = {ph()} OR email = {ph()}', (slug, email))
    if c.fetchone():
        conn.close()
        return jsonify({"success": False, "error": "A hotel with this name or email already exists"})
    conn.close()

    ls_api_key    = os.environ.get("LEMONSQUEEZY_API_KEY", "")
    ls_store_id   = os.environ.get("LEMONSQUEEZY_STORE_ID", "")
    ls_variant_id = os.environ.get("LEMONSQUEEZY_VARIANT_ID", "")

    # ── Lemon Squeezy checkout (production) ──
    if ls_api_key and ls_store_id and ls_variant_id:
        payload = {
            "data": {
                "type": "checkouts",
                "attributes": {
                    "checkout_data": {
                        "email": email,
                        "custom": {
                            "hotel_name": hotel_name, "slug": slug, "email": email,
                            "password": password, "staff_password": staff_pw,
                            "manager_password": manager_pw
                        }
                    },
                    "product_options": {
                        "redirect_url": f"https://favvi.ai/signup-success?slug={slug}"
                    }
                },
                "relationships": {
                    "store":   {"data": {"type": "stores",   "id": str(ls_store_id)}},
                    "variant": {"data": {"type": "variants", "id": str(ls_variant_id)}}
                }
            }
        }
        try:
            req = urllib.request.Request(
                "https://api.lemonsqueezy.com/v1/checkouts",
                data=json.dumps(payload).encode(),
                headers={
                    "Accept": "application/vnd.api+json",
                    "Content-Type": "application/vnd.api+json",
                    "Authorization": f"Bearer {ls_api_key}"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode())
            return jsonify({"success": True, "checkout_url": body["data"]["attributes"]["url"]})
        except urllib.error.HTTPError as e:
            print(f"Lemon Squeezy HTTPError: {e.read().decode()[:500]}")
            return jsonify({"success": False, "error": "Could not start checkout — please try again"})
        except Exception as e:
            print(f"Lemon Squeezy checkout failed: {e}")
            return jsonify({"success": False, "error": "Could not start checkout — please try again"})

    # ── Fallback (no billing env): create hotel directly ──
    from datetime import timedelta
    conn = get_conn(); c = conn.cursor()
    if USE_POSTGRES:
        c.execute(f"""INSERT INTO hotels
                      (name, slug, email, password, system_prompt,
                       staff_password, manager_password, date_created,
                       trial_ends_at, subscription_status)
                      VALUES ({ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()})""",
                  (hotel_name, slug, email, password, '',
                   staff_pw, manager_pw, datetime.now().strftime("%Y-%m-%d %H:%M"),
                   datetime.now() + timedelta(days=14), 'on_trial'))
    else:
        c.execute(f"""INSERT INTO hotels
                      (name, slug, email, password, system_prompt,
                       staff_password, manager_password, date_created)
                      VALUES ({ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()})""",
                  (hotel_name, slug, email, password, '',
                   staff_pw, manager_pw, datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit(); conn.close()

    try:
        resend.Emails.send({
            "from": "Favvi <hello@favvi.ai>", "to": "hello@favvi.ai",
            "subject": f"New signup (direct) — {hotel_name}",
            "html": f"<p><strong>Hotel:</strong> {hotel_name}</p><p><strong>Email:</strong> {email}</p><p><strong>Slug:</strong> {slug}</p>"
        })
    except Exception: pass

    send_welcome_email(hotel_name, slug, email)
    return jsonify({"success": True, "checkout_url": f"/signup-success?slug={slug}"})


# ── SETTINGS ──────────────────────────────────────────────────────────────────

@app.route('/update-hotel-settings', methods=['POST'])
def update_hotel_settings():
    data = request.json or {}
    slug = data.get('slug')
    if not slug:
        return jsonify({"success": False, "error": "No slug provided"})

    # Require current manager password
    if not verify_hotel_password(slug, data.get('auth_password'), 'manager'):
        return jsonify({"success": False, "error": "Not authorised"}), 403

    allowed = ['name', 'manager_email', 'hotel_info', 'current_offers',
               'staff_password', 'manager_password', 'staff_knowledge', 'menu_notes']
    fields, values = [], []
    for field in allowed:
        if field in data:
            fields.append(f'{field} = {ph()}')
            values.append(data[field])

    if not fields:
        return jsonify({"success": False, "error": "Nothing to update"})

    values.append(slug)
    try:
        conn = get_conn(); c = conn.cursor()
        c.execute(f"UPDATE hotels SET {', '.join(fields)} WHERE slug = {ph()}", values)
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


MENU_TEXT_LIMIT = 12000  # characters of extracted PDF text kept for the AI


@app.route('/upload-menu/<slug>', methods=['POST'])
def upload_menu(slug):
    """Manager uploads a PDF menu. We extract its text (for the AI to read) and
    store the raw bytes (so guests can view the file). Auth via manager password."""
    auth = request.form.get('auth_password', '')
    if not verify_hotel_password(slug, auth, 'manager'):
        return jsonify({"success": False, "error": "Not authorised"}), 403

    if 'file' not in request.files:
        return jsonify({"success": False, "error": "No file provided"})
    f = request.files['file']
    if not f or not f.filename:
        return jsonify({"success": False, "error": "No file selected"})

    filename = f.filename
    if not filename.lower().endswith('.pdf'):
        return jsonify({"success": False, "error": "Please upload a PDF file"})

    raw = f.read()
    if len(raw) > 5 * 1024 * 1024:
        return jsonify({"success": False, "error": "File too large (max 5 MB)"})

    # Extract text for the AI
    extracted = ""
    truncated = False
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        extracted = "\n".join(parts).strip()
        if len(extracted) > MENU_TEXT_LIMIT:
            extracted = extracted[:MENU_TEXT_LIMIT]
            truncated = True
    except Exception as e:
        # Even if extraction fails, we can still store the file for viewing.
        extracted = ""

    pdf_b64 = base64.b64encode(raw).decode('ascii')

    try:
        conn = get_conn(); c = conn.cursor()
        c.execute(f'''UPDATE hotels SET menu_content = {ph()}, menu_filename = {ph()},
                      menu_pdf = {ph()} WHERE slug = {ph()}''',
                  (extracted, filename, pdf_b64, slug))
        conn.commit(); conn.close()
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

    return jsonify({
        "success": True,
        "filename": filename,
        "extracted_chars": len(extracted),
        "text_extracted": bool(extracted),
        "truncated": truncated
    })


@app.route('/delete-menu/<slug>', methods=['POST'])
def delete_menu(slug):
    data = request.json or {}
    if not verify_hotel_password(slug, data.get('auth_password'), 'manager'):
        return jsonify({"success": False, "error": "Not authorised"}), 403
    try:
        conn = get_conn(); c = conn.cursor()
        c.execute(f'''UPDATE hotels SET menu_content = NULL, menu_filename = NULL,
                      menu_pdf = NULL WHERE slug = {ph()}''', (slug,))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/menu/<slug>')
def view_menu(slug):
    """Serve the stored PDF so guests (and managers) can view it in the browser."""
    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT menu_filename, menu_pdf FROM hotels WHERE slug = {ph()}', (slug,))
    row = c.fetchone(); conn.close()
    if not row or not row[1]:
        return "No menu available", 404
    filename = row[0] or "menu.pdf"
    try:
        pdf_bytes = base64.b64decode(row[1])
    except Exception:
        return "Could not load menu", 500
    return Response(pdf_bytes, mimetype='application/pdf',
                    headers={'Content-Disposition': f'inline; filename="{filename}"'})


@app.route('/menu-info/<slug>')
def menu_info(slug):
    """Lightweight check used by the guest chat to know if a viewable menu exists."""
    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT menu_filename FROM hotels WHERE slug = {ph()}', (slug,))
    row = c.fetchone(); conn.close()
    has_menu = bool(row and row[0])
    return jsonify({"has_menu": has_menu, "filename": (row[0] if has_menu else None)})


# ── GUEST CHAT ────────────────────────────────────────────────────────────────

def get_system_prompt(slug=None):
    name           = "this hotel"
    hotel_info     = "No hotel information has been configured yet. Ask the manager to set this up in Settings."
    current_offers = ""
    menu_content   = ""
    menu_filename  = ""
    menu_notes     = ""

    if slug:
        conn = get_conn(); c = conn.cursor()
        c.execute(f'SELECT name, hotel_info, current_offers, menu_content, menu_filename, menu_notes FROM hotels WHERE slug = {ph()}', (slug,))
        row = c.fetchone(); conn.close()
        if row:
            name           = row[0] or "this hotel"
            hotel_info     = row[1] or hotel_info
            current_offers = row[2] or ""
            menu_content   = row[3] if len(row) > 3 and row[3] else ""
            menu_filename  = row[4] if len(row) > 4 and row[4] else ""
            menu_notes     = row[5] if len(row) > 5 and row[5] else ""

    offers_block = f"\nCURRENT OFFERS:\n{current_offers}\n" if current_offers else ""

    menu_block = ""
    if menu_notes:
        menu_block += f"\nADDITIONAL NOTES FROM THE HOTEL (for guests):\n{menu_notes}\n"
    if menu_content:
        menu_block += (
            f"\nMENU & SERVICES DOCUMENT:\n"
            f"The hotel has provided the following menu/services information. "
            f"Use it to answer guest questions about food, drinks, dining, and services. "
            f"If a guest wants to see the full document, tell them they can view it in the chat — "
            f"a link is shown to them automatically.\n\n{menu_content}\n"
        )

    return f"""You are the AI guest concierge for {name}.

HOTEL INFORMATION:
{hotel_info}
{offers_block}{menu_block}
When a guest makes a REAL REQUEST (towels, room service, maintenance, housekeeping, spa booking, transport):
1. Ask for their room number if you don't have it
2. Confirm their request warmly
3. Tell them the team has been notified
4. End with exactly: STAFF_ALERT: [DEPARTMENT] Room [number] - [request details]

[DEPARTMENT] must be exactly one of:
- HOUSEKEEPING — cleaning, towels, linens, tissues, toiletries, room amenities
- ROOMSERVICE  — food, drinks, dining orders
- MAINTENANCE  — anything broken or not working: AC, TV, wifi, plumbing, lights
- CONCIERGE    — taxis, airport transfers, transport, tours, restaurant bookings, local tips
- HEALTH       — spa, massage, gym, fitness, pool, sauna, wellness
- GENERAL      — anything that doesn't fit the above

For simple questions answer directly and helpfully.
If you don't have specific information, say so honestly and suggest contacting reception.
Always be warm, professional and friendly.
Reply in the same language the guest uses.
Never use markdown formatting like #, ##, **, or ---.
Use plain text only with line breaks for spacing."""


def classify_department(alert_details):
    """
    Parse the [DEPARTMENT] tag the AI writes.
    Falls back to keyword matching if the AI didn't follow format.
    """
    import re as _re
    m = _re.match(r'\[?(HOUSEKEEPING|ROOMSERVICE|MAINTENANCE|CONCIERGE|HEALTH|GENERAL)\]?\s*',
                  alert_details, _re.IGNORECASE)
    if m:
        dept = m.group(1).lower()
        rest = alert_details[m.end():].strip()
        return dept, rest

    # Keyword fallback
    low = ' ' + alert_details.lower() + ' '
    def has(*words):
        return any(_re.search(r'\b' + _re.escape(w) + r'\b', low) for w in words)

    if has('spa','massage','gym','fitness','sauna','pool','wellness','yoga'):
        return 'health', alert_details
    if has('towel','towels','sheet','linen','clean','housekeeping','tissue','toiletries','soap','pillow','blanket','shampoo'):
        return 'housekeeping', alert_details
    if has('food','drink','room service','sandwich','breakfast','lunch','dinner','coffee','tea','wine','water','meal'):
        return 'roomservice', alert_details
    if has('ac','aircon','a/c','heating','light','lights','tv','broken','leak','wifi','internet','shower','toilet','noise'):
        return 'maintenance', alert_details
    if has('taxi','transfer','airport','transport','tour','concierge','booking','reservation','recommend','car'):
        return 'concierge', alert_details
    return 'general', alert_details


@app.route('/chat', methods=['POST'])
def chat():
    data         = request.json or {}
    user_message = data.get('message', '')
    slug         = data.get('slug', '')
    history      = data.get('history', [])

    history.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        system=get_system_prompt(slug),
        messages=history
    )

    assistant_message = response.content[0].text
    history.append({"role": "assistant", "content": assistant_message})

    if "STAFF_ALERT:" in assistant_message:
        alert_line = next(
            (l for l in assistant_message.split('\n') if 'STAFF_ALERT:' in l), ''
        )
        raw_details  = alert_line.replace('STAFF_ALERT:', '').strip()
        department, alert_details = classify_department(raw_details)

        # Extract room number
        room = "N/A"
        for word in alert_details.split():
            if word.isdigit():
                room = word; break

        # Save request
        conn = get_conn(); c = conn.cursor()
        c.execute(f'''INSERT INTO requests
                      (room_number, department, details, status, date, hotel_slug)
                      VALUES ({ph()}, {ph()}, {ph()}, {ph()}, {ph()}, {ph()})''',
                  (room, department, alert_details, 'new',
                   datetime.now().strftime("%Y-%m-%d %H:%M"), slug))
        conn.commit(); conn.close()

        # Email manager
        email_to = "hello@favvi.ai"
        if slug:
            conn = get_conn(); c = conn.cursor()
            c.execute(f'SELECT manager_email, email FROM hotels WHERE slug = {ph()}', (slug,))
            row = c.fetchone(); conn.close()
            if row:
                email_to = row[0] or row[1] or email_to
        try:
            resend.Emails.send({
                "from": "Favvi Requests <requests@favvi.ai>",
                "to": email_to,
                "subject": f"New Request — Room {room}",
                "html": f"""<h2>New Guest Request</h2>
                    <p><strong>Department:</strong> {department.title()}</p>
                    <p><strong>Room:</strong> {room}</p>
                    <p><strong>Details:</strong> {alert_details}</p>"""
            })
        except Exception: pass

        # Push notification
        send_push_notifications(
            hotel_slug=slug,
            department=department,
            title=f"New {department.title()} Request",
            body=f"Room {room} — {alert_details[:80]}",
            url=f"/portal/{slug}/staff"
        )

        clean_message = assistant_message.replace(alert_line, '').strip()
        return jsonify({"response": clean_message, "history": history})

    return jsonify({"response": assistant_message, "history": history})


# ── STAFF CHAT ────────────────────────────────────────────────────────────────

@app.route('/staff-chat', methods=['POST'])
def staff_chat():
    data         = request.json or {}
    user_message = data.get('message', '')
    slug         = data.get('slug', '')

    hotel_name      = "this hotel"
    hotel_info      = "No hotel information has been configured yet."
    staff_knowledge = ""

    if slug:
        conn = get_conn(); c = conn.cursor()
        c.execute(f'SELECT name, hotel_info, staff_knowledge FROM hotels WHERE slug = {ph()}', (slug,))
        row = c.fetchone(); conn.close()
        if row:
            hotel_name      = row[0] or hotel_name
            hotel_info      = row[1] or hotel_info
            staff_knowledge = row[2] or ""

    kb = staff_knowledge or "No staff knowledge base set up yet. Ask your manager to add procedures in Settings."
    system = f"""You are an internal AI assistant for the staff of {hotel_name}.

HOTEL INFORMATION:
{hotel_info}

STAFF KNOWLEDGE BASE:
{kb}

Help hotel staff with: procedures, complaint handling, check-in, emergencies,
maintenance, upselling, and any hotel operations questions.
Be concise, practical and professional. You are talking to staff, not guests.
Use bullet points and clear steps when explaining procedures."""

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user_message}]
    )
    return jsonify({"response": response.content[0].text})


# ── FEEDBACK ──────────────────────────────────────────────────────────────────

@app.route('/submit-feedback', methods=['POST'])
def submit_feedback():
    data        = request.json or {}
    guest_name  = data.get('guest_name', 'Guest')
    room_number = data.get('room_number', 'N/A')
    overall     = data.get('overall', 0)
    cleanliness = data.get('cleanliness', 0)
    staff_score = data.get('staff', 0)
    dining      = data.get('dining', 0)
    wifi        = data.get('wifi', 0)
    comment     = data.get('comment', '')
    slug        = data.get('slug', '')
    date        = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = get_conn(); c = conn.cursor()
    c.execute(f'''INSERT INTO feedback
                  (guest_name, room_number, overall, cleanliness, staff,
                   dining, wifi, comment, date, hotel_slug)
                  VALUES ({ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()})''',
              (guest_name, room_number, overall, cleanliness, staff_score,
               dining, wifi, comment, date, slug))
    conn.commit(); conn.close()

    # Email manager
    email_to = "hello@favvi.ai"
    if slug:
        conn = get_conn(); c = conn.cursor()
        c.execute(f'SELECT manager_email, email FROM hotels WHERE slug = {ph()}', (slug,))
        row = c.fetchone(); conn.close()
        if row:
            email_to = row[0] or row[1] or email_to
    def emoji(s): return {1:"😞",2:"😐",3:"🙂",4:"😊",5:"🤩"}.get(s, "—")
    try:
        resend.Emails.send({
            "from": "Favvi Feedback <feedback@favvi.ai>",
            "to": email_to,
            "subject": f"New Feedback — Room {room_number}",
            "html": f"""<h2>New Guest Feedback</h2>
                <p><strong>Guest:</strong> {guest_name} &nbsp;|&nbsp; <strong>Room:</strong> {room_number}</p>
                <p><strong>Date:</strong> {date}</p><hr>
                <p>Overall: {emoji(overall)} {overall}/5</p>
                <p>Cleanliness: {emoji(cleanliness)} {cleanliness}/5</p>
                <p>Staff: {emoji(staff_score)} {staff_score}/5</p>
                <p>Dining: {emoji(dining)} {dining}/5</p>
                <p>WiFi: {emoji(wifi)} {wifi}/5</p><hr>
                <p><strong>Comment:</strong> {comment or 'No comment left'}</p>"""
        })
    except Exception: pass

    return jsonify({"success": True})


@app.route('/feedback-stats')
def feedback_stats():
    slug = request.args.get('slug', '')
    if not slug:
        return jsonify({"error": "Not authorised"}), 403
    if not verify_hotel_password(slug, request.args.get('pw', ''), 'manager'):
        return jsonify({"error": "Not authorised"}), 403

    conn = get_conn(); c = conn.cursor()
    c.execute(f'''SELECT AVG(overall), AVG(cleanliness), AVG(staff),
                         AVG(dining), AVG(wifi), COUNT(*)
                  FROM feedback WHERE hotel_slug = {ph()}''', (slug,))
    row = c.fetchone()
    c.execute(f'SELECT * FROM feedback WHERE hotel_slug = {ph()} ORDER BY date DESC LIMIT 10', (slug,))
    recent = c.fetchall(); conn.close()

    return jsonify({
        "averages": {
            "overall":         round(float(row[0] or 0), 1),
            "cleanliness":     round(float(row[1] or 0), 1),
            "staff":           round(float(row[2] or 0), 1),
            "dining":          round(float(row[3] or 0), 1),
            "wifi":            round(float(row[4] or 0), 1),
            "total_responses": row[5] or 0,
        },
        "recent": [list(r) for r in recent]
    })


# ── STAFF REQUESTS ────────────────────────────────────────────────────────────

@app.route('/get-requests')
def get_requests():
    slug = request.args.get('slug', '')
    if not verify_hotel_password(slug, request.args.get('pw', ''), 'staff'):
        return jsonify({"error": "Not authorised"}), 403

    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT * FROM requests WHERE hotel_slug = {ph()} ORDER BY date DESC', (slug,))
    rows = c.fetchall(); conn.close()
    return jsonify([{
        'id':         row[0],
        'room':       row[1],
        'department': row[2],
        'details':    row[3],
        'status':     row[4],
        'claimed_by': row[5],
        'date':       row[6],
    } for row in rows])


@app.route('/update-request', methods=['POST'])
def update_request():
    data       = request.json or {}
    slug       = data.get('slug', '')
    request_id = data.get('id')
    status     = data.get('status')
    claimed_by = data.get('claimed_by', '')

    if not verify_hotel_password(slug, data.get('password', ''), 'staff'):
        return jsonify({"success": False, "error": "Not authorised"}), 403

    conn = get_conn(); c = conn.cursor()
    c.execute(f'UPDATE requests SET status = {ph()}, claimed_by = {ph()} WHERE id = {ph()}',
              (status, claimed_by, request_id))
    conn.commit(); conn.close()
    return jsonify({"success": True})


# ── PUSH NOTIFICATIONS ────────────────────────────────────────────────────────

def send_push_notifications(hotel_slug, department, title, body, url):
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        print("VAPID keys missing — skipping push")
        return
    try:
        conn = get_conn(); c = conn.cursor()
        c.execute(f'''SELECT subscription FROM push_subscriptions
                      WHERE hotel_slug = {ph()} AND (department = {ph()} OR department = 'all')''',
                  (hotel_slug, department))
        rows = c.fetchall(); conn.close()
        for row in rows:
            try:
                webpush(
                    subscription_info=json.loads(row[0]),
                    data=json.dumps({"title": title, "body": body, "url": url,
                                     "tag": f"request-{hotel_slug}"}),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": VAPID_EMAIL}
                )
            except WebPushException as e:
                print(f"WebPushException: {e}")
            except Exception as e:
                print(f"Push error: {e}")
    except Exception as e:
        print(f"Push notification error: {e}")


@app.route('/vapid-public-key')
def vapid_public_key():
    return jsonify({"key": VAPID_PUBLIC_KEY})


@app.route('/push-subscribe', methods=['POST'])
def push_subscribe():
    data       = request.json or {}
    slug       = data.get('slug')
    name       = data.get('name')
    department = data.get('department')
    sub        = data.get('subscription')
    if not all([slug, name, department, sub]):
        return jsonify({"success": False, "error": "Missing fields"})
    conn = get_conn(); c = conn.cursor()
    # Replace any existing subscription for this staff member
    c.execute(f'DELETE FROM push_subscriptions WHERE hotel_slug = {ph()} AND staff_name = {ph()}',
              (slug, name))
    c.execute(f'''INSERT INTO push_subscriptions
                  (hotel_slug, staff_name, department, subscription, created_at)
                  VALUES ({ph()},{ph()},{ph()},{ph()},{ph()})''',
              (slug, name, department, json.dumps(sub),
               datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit(); conn.close()
    return jsonify({"success": True})


@app.route('/push-unsubscribe', methods=['POST'])
def push_unsubscribe():
    data = request.json or {}
    slug = data.get('slug')
    name = data.get('name')
    conn = get_conn(); c = conn.cursor()
    c.execute(f'DELETE FROM push_subscriptions WHERE hotel_slug = {ph()} AND staff_name = {ph()}',
              (slug, name))
    conn.commit(); conn.close()
    return jsonify({"success": True})


# ── CONTACT FORM ──────────────────────────────────────────────────────────────

@app.route('/contact', methods=['POST'])
def contact():
    import html as _html
    data    = request.json or {}
    name    = data.get('name', '').strip()
    hotel   = data.get('hotel', '').strip()
    email   = data.get('email', '').strip()
    message = data.get('message', '').strip()
    if not name or not email or not message:
        return jsonify({"success": False, "error": "All fields required"})
    # Escape user input so it renders safely in the email
    s_name    = _html.escape(name)
    s_hotel   = _html.escape(hotel) if hotel else "—"
    s_email   = _html.escape(email)
    s_message = _html.escape(message).replace("\n", "<br>")
    try:
        resend.Emails.send({
            "from": "Favvi Contact <hello@favvi.ai>",
            "to": "hello@favvi.ai",
            "reply_to": email,
            "subject": f"Contact form — {name}" + (f" ({hotel})" if hotel else ""),
            "html": (
                f"<p><strong>Name:</strong> {s_name}</p>"
                f"<p><strong>Hotel / property:</strong> {s_hotel}</p>"
                f"<p><strong>Email:</strong> {s_email}</p>"
                f"<p><strong>Message:</strong><br>{s_message}</p>"
            )
        })
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/request-cancellation', methods=['POST'])
def request_cancellation():
    """Pragmatic cancel flow: notify us so we action it manually.
    (Swap to a self-serve Lemon Squeezy / Stripe link later — the IDs are stored.)"""
    import html as _html
    data   = request.json or {}
    slug   = data.get('slug', '').strip()
    reason = data.get('reason', '').strip()
    if not slug:
        return jsonify({"success": False, "error": "Missing hotel"})

    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT name, email, lemon_subscription_id FROM hotels WHERE slug = {ph()}', (slug,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({"success": False, "error": "Hotel not found"})

    name   = row[0] or slug
    email  = row[1] or "(no email on file)"
    sub_id = (row[2] if len(row) > 2 else "") or "(not stored)"

    try:
        resend.Emails.send({
            "from": "Favvi <hello@favvi.ai>",
            "to": "hello@favvi.ai",
            "reply_to": email if "@" in email else "hello@favvi.ai",
            "subject": f"Cancellation request — {name}",
            "html": (
                f"<p><strong>Hotel:</strong> {_html.escape(name)}</p>"
                f"<p><strong>Slug:</strong> {_html.escape(slug)}</p>"
                f"<p><strong>Email:</strong> {_html.escape(email)}</p>"
                f"<p><strong>Lemon subscription ID:</strong> {_html.escape(str(sub_id))}</p>"
                f"<p><strong>Reason:</strong><br>{_html.escape(reason) if reason else '—'}</p>"
            )
        })
        # Confirmation to the hotel, if we have a real address.
        if "@" in email:
            resend.Emails.send({
                "from": "Favvi <hello@favvi.ai>",
                "to": email,
                "subject": "We've received your cancellation request",
                "html": (
                    f"<p>Hi,</p>"
                    f"<p>We've received your request to cancel Favvi for <strong>{_html.escape(name)}</strong>. "
                    f"A member of our team will confirm the cancellation shortly — there's nothing further you need to do.</p>"
                    f"<p>If this was a mistake, just reply to this email and we'll keep things running.</p>"
                    f"<p>— The Favvi team</p>"
                )
            })
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/send-trial-warnings', methods=['GET', 'POST'])
def send_trial_warnings():
    if request.args.get('secret', '') != CRON_SECRET:
        return jsonify({"error": "Unauthorised"}), 403

    conn = get_conn(); c = conn.cursor()
    c.execute("""SELECT slug, name, email, trial_ends_at FROM hotels
                 WHERE trial_ends_at IS NOT NULL""")
    hotels = c.fetchall(); conn.close()

    sent  = []
    today = datetime.now().date()

    for slug, name, email, trial_ends_at in hotels:
        if not email or not trial_ends_at:
            continue
        try:
            ends = trial_ends_at.date() if hasattr(trial_ends_at, 'date') else datetime.strptime(str(trial_ends_at)[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        days_left = (ends - today).days

        if days_left == 7:
            subject  = "Your Favvi trial ends in 7 days"
            headline = "One week left in your trial"
            body_txt = f"Your free trial of Favvi for {name} ends in 7 days. After that, your subscription begins — nothing to do if you'd like to continue."
        elif days_left == 1:
            subject  = "Your Favvi trial ends tomorrow"
            headline = "Your trial ends tomorrow"
            body_txt = f"Your free trial of Favvi for {name} ends tomorrow. We hope your guests have been loving it!"
        else:
            continue

        html = f"""
        <div style="font-family:Georgia,serif;max-width:540px;margin:0 auto;padding:40px 20px;">
            <div style="color:#c9a84c;font-size:24px;margin-bottom:16px;">&#10022;</div>
            <h1 style="font-size:26px;color:#1a1a2e;font-weight:600;margin-bottom:14px;">{headline}</h1>
            <p style="font-size:15px;color:#444;line-height:1.7;margin-bottom:14px;">{body_txt}</p>
            <p style="font-size:15px;color:#444;line-height:1.7;margin-bottom:24px;">
                Questions or want to make changes? Just reply — we read everything.</p>
            <a href="https://favvi.ai/portal/{slug}"
               style="display:inline-block;background:#1a1a2e;color:#f8f4ee;
                      padding:13px 28px;text-decoration:none;font-size:14px;font-family:Arial,sans-serif;">
               Open your portal</a>
            <p style="font-size:12px;color:#999;margin-top:36px;">
               Favvi — AI Concierge for Boutique Hotels<br>hello@favvi.ai</p>
        </div>"""

        try:
            resend.Emails.send({
                "from": "Favvi <hello@favvi.ai>",
                "to": email,
                "subject": subject,
                "html": html
            })
            sent.append({"slug": slug, "days_left": days_left})
        except Exception as e:
            print(f"Trial warning failed for {slug}: {e}")

    return jsonify({"sent": sent, "checked": len(hotels)})


# ── PASSWORD RESET ────────────────────────────────────────────────────────────

@app.route('/reset-password')
def reset_password_page():
    return send_from_directory('.', 'reset.html')


@app.route('/request-password-reset', methods=['POST'])
def request_password_reset():
    email = (request.json or {}).get('email', '').strip().lower()
    # Always respond identically — never reveal whether an email exists
    generic = jsonify({"success": True})
    if not email:
        return generic

    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT slug, name FROM hotels WHERE LOWER(email) = {ph()}', (email,))
    row = c.fetchone(); conn.close()
    if not row:
        time.sleep(0.4)
        return generic

    slug, hotel_name = row[0], row[1]
    token   = secrets.token_urlsafe(32)
    expires = (datetime.now() + __import__('datetime').timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_conn(); c = conn.cursor()
    c.execute(f"""INSERT INTO password_resets (email, slug, token, expires_at, used)
                  VALUES ({ph()}, {ph()}, {ph()}, {ph()}, 0)""",
              (email, slug, token, expires))
    conn.commit(); conn.close()

    reset_link = f"https://favvi.ai/reset-password?token={token}"
    try:
        resend.Emails.send({
            "from": "Favvi <hello@favvi.ai>",
            "to": email,
            "subject": "Reset your Favvi passwords",
            "html": f"""
            <div style="font-family:Georgia,serif;max-width:540px;margin:0 auto;padding:40px 20px;">
                <div style="color:#c9a84c;font-size:24px;margin-bottom:16px;">&#10022;</div>
                <h1 style="font-size:26px;color:#1a1a2e;font-weight:600;margin-bottom:14px;">Reset your passwords</h1>
                <p style="font-size:15px;color:#444;line-height:1.7;margin-bottom:24px;">
                    We received a request to reset the passwords for <strong>{hotel_name}</strong>.
                    The link below is valid for one hour. If you didn't request this, you can safely ignore this email.</p>
                <a href="{reset_link}"
                   style="display:inline-block;background:#1a1a2e;color:#f8f4ee;
                          padding:13px 28px;text-decoration:none;font-size:14px;font-family:Arial,sans-serif;">
                   Choose new passwords</a>
                <p style="font-size:12px;color:#999;margin-top:36px;">Favvi — AI Concierge for Boutique Hotels<br>hello@favvi.ai</p>
            </div>"""
        })
    except Exception as e:
        print(f"Reset email failed: {e}")
    return generic


@app.route('/reset-password', methods=['POST'])
def reset_password_submit():
    data       = request.json or {}
    token      = data.get('token', '')
    new_pw     = data.get('password', '').strip()
    staff_pw   = data.get('staff_password', '').strip()
    manager_pw = data.get('manager_password', '').strip()

    if not token or not new_pw:
        return jsonify({"success": False, "error": "Missing token or password"})
    if len(new_pw) < 6:
        return jsonify({"success": False, "error": "Password must be at least 6 characters"})

    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT email, slug, expires_at, used FROM password_resets WHERE token = {ph()}', (token,))
    row = c.fetchone(); conn.close()

    if not row:
        time.sleep(0.4)
        return jsonify({"success": False, "error": "Invalid or expired link"})
    email, slug, expires_at, used = row

    if used:
        return jsonify({"success": False, "error": "This link has already been used"})
    try:
        if datetime.now() > datetime.strptime(str(expires_at)[:19], "%Y-%m-%d %H:%M:%S"):
            return jsonify({"success": False, "error": "This link has expired — request a new one"})
    except Exception:
        return jsonify({"success": False, "error": "Invalid link"})

    fields = [f'password = {ph()}']; values = [new_pw]
    if staff_pw:
        fields.append(f'staff_password = {ph()}');   values.append(staff_pw)
    if manager_pw:
        fields.append(f'manager_password = {ph()}'); values.append(manager_pw)
    values.append(slug)

    conn = get_conn(); c = conn.cursor()
    c.execute(f"UPDATE hotels SET {', '.join(fields)} WHERE slug = {ph()}", values)
    c.execute(f'UPDATE password_resets SET used = 1 WHERE token = {ph()}', (token,))
    conn.commit(); conn.close()
    return jsonify({"success": True, "slug": slug})


# ── ADMIN PANEL ───────────────────────────────────────────────────────────────

@app.route('/admin')
def admin_page():
    return send_from_directory('.', 'admin.html')


@app.route('/admin-data')
def admin_data():
    if not is_admin():
        return jsonify({"error": "Unauthorised"}), 403
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute('''SELECT id, name, slug, email, date_created,
                            trial_ends_at, subscription_status
                     FROM hotels ORDER BY id DESC''')
        rows = c.fetchall()
    except Exception:
        # Older SQLite schema without trial columns
        c.execute('SELECT id, name, slug, email, date_created FROM hotels ORDER BY id DESC')
        rows = [(r[0], r[1], r[2], r[3], r[4], None, None) for r in c.fetchall()]
    conn.close()

    import datetime as _dt
    hotels = []
    for r in rows:
        trial_ends = r[5]
        days_remaining = None
        if trial_ends:
            try:
                if isinstance(trial_ends, str):
                    trial_ends = _dt.datetime.strptime(trial_ends[:19], "%Y-%m-%d %H:%M:%S")
                days_remaining = (trial_ends.date() - _dt.date.today()).days
            except Exception:
                pass
        hotels.append({
            "id": r[0], "name": r[1], "slug": r[2], "email": r[3],
            "date_created": r[4],
            "trial_ends_at": str(r[5]) if r[5] else None,
            "subscription_status": r[6],
            "days_remaining": days_remaining,
        })
    return jsonify({"hotels": hotels})

@app.route('/admin-extend-trial', methods=['POST'])
def admin_extend_trial():
    if not is_admin():
        return jsonify({"error": "Unauthorised"}), 403
    data = request.get_json(silent=True) or {}
    slug = data.get('slug')
    days = int(data.get('days', 7))
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute(f'SELECT trial_ends_at FROM hotels WHERE slug = {ph()}', (slug,))
        row = c.fetchone()
    except Exception:
        row = None
    conn.close()
    if row is None:
        from datetime import timedelta
        new_end = datetime.now() + timedelta(days=days)
        conn2 = get_conn(); c2 = conn2.cursor()
        try:
            c2.execute(f'UPDATE hotels SET trial_ends_at = {ph()} WHERE slug = {ph()}', (new_end, slug))
            conn2.commit()
        except Exception: pass
        conn2.close()
        return jsonify({"success": True, "new_trial_ends_at": str(new_end)})
    from datetime import timedelta
    current = row[0] if row[0] else datetime.now()
    if hasattr(current, 'date'):
        new_end = current + timedelta(days=days)
    else:
        new_end = datetime.now() + timedelta(days=days)
    conn = get_conn(); c = conn.cursor()
    c.execute(f'UPDATE hotels SET trial_ends_at = {ph()} WHERE slug = {ph()}', (new_end, slug))
    conn.commit(); conn.close()
    return jsonify({"success": True, "new_trial_ends_at": str(new_end)})


@app.route('/admin-delete-hotel', methods=['POST'])
def admin_delete_hotel():
    if not is_admin():
        return jsonify({"error": "Unauthorised"}), 403
    slug = (request.get_json(silent=True) or {}).get('slug')
    if not slug:
        return jsonify({"success": False, "error": "No slug"})
    conn = get_conn(); c = conn.cursor()
    for table in ['requests', 'feedback', 'push_subscriptions']:
        c.execute(f'DELETE FROM {table} WHERE hotel_slug = {ph()}', (slug,))
    c.execute(f'DELETE FROM hotels WHERE slug = {ph()}', (slug,))
    conn.commit(); conn.close()
    return jsonify({"success": True})


# ── LEMON SQUEEZY WEBHOOK ─────────────────────────────────────────────────────

@app.route('/lemon-webhook', methods=['POST'])
def lemon_webhook():
    import hmac, hashlib
    secret    = os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET", "")
    signature = request.headers.get("X-Signature", "")
    raw_body  = request.get_data()

    if secret:
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return jsonify({"error": "Invalid signature"}), 401

    data       = request.json or {}
    event_name = data.get("meta", {}).get("event_name", "")
    attrs      = data.get("data", {}).get("attributes", {})
    custom     = data.get("meta", {}).get("custom_data", {})

    if event_name in ("order_created", "subscription_created"):
        hotel_name = custom.get("hotel_name", "").strip()
        slug       = custom.get("slug", "").strip()
        email      = attrs.get("user_email") or custom.get("email", "")
        password   = custom.get("password", "")
        staff_pw   = custom.get("staff_password", "staff2024")
        manager_pw = custom.get("manager_password", "manager2024")

        if not slug or not hotel_name:
            return jsonify({"error": "Missing hotel data"}), 400

        from datetime import timedelta
        trial_end = datetime.now() + timedelta(days=14)

        # Lemon Squeezy IDs — stored now so self-serve cancel/manage links are
        # trivial to switch on later (especially once on live mode / Stripe).
        lemon_sub_id  = str(data.get("data", {}).get("id", "") or "")
        lemon_cust_id = str(attrs.get("customer_id", "") or "")

        conn = get_conn(); c = conn.cursor()
        c.execute(f'SELECT id FROM hotels WHERE slug = {ph()} OR email = {ph()}', (slug, email))
        newly_created = False
        if not c.fetchone():
            if USE_POSTGRES:
                c.execute(f'''INSERT INTO hotels
                              (name, slug, email, password, system_prompt,
                               staff_password, manager_password, date_created,
                               trial_ends_at, subscription_status,
                               lemon_customer_id, lemon_subscription_id)
                              VALUES ({ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()})''',
                          (hotel_name, slug, email, password, '',
                           staff_pw, manager_pw, datetime.now().strftime("%Y-%m-%d %H:%M"),
                           trial_end, 'on_trial', lemon_cust_id, lemon_sub_id))
            else:
                c.execute(f'''INSERT INTO hotels
                              (name, slug, email, password, system_prompt,
                               staff_password, manager_password, date_created)
                              VALUES ({ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()})''',
                          (hotel_name, slug, email, password, '',
                           staff_pw, manager_pw, datetime.now().strftime("%Y-%m-%d %H:%M")))
            conn.commit()
            newly_created = True
        else:
            # Existing hotel re-subscribing — update their Lemon IDs if we have them.
            if USE_POSTGRES and (lemon_sub_id or lemon_cust_id):
                c.execute(f'''UPDATE hotels SET lemon_customer_id = {ph()},
                              lemon_subscription_id = {ph()} WHERE slug = {ph()}''',
                          (lemon_cust_id, lemon_sub_id, slug))
                conn.commit()
        conn.close()
        if newly_created and email:
            send_welcome_email(hotel_name, slug, email)

        try:
            resend.Emails.send({
                "from": "Favvi <hello@favvi.ai>",
                "to": "hello@favvi.ai",
                "subject": f"New signup via Lemon Squeezy — {hotel_name}",
                "html": f"<p><strong>Hotel:</strong> {hotel_name}</p><p><strong>Slug:</strong> {slug}</p><p><strong>Email:</strong> {email}</p>"
            })
        except Exception: pass

    return jsonify({"success": True})


# ── RUN ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)