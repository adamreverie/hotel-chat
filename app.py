HOTEL_NAME     = "Favvi Hotel"
HOTEL_LOCATION = ""
MANAGER_EMAIL  = "hello@favvi.ai"
STAFF_PASSWORD = "staff2024"
MANAGER_PASSWORD = "manager2024"
HOTEL_INFO     = ""
CURRENT_OFFERS = ""
import os
import resend
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
import anthropic

try:
    import psycopg2
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
resend.api_key = os.environ.get("RESEND_API_KEY")

DATABASE_URL = os.environ.get("DATABASE_URL")
USE_POSTGRES = bool(DATABASE_URL and HAS_POSTGRES)

def get_conn():
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect('feedback.db')

def placeholder():
    return "%s" if USE_POSTGRES else "?"


def init_db():
    conn = get_conn()
    c = conn.cursor()

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
                     staff_knowledge TEXT)''')
    else:
        c.execute('''CREATE TABLE IF NOT EXISTS feedback
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     guest_name TEXT, room_number TEXT,
                     overall INTEGER, cleanliness INTEGER, staff INTEGER,
                     dining INTEGER, wifi INTEGER,
                     comment TEXT, date TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS requests
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     room_number TEXT, department TEXT, details TEXT,
                     status TEXT DEFAULT 'new', claimed_by TEXT, date TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS hotels
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     name TEXT, slug TEXT UNIQUE, email TEXT, password TEXT,
                     system_prompt TEXT, staff_password TEXT, manager_password TEXT,
                     date_created TEXT)''')
        for col_sql in [
            "ALTER TABLE hotels ADD COLUMN hotel_info TEXT",
            "ALTER TABLE hotels ADD COLUMN current_offers TEXT",
            "ALTER TABLE hotels ADD COLUMN manager_email TEXT",
            "ALTER TABLE hotels ADD COLUMN staff_knowledge TEXT",
        ]:
            try: c.execute(col_sql)
            except Exception: pass

    conn.commit()
    conn.close()

init_db()


def get_system_prompt(slug=None):
    name           = "this hotel"
    hotel_info     = "No hotel information has been configured yet."
    current_offers = ""

    if slug:
        conn = get_conn(); c = conn.cursor()
        c.execute(f'SELECT * FROM hotels WHERE slug = {placeholder()}', (slug,))
        hotel = c.fetchone(); conn.close()
        if hotel:
            name           = hotel[1] or "this hotel"
            hotel_info     = hotel[9]  if len(hotel) > 9  and hotel[9]  else "No hotel information has been configured yet. Please ask the manager to set this up in Settings."
            current_offers = hotel[10] if len(hotel) > 10 and hotel[10] else ""

    return f"""You are the AI guest concierge for {name}.

{hotel_info}

{current_offers}

When a guest makes a REAL REQUEST (towels, room service, maintenance, housekeeping, spa booking):
1. Ask for their room number if you don't have it
2. Confirm their request warmly
3. Tell them the team has been notified
4. End with exactly: STAFF_ALERT: Room [number] - [request details]

For simple questions answer directly and helpfully.
If you don't have specific information about something, say so honestly and suggest the guest contact reception.
Always be warm, professional and friendly. Use occasional emojis.
Reply in the same language the guest uses.
Never use markdown formatting like #, ##, **, or ---.
Use plain text only with line breaks for spacing."""


@app.route('/')
def home(): return send_from_directory('.', 'landing.html')

@app.route('/signup')
def signup_page(): return send_from_directory('.', 'signup.html')

@app.route('/login')
def login_page(): return send_from_directory('.', 'login.html')

@app.route('/staff')
def staff(): return send_from_directory('.', 'staff.html')

@app.route('/feedback')
def feedback(): return send_from_directory('.', 'feedback.html')

@app.route('/dashboard')
def dashboard(): return send_from_directory('.', 'dashboard.html')

@app.route('/portal/<slug>')
def portal(slug):
    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT * FROM hotels WHERE slug = {placeholder()}', (slug,))
    hotel = c.fetchone(); conn.close()
    if not hotel: return "Hotel not found", 404
    return send_from_directory('.', 'portal.html')

@app.route('/portal/<slug>/chat')
def portal_chat(slug): return send_from_directory('.', 'index.html')

@app.route('/portal/<slug>/staff')
def portal_staff(slug): return send_from_directory('.', 'staff.html')

@app.route('/portal/<slug>/feedback')
def portal_feedback(slug): return send_from_directory('.', 'feedback.html')

@app.route('/portal/<slug>/dashboard')
def portal_dashboard(slug): return send_from_directory('.', 'dashboard.html')

@app.route('/portal/<slug>/settings')
def portal_settings(slug): return send_from_directory('.', 'settings.html')

@app.route('/portal/<slug>/staffchat')
def portal_staffchat(slug): return send_from_directory('.', 'staffchat.html')

@app.route('/portal/<slug>/links')
def portal_links(slug): return send_from_directory('.', 'links.html')

@app.route('/chat', methods=['POST'])
def chat():
    data         = request.json
    user_message = data.get('message')
    slug         = data.get('slug')
    history      = data.get('history', [])

    history.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=get_system_prompt(slug),
        messages=history
    )

    assistant_message = response.content[0].text
    history.append({"role": "assistant", "content": assistant_message})

    if "STAFF_ALERT:" in assistant_message:
        alert_line    = [line for line in assistant_message.split('\n') if 'STAFF_ALERT:' in line][0]
        alert_details = alert_line.replace('STAFF_ALERT:', '').strip()

        department = "general"
        if any(w in alert_details.lower() for w in ["towel","sheet","clean","housekeeping","linen"]):
            department = "housekeeping"
        elif any(w in alert_details.lower() for w in ["food","drink","room service","sandwich","breakfast","dinner","lunch","water"]):
            department = "roomservice"
        elif any(w in alert_details.lower() for w in ["ac","air","light","tv","broken","leak","maintenance","wifi","internet"]):
            department = "maintenance"
        elif any(w in alert_details.lower() for w in ["taxi","transfer","transport","tour","concierge","recommend"]):
            department = "concierge"

        room = "N/A"
        for word in alert_details.split():
            if word.isdigit(): room = word; break

        ph = placeholder()
        conn = get_conn(); c = conn.cursor()
        c.execute(f'''INSERT INTO requests (room_number, department, details, status, date, hotel_slug)
             VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})''',
          (room, department, alert_details, 'new',
           datetime.now().strftime("%Y-%m-%d %H:%M"), slug))
        conn.commit(); conn.close()

        email_to = MANAGER_EMAIL
        if slug:
            conn = get_conn(); c = conn.cursor()
            c.execute(f'SELECT manager_email FROM hotels WHERE slug = {ph}', (slug,))
            row = c.fetchone(); conn.close()
            if row and row[0]: email_to = row[0]

        resend.Emails.send({
            "from": "requests@favvi.ai",
            "to": email_to,
            "subject": f"🛎️ New Request — Room {room}",
            "html": f"<h2>New Guest Request</h2><p><strong>Department:</strong> {department.title()}</p><p><strong>Details:</strong> {alert_details}</p>"
        })

        clean_message = assistant_message.replace(alert_line, '').strip()
        return jsonify({"response": clean_message, "history": history})

    return jsonify({"response": assistant_message, "history": history})


@app.route('/staff-chat', methods=['POST'])
def staff_chat():
    data         = request.json
    user_message = data.get('message')
    slug         = data.get('slug')

    hotel_info      = "No hotel information has been configured yet."
    hotel_name      = "this hotel"
    staff_knowledge = ""

    if slug:
        conn = get_conn(); c = conn.cursor()
        c.execute(f'SELECT name, hotel_info, staff_knowledge FROM hotels WHERE slug = {placeholder()}', (slug,))
        row = c.fetchone(); conn.close()
        if row:
            hotel_name      = row[0] or "this hotel"
            hotel_info      = row[1] or "No hotel information has been configured yet."
            staff_knowledge = row[2] or ""

    staff_prompt = f"""You are an internal AI assistant for the staff of {hotel_name}.

HOTEL INFORMATION:
{hotel_info}

STAFF KNOWLEDGE BASE:
{staff_knowledge if staff_knowledge else "No staff knowledge base has been set up yet. Ask your manager to add procedures and policies in the Settings page."}

You help hotel staff with hotel procedures, complaint handling, check-in, emergencies, maintenance, upselling.
Be concise, practical and professional. You are talking to hotel staff, not guests.
Use bullet points and clear steps when explaining procedures."""

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=staff_prompt,
        messages=[{"role": "user", "content": user_message}]
    )
    return jsonify({"response": response.content[0].text})


@app.route('/submit-feedback', methods=['POST'])
def submit_feedback():
    data        = request.json
    guest_name  = data.get('guest_name', 'Guest')
    room_number = data.get('room_number', 'N/A')
    overall     = data.get('overall', 0)
    cleanliness = data.get('cleanliness', 0)
    staff       = data.get('staff', 0)
    dining      = data.get('dining', 0)
    wifi        = data.get('wifi', 0)
    comment     = data.get('comment', '')
    date        = datetime.now().strftime("%Y-%m-%d %H:%M")

    ph = placeholder()
    conn = get_conn(); c = conn.cursor()
    c.execute(f'''INSERT INTO feedback
              (guest_name, room_number, overall, cleanliness, staff, dining, wifi, comment, date, hotel_slug)
              VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})''',
            (guest_name, room_number, overall, cleanliness, staff, dining, wifi, comment, date, 
             request.json.get('slug', '')))
    conn.commit(); conn.close()

    def score_emoji(s): return {1:"😞",2:"😐",3:"🙂",4:"😊",5:"🤩"}.get(s, "N/A")

    resend.Emails.send({
        "from": "feedback@favvi.ai",
        "to": MANAGER_EMAIL,
        "subject": f"⭐ New Feedback from Room {room_number}",
        "html": f"""<h2>New Guest Feedback</h2>
        <p><strong>Guest:</strong> {guest_name}</p>
        <p><strong>Room:</strong> {room_number}</p>
        <p><strong>Date:</strong> {date}</p><hr>
        <p>Overall: {score_emoji(overall)} {overall}/5</p>
        <p>Cleanliness: {score_emoji(cleanliness)} {cleanliness}/5</p>
        <p>Staff: {score_emoji(staff)} {staff}/5</p>
        <p>Dining: {score_emoji(dining)} {dining}/5</p>
        <p>WiFi: {score_emoji(wifi)} {wifi}/5</p><hr>
        <p><strong>Comment:</strong> {comment or 'No comment left'}</p>"""
    })
    return jsonify({"success": True})


@app.route('/feedback-stats')
def feedback_stats():
    slug = request.args.get('slug', '')
    conn = get_conn(); c = conn.cursor()
    if slug:
        c.execute(f'SELECT AVG(overall), AVG(cleanliness), AVG(staff), AVG(dining), AVG(wifi), COUNT(*) FROM feedback WHERE hotel_slug = {placeholder()}', (slug,))
        row = c.fetchone()
        c.execute(f'SELECT * FROM feedback WHERE hotel_slug = {placeholder()} ORDER BY date DESC LIMIT 10', (slug,))
    else:
        c.execute('SELECT AVG(overall), AVG(cleanliness), AVG(staff), AVG(dining), AVG(wifi), COUNT(*) FROM feedback')
        row = c.fetchone()
        c.execute('SELECT * FROM feedback ORDER BY date DESC LIMIT 10')
    recent = c.fetchall()
    conn.close()
    return jsonify({
        "averages": {
            "overall":         round(float(row[0] or 0), 1),
            "cleanliness":     round(float(row[1] or 0), 1),
            "staff":           round(float(row[2] or 0), 1),
            "dining":          round(float(row[3] or 0), 1),
            "wifi":            round(float(row[4] or 0), 1),
            "total_responses": row[5] or 0
        },
        "recent": [list(r) for r in recent]
    })


@app.route('/get-requests')
def get_requests():
    slug = request.args.get('slug', '')
    conn = get_conn(); c = conn.cursor()
    if slug:
        c.execute(f'SELECT * FROM requests WHERE hotel_slug = {placeholder()} ORDER BY date DESC', (slug,))
    else:
        c.execute('SELECT * FROM requests ORDER BY date DESC')
    rows = c.fetchall(); conn.close()
    return jsonify([{
        'id':row[0],'room':row[1],'department':row[2],'details':row[3],
        'status':row[4],'claimed_by':row[5],'date':row[6]
    } for row in rows])


@app.route('/update-request', methods=['POST'])
def update_request():
    data       = request.json
    request_id = data.get('id')
    status     = data.get('status')
    claimed_by = data.get('claimed_by', '')

    ph = placeholder()
    conn = get_conn(); c = conn.cursor()
    c.execute(f'UPDATE requests SET status = {ph}, claimed_by = {ph} WHERE id = {ph}',
              (status, claimed_by, request_id))
    conn.commit(); conn.close()
    return jsonify({"success": True})


@app.route('/hotel-config')
def hotel_config():
    return jsonify({
        "name": HOTEL_NAME, "location": HOTEL_LOCATION,
        "staff_password": STAFF_PASSWORD, "manager_password": MANAGER_PASSWORD
    })


@app.route('/hotel-login', methods=['POST'])
def hotel_login():
    data = request.json
    email = data.get('email'); password = data.get('password')
    ph = placeholder()
    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT * FROM hotels WHERE email = {ph} AND password = {ph}', (email, password))
    hotel = c.fetchone(); conn.close()
    if hotel: return jsonify({"success": True, "slug": hotel[2], "name": hotel[1]})
    return jsonify({"success": False})


@app.route('/get-hotel/<slug>')
def get_hotel(slug):
    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT * FROM hotels WHERE slug = {placeholder()}', (slug,))
    hotel = c.fetchone(); conn.close()
    if not hotel: return jsonify({"error": "Not found"}), 404
    return jsonify({
        "id": hotel[0], "name": hotel[1], "slug": hotel[2], "email": hotel[3],
        "staff_password": hotel[6], "manager_password": hotel[7],
        "hotel_info":      hotel[9]  if len(hotel) > 9  and hotel[9]  else "",
        "current_offers":  hotel[10] if len(hotel) > 10 and hotel[10] else "",
        "manager_email":   hotel[11] if len(hotel) > 11 and hotel[11] else hotel[3],
        "staff_knowledge": hotel[12] if len(hotel) > 12 and hotel[12] else "",
        "location":        HOTEL_LOCATION,
    })


@app.route('/add-hotel', methods=['POST'])
def add_hotel():
    data = request.json
    ph = placeholder()
    conn = get_conn(); c = conn.cursor()
    c.execute(f'''INSERT INTO hotels
                  (name, slug, email, password, system_prompt,
                   staff_password, manager_password, date_created)
                  VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})''',
              (data.get('name'), data.get('slug'), data.get('email'),
               data.get('password'), data.get('system_prompt', ''),
               data.get('staff_password', 'staff2024'),
               data.get('manager_password', 'manager2024'),
               datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit(); conn.close()
    return jsonify({"success": True})


@app.route('/update-hotel-settings', methods=['POST'])
def update_hotel_settings():
    data = request.json
    slug = data.get('slug')
    if not slug: return jsonify({"success": False, "error": "No slug provided"})

    ph = placeholder()
    fields, values = [], []
    for field in ['name','manager_email','hotel_info','current_offers',
                  'staff_password','manager_password','staff_knowledge']:
        if field in data:
            fields.append(f'{field} = {ph}')
            values.append(data[field])

    if not fields: return jsonify({"success": False, "error": "Nothing to update"})

    values.append(slug)
    try:
        conn = get_conn(); c = conn.cursor()
        c.execute(f"UPDATE hotels SET {', '.join(fields)} WHERE slug = {ph}", values)
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

    
@app.route('/signup', methods=['POST'])
def signup():
    data       = request.json
    hotel_name = data.get('name', '').strip()
    email      = data.get('email', '').strip()
    password   = data.get('password', '').strip()
    staff_pw   = data.get('staff_password', '').strip()
    manager_pw = data.get('manager_password', '').strip()

    if not hotel_name or not email or not password:
        return jsonify({"success": False, "error": "Please fill in all fields"})

    import re
    slug = re.sub(r'[^a-z0-9]+', '-', hotel_name.lower()).strip('-')

    ph = placeholder()
    conn = get_conn(); c = conn.cursor()
    c.execute(f'SELECT id FROM hotels WHERE slug = {ph} OR email = {ph}', (slug, email))
    existing = c.fetchone(); conn.close()

    if existing:
        return jsonify({"success": False, "error": "A hotel with this name or email already exists"})

    conn = get_conn(); c = conn.cursor()
    c.execute(f'''INSERT INTO hotels
                  (name, slug, email, password, system_prompt,
                   staff_password, manager_password, date_created)
                  VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})''',
              (hotel_name, slug, email, password, '',
               staff_pw or 'staff2024',
               manager_pw or 'manager2024',
               datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit(); conn.close()

    try:
        resend.Emails.send({
            "from": "hello@favvi.ai",
            "to": email,
            "subject": f"Welcome to Favvi — {hotel_name} is live!",
            "html": f"""
            <div style="font-family: Arial, sans-serif; max-width: 560px; margin: 0 auto;">
                <h2 style="color: #1a1a2e;">Welcome to Favvi</h2>
                <p>Your hotel portal is ready. Here are your details:</p>
                <div style="background: #f5f5f5; padding: 20px; border-radius: 8px; margin: 20px 0;">
                    <p><strong>Hotel:</strong> {hotel_name}</p>
                    <p><strong>Portal:</strong> https://favvi.ai/portal/{slug}</p>
                    <p><strong>Login:</strong> https://favvi.ai/login</p>
                    <p><strong>Email:</strong> {email}</p>
                    <p><strong>Password:</strong> {password}</p>
                    <p><strong>Staff Password:</strong> {staff_pw or 'staff2024'}</p>
                    <p><strong>Manager Password:</strong> {manager_pw or 'manager2024'}</p>
                </div>
                <h3 style="color: #1a1a2e;">Getting Started</h3>
                <ol>
                    <li>Log in and go to <strong>Settings</strong></li>
                    <li>Paste your hotel info, room details and facilities</li>
                    <li>Share <strong>favvi.ai/portal/{slug}/chat</strong> as a QR code in guest rooms</li>
                    <li>Staff log into <strong>favvi.ai/portal/{slug}/staff</strong> for live requests</li>
                </ol>
                <a href="https://favvi.ai/portal/{slug}"
                   style="display:inline-block; background:#1a1a2e; color:white;
                          padding:12px 28px; text-decoration:none; border-radius:4px; margin-top:16px;">
                    Go to Your Portal
                </a>
                <p style="color:#999; font-size:12px; margin-top:32px;">
                    Favvi — AI guest experience platform
                </p>
            </div>
            """
        })
    except Exception:
        pass

    return jsonify({"success": True, "slug": slug})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)