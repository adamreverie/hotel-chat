import os
import re
import json
import time
import secrets
import resend
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
import anthropic
from pywebpush import webpush, WebPushException

try:
    import psycopg2
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

# ── APP & CLIENTS ─────────────────────────────────────────────────────────────

app    = Flask(__name__)
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
                     lemon_customer_id TEXT, lemon_subscription_id TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions (
                     id SERIAL PRIMARY KEY,
                     hotel_slug TEXT, staff_name TEXT, department TEXT,
                     subscription TEXT, created_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS password_resets (
                     id SERIAL PRIMARY KEY,
                     email TEXT, slug TEXT, token TEXT UNIQUE,
                     expires_at TEXT, used INTEGER DEFAULT 0)''')
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
                     staff_knowledge TEXT)''')
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
    """Check admin credentials from body or query string."""
    # Support both old field name (admin_password) and what admin.html actually sends (password)
    body     = request.json or {}
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
    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT id FROM hotels WHERE slug = {ph()}', (slug,))
    if not c.fetchone():
        conn.close(); return "Hotel not found", 404
    conn.close()
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
        if sub_status == 'on_trial' and trial_ends and datetime.now() > trial_ends:
            return jsonify({"error": "Trial expired"}), 403

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

    c.execute(f'''INSERT INTO hotels
                  (name, slug, email, password, system_prompt,
                   staff_password, manager_password, date_created)
                  VALUES ({ph()}, {ph()}, {ph()}, {ph()}, {ph()}, {ph()}, {ph()}, {ph()})''',
              (hotel_name, slug, email, password, '',
               staff_pw, manager_pw, datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit(); conn.close()

    # Notify us
    try:
        resend.Emails.send({
            "from": "Favvi <hello@favvi.ai>",
            "to": "hello@favvi.ai",
            "subject": f"New signup — {hotel_name}",
            "html": f"""<h2>New Hotel Signed Up</h2>
                <p><strong>Hotel:</strong> {hotel_name}</p>
                <p><strong>Email:</strong> {email}</p>
                <p><strong>Slug:</strong> {slug}</p>
                <p><strong>Time:</strong> {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>"""
        })
    except Exception: pass

    # Welcome the hotel
    try:
        resend.Emails.send({
            "from": "Favvi <hello@favvi.ai>",
            "to": email,
            "subject": f"Welcome to Favvi — {hotel_name} is live!",
            "html": f"""
            <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;">
                <h2 style="color:#1a1a2e;">Welcome to Favvi</h2>
                <p>Your hotel portal is ready. Here are your details:</p>
                <div style="background:#f5f5f5;padding:20px;border-radius:8px;margin:20px 0;">
                    <p><strong>Hotel:</strong> {hotel_name}</p>
                    <p><strong>Portal:</strong> https://favvi.ai/portal/{slug}</p>
                    <p><strong>Email:</strong> {email}</p>
                    <p><strong>Password:</strong> {password}</p>
                    <p><strong>Staff Password:</strong> {staff_pw}</p>
                    <p><strong>Manager Password:</strong> {manager_pw}</p>
                </div>
                <h3>Getting Started</h3>
                <ol>
                    <li>Visit your portal and open <strong>Settings</strong></li>
                    <li>Add your hotel info, facilities and room details</li>
                    <li>Share the QR code from <strong>Links & QR</strong> in guest rooms</li>
                    <li>Staff log in at <strong>favvi.ai/portal/{slug}/staff</strong></li>
                </ol>
                <a href="https://favvi.ai/portal/{slug}"
                   style="display:inline-block;background:#1a1a2e;color:white;
                          padding:12px 28px;text-decoration:none;border-radius:4px;margin-top:16px;">
                    Open Your Portal</a>
            </div>"""
        })
    except Exception: pass

    return jsonify({"success": True, "slug": slug})


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
               'staff_password', 'manager_password', 'staff_knowledge']
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


# ── GUEST CHAT ────────────────────────────────────────────────────────────────

def get_system_prompt(slug=None):
    name           = "this hotel"
    hotel_info     = "No hotel information has been configured yet. Ask the manager to set this up in Settings."
    current_offers = ""

    if slug:
        conn = get_conn(); c = conn.cursor()
        c.execute(f'SELECT name, hotel_info, current_offers FROM hotels WHERE slug = {ph()}', (slug,))
        row = c.fetchone(); conn.close()
        if row:
            name           = row[0] or "this hotel"
            hotel_info     = row[1] or hotel_info
            current_offers = row[2] or ""

    offers_block = f"\nCURRENT OFFERS:\n{current_offers}\n" if current_offers else ""

    return f"""You are the AI guest concierge for {name}.

HOTEL INFORMATION:
{hotel_info}
{offers_block}
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
    data    = request.json or {}
    name    = data.get('name', '').strip()
    email   = data.get('email', '').strip()
    message = data.get('message', '').strip()
    if not name or not email or not message:
        return jsonify({"success": False, "error": "All fields required"})
    try:
        resend.Emails.send({
            "from": "Favvi Contact <hello@favvi.ai>",
            "to": "hello@favvi.ai",
            "reply_to": email,
            "subject": f"Contact form — {name}",
            "html": f"<p><strong>Name:</strong> {name}</p><p><strong>Email:</strong> {email}</p><p><strong>Message:</strong><br>{message}</p>"
        })
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ── TRIAL EXPIRY WARNINGS ─────────────────────────────────────────────────────

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
    c.execute('''SELECT id, name, slug, email, date_created,
                        trial_ends_at, subscription_status
                 FROM hotels ORDER BY id DESC''')
    rows = c.fetchall(); conn.close()
    return jsonify([{
        "id": r[0], "name": r[1], "slug": r[2],
        "email": r[3], "date_created": r[4],
        "trial_ends_at": str(r[5]) if r[5] else None,
        "subscription_status": r[6],
    } for r in rows])


@app.route('/admin-extend-trial', methods=['POST'])
def admin_extend_trial():
    if not is_admin():
        return jsonify({"error": "Unauthorised"}), 403
    data = request.json or {}
    slug = data.get('slug')
    days = int(data.get('days', 7))
    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT trial_ends_at FROM hotels WHERE slug = {ph()}', (slug,))
    row = c.fetchone(); conn.close()
    if not row:
        return jsonify({"success": False, "error": "Hotel not found"})
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
    slug = (request.json or {}).get('slug')
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

        conn = get_conn(); c = conn.cursor()
        c.execute(f'SELECT id FROM hotels WHERE slug = {ph()} OR email = {ph()}', (slug, email))
        if not c.fetchone():
            c.execute(f'''INSERT INTO hotels
                          (name, slug, email, password, system_prompt,
                           staff_password, manager_password, date_created,
                           trial_ends_at, subscription_status)
                          VALUES ({ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()},{ph()})''',
                      (hotel_name, slug, email, password, '',
                       staff_pw, manager_pw, datetime.now().strftime("%Y-%m-%d %H:%M"),
                       trial_end, 'on_trial'))
            conn.commit()
        conn.close()

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