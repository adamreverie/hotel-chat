from config import (HOTEL_NAME, HOTEL_LOCATION, MANAGER_EMAIL,
                    STAFF_PASSWORD, MANAGER_PASSWORD,
                    HOTEL_INFO, CURRENT_OFFERS)
import os
import resend
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
import anthropic

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
resend.api_key = os.environ.get("RESEND_API_KEY")


# ── DATABASE SETUP ────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect('feedback.db')
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS feedback
                (id INTEGER PRIMARY KEY AUTOINCREMENT,
                 guest_name TEXT,
                 room_number TEXT,
                 overall INTEGER,
                 cleanliness INTEGER,
                 staff INTEGER,
                 dining INTEGER,
                 wifi INTEGER,
                 comment TEXT,
                 date TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS requests
                (id INTEGER PRIMARY KEY AUTOINCREMENT,
                 room_number TEXT,
                 department TEXT,
                 details TEXT,
                 status TEXT DEFAULT 'new',
                 claimed_by TEXT,
                 date TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS hotels
                (id INTEGER PRIMARY KEY AUTOINCREMENT,
                 name TEXT,
                 slug TEXT UNIQUE,
                 email TEXT,
                 password TEXT,
                 system_prompt TEXT,
                 staff_password TEXT,
                 manager_password TEXT,
                 date_created TEXT)''')

    # Add new columns if they don't exist yet
    for col_sql in [
        "ALTER TABLE hotels ADD COLUMN hotel_info TEXT",
        "ALTER TABLE hotels ADD COLUMN current_offers TEXT",
        "ALTER TABLE hotels ADD COLUMN manager_email TEXT",
        "ALTER TABLE hotels ADD COLUMN staff_knowledge TEXT",
    ]:
        try:
            c.execute(col_sql)
        except Exception:
            pass

    conn.commit()
    conn.close()

init_db()


# ── SYSTEM PROMPT (reads from DB, falls back to config.py) ───────────────────

def get_system_prompt(slug=None):
    name           = HOTEL_NAME
    hotel_info     = HOTEL_INFO
    current_offers = CURRENT_OFFERS
    manager_email  = MANAGER_EMAIL

    if slug:
        conn = sqlite3.connect('feedback.db')
        c = conn.cursor()
        c.execute('SELECT * FROM hotels WHERE slug = ?', (slug,))
        hotel = c.fetchone()
        conn.close()
        if hotel:
            name           = hotel[1] or HOTEL_NAME
            hotel_info     = hotel[9]  if len(hotel) > 9  and hotel[9]  else HOTEL_INFO
            current_offers = hotel[10] if len(hotel) > 10 and hotel[10] else CURRENT_OFFERS
            manager_email  = hotel[11] if len(hotel) > 11 and hotel[11] else MANAGER_EMAIL

    return f"""You are the AI guest concierge for {name}, located in {HOTEL_LOCATION}.

{hotel_info}

{current_offers}

When a guest makes a REAL REQUEST (towels, room service, maintenance, housekeeping, spa booking):
1. Ask for their room number if you don't have it
2. Confirm their request warmly
3. Tell them the team has been notified
4. End with exactly: STAFF_ALERT: Room [number] - [request details]

For simple questions answer directly and helpfully.
Always be warm, professional and friendly. Use occasional emojis.
Reply in the same language the guest uses.
Never use markdown formatting like #, ##, **, or ---.
Use plain text only with line breaks for spacing."""


# ── PAGE ROUTES ───────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return send_from_directory('.', 'landing.html')

@app.route('/login')
def login_page():
    return send_from_directory('.', 'login.html')

@app.route('/staff')
def staff():
    return send_from_directory('.', 'staff.html')

@app.route('/feedback')
def feedback():
    return send_from_directory('.', 'feedback.html')

@app.route('/dashboard')
def dashboard():
    return send_from_directory('.', 'dashboard.html')

@app.route('/portal/<slug>')
def portal(slug):
    conn = sqlite3.connect('feedback.db')
    c = conn.cursor()
    c.execute('SELECT * FROM hotels WHERE slug = ?', (slug,))
    hotel = c.fetchone()
    conn.close()
    if not hotel:
        return "Hotel not found", 404
    return send_from_directory('.', 'portal.html')

@app.route('/portal/<slug>/chat')
def portal_chat(slug):
    return send_from_directory('.', 'index.html')

@app.route('/portal/<slug>/staff')
def portal_staff(slug):
    return send_from_directory('.', 'staff.html')

@app.route('/portal/<slug>/feedback')
def portal_feedback(slug):
    return send_from_directory('.', 'feedback.html')

@app.route('/portal/<slug>/dashboard')
def portal_dashboard(slug):
    return send_from_directory('.', 'dashboard.html')

@app.route('/portal/<slug>/settings')
def portal_settings(slug):
    return send_from_directory('.', 'settings.html')

@app.route('/portal/<slug>/staffchat')
def portal_staffchat(slug):
    return send_from_directory('.', 'staffchat.html')


# ── GUEST CHAT ────────────────────────────────────────────────────────────────

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
        alert_line   = [line for line in assistant_message.split('\n') if 'STAFF_ALERT:' in line][0]
        alert_details = alert_line.replace('STAFF_ALERT:', '').strip()

        # Detect department
        department = "general"
        if any(w in alert_details.lower() for w in ["towel", "sheet", "clean", "housekeeping", "linen"]):
            department = "housekeeping"
        elif any(w in alert_details.lower() for w in ["food", "drink", "room service", "sandwich", "breakfast", "dinner", "lunch", "water"]):
            department = "roomservice"
        elif any(w in alert_details.lower() for w in ["ac", "air", "light", "tv", "broken", "leak", "maintenance", "wifi", "internet"]):
            department = "maintenance"
        elif any(w in alert_details.lower() for w in ["taxi", "transfer", "transport", "tour", "concierge", "recommend"]):
            department = "concierge"

        # Extract room number
        room = "N/A"
        for word in alert_details.split():
            if word.isdigit():
                room = word
                break

        # Save request to DB
        conn = sqlite3.connect('feedback.db')
        c = conn.cursor()
        c.execute('''INSERT INTO requests (room_number, department, details, status, date)
                     VALUES (?, ?, ?, ?, ?)''',
                  (room, department, alert_details, 'new',
                   datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
        conn.close()

        # Get manager email from DB
        email_to = MANAGER_EMAIL
        if slug:
            conn = sqlite3.connect('feedback.db')
            c = conn.cursor()
            c.execute('SELECT manager_email FROM hotels WHERE slug = ?', (slug,))
            row = c.fetchone()
            conn.close()
            if row and row[0]:
                email_to = row[0]

        resend.Emails.send({
            "from": "onboarding@resend.dev",
            "to": email_to,
            "subject": f"🛎️ New Request — Room {room}",
            "html": f"<h2>New Guest Request</h2><p><strong>Department:</strong> {department.title()}</p><p><strong>Details:</strong> {alert_details}</p>"
        })

        clean_message = assistant_message.replace(alert_line, '').strip()
        return jsonify({"response": clean_message, "history": history})

    return jsonify({"response": assistant_message, "history": history})


# ── STAFF CHAT ────────────────────────────────────────────────────────────────

@app.route('/staff-chat', methods=['POST'])
def staff_chat():
    data         = request.json
    user_message = data.get('message')
    slug         = data.get('slug')

    hotel_info      = HOTEL_INFO
    hotel_name      = HOTEL_NAME
    staff_knowledge = ""

    if slug:
        conn = sqlite3.connect('feedback.db')
        c = conn.cursor()
        c.execute('SELECT name, hotel_info, staff_knowledge FROM hotels WHERE slug = ?', (slug,))
        row = c.fetchone()
        conn.close()
        if row:
            hotel_name      = row[0] or HOTEL_NAME
            hotel_info      = row[1] or HOTEL_INFO
            staff_knowledge = row[2] or ""

    staff_prompt = f"""You are an internal AI assistant for the staff of {hotel_name}.

HOTEL INFORMATION:
{hotel_info}

STAFF KNOWLEDGE BASE:
{staff_knowledge if staff_knowledge else "No staff knowledge base has been set up yet. Ask your manager to add procedures and policies in the Settings page."}

You help hotel staff with:
- Hotel procedures and policies
- How to handle guest complaints and difficult situations
- Room upgrade and check-in procedures
- Emergency procedures
- Maintenance and housekeeping protocols
- Upselling techniques and guest satisfaction tips

Be concise, practical and professional.
You are talking to hotel staff, not guests.
Use bullet points and clear steps when explaining procedures."""

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=staff_prompt,
        messages=[{"role": "user", "content": user_message}]
    )

    return jsonify({"response": response.content[0].text})


# ── FEEDBACK ──────────────────────────────────────────────────────────────────

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

    conn = sqlite3.connect('feedback.db')
    c = conn.cursor()
    c.execute('''INSERT INTO feedback
                (guest_name, room_number, overall, cleanliness, staff, dining, wifi, comment, date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (guest_name, room_number, overall, cleanliness, staff, dining, wifi, comment, date))
    conn.commit()
    conn.close()

    def score_emoji(score):
        return {1:"😞", 2:"😐", 3:"🙂", 4:"😊", 5:"🤩"}.get(score, "N/A")

    resend.Emails.send({
        "from": "onboarding@resend.dev",
        "to": MANAGER_EMAIL,
        "subject": f"⭐ New Feedback from Room {room_number}",
        "html": f"""
        <h2>New Guest Feedback</h2>
        <p><strong>Guest:</strong> {guest_name}</p>
        <p><strong>Room:</strong> {room_number}</p>
        <p><strong>Date:</strong> {date}</p>
        <hr>
        <p>Overall Experience: {score_emoji(overall)} {overall}/5</p>
        <p>Room Cleanliness: {score_emoji(cleanliness)} {cleanliness}/5</p>
        <p>Staff Friendliness: {score_emoji(staff)} {staff}/5</p>
        <p>Dining Experience: {score_emoji(dining)} {dining}/5</p>
        <p>WiFi Quality: {score_emoji(wifi)} {wifi}/5</p>
        <hr>
        <p><strong>Comment:</strong> {comment or 'No comment left'}</p>
        """
    })

    return jsonify({"success": True})


@app.route('/feedback-stats')
def feedback_stats():
    conn = sqlite3.connect('feedback.db')
    c = conn.cursor()
    c.execute('''SELECT AVG(overall), AVG(cleanliness), AVG(staff),
                        AVG(dining), AVG(wifi), COUNT(*)
                 FROM feedback''')
    row = c.fetchone()
    c.execute('SELECT * FROM feedback ORDER BY date DESC LIMIT 10')
    recent = c.fetchall()
    conn.close()

    return jsonify({
        "averages": {
            "overall":         round(row[0] or 0, 1),
            "cleanliness":     round(row[1] or 0, 1),
            "staff":           round(row[2] or 0, 1),
            "dining":          round(row[3] or 0, 1),
            "wifi":            round(row[4] or 0, 1),
            "total_responses": row[5]
        },
        "recent": recent
    })


@app.route('/send-feedback-email', methods=['POST'])
def send_feedback_email():
    data        = request.json
    guest_email = data.get('email')
    guest_name  = data.get('name', 'Guest')
    room_number = data.get('room', '')
    feedback_link = f"{request.host_url}feedback?room={room_number}&name={guest_name}"

    resend.Emails.send({
        "from": "onboarding@resend.dev",
        "to": guest_email,
        "subject": f"How was your stay at {HOTEL_NAME}?",
        "html": f"""
        <div style="font-family: Arial, sans-serif; max-width: 500px; margin: 0 auto;">
            <h2 style="color: #1a1a2e;">Thank you for staying with us!</h2>
            <p>Dear {guest_name},</p>
            <p>We hope you had a wonderful stay at {HOTEL_NAME}.</p>
            <p>We'd love to hear about your experience. It takes less than 30 seconds:</p>
            <a href="{feedback_link}"
               style="background: #1a1a2e; color: white; padding: 12px 24px;
                      text-decoration: none; border-radius: 4px; display: inline-block; margin: 20px 0;">
                Share Your Feedback
            </a>
            <p style="color: #999; font-size: 12px;">{HOTEL_NAME} — {HOTEL_LOCATION}</p>
        </div>
        """
    })

    return jsonify({"success": True})


# ── STAFF REQUESTS ────────────────────────────────────────────────────────────

@app.route('/get-requests')
def get_requests():
    conn = sqlite3.connect('feedback.db')
    c = conn.cursor()
    c.execute('SELECT * FROM requests ORDER BY date DESC')
    rows = c.fetchall()
    conn.close()

    return jsonify([{
        'id':         row[0],
        'room':       row[1],
        'department': row[2],
        'details':    row[3],
        'status':     row[4],
        'claimed_by': row[5],
        'date':       row[6]
    } for row in rows])


@app.route('/update-request', methods=['POST'])
def update_request():
    data       = request.json
    request_id = data.get('id')
    status     = data.get('status')
    claimed_by = data.get('claimed_by', '')

    conn = sqlite3.connect('feedback.db')
    c = conn.cursor()
    c.execute('UPDATE requests SET status = ?, claimed_by = ? WHERE id = ?',
              (status, claimed_by, request_id))
    conn.commit()
    conn.close()

    return jsonify({"success": True})


# ── HOTEL CONFIG & AUTH ───────────────────────────────────────────────────────

@app.route('/hotel-config')
def hotel_config():
    return jsonify({
        "name":             HOTEL_NAME,
        "location":         HOTEL_LOCATION,
        "staff_password":   STAFF_PASSWORD,
        "manager_password": MANAGER_PASSWORD
    })


@app.route('/hotel-login', methods=['POST'])
def hotel_login():
    data     = request.json
    email    = data.get('email')
    password = data.get('password')

    conn = sqlite3.connect('feedback.db')
    c = conn.cursor()
    c.execute('SELECT * FROM hotels WHERE email = ? AND password = ?', (email, password))
    hotel = c.fetchone()
    conn.close()

    if hotel:
        return jsonify({"success": True, "slug": hotel[2], "name": hotel[1]})
    return jsonify({"success": False})


@app.route('/get-hotel/<slug>')
def get_hotel(slug):
    conn = sqlite3.connect('feedback.db')
    c = conn.cursor()
    c.execute('SELECT * FROM hotels WHERE slug = ?', (slug,))
    hotel = c.fetchone()
    conn.close()

    if not hotel:
        return jsonify({"error": "Not found"}), 404

    return jsonify({
        "id":               hotel[0],
        "name":             hotel[1],
        "slug":             hotel[2],
        "email":            hotel[3],
        "staff_password":   hotel[6],
        "manager_password": hotel[7],
        "hotel_info":       hotel[9]  if len(hotel) > 9  and hotel[9]  else "",
        "current_offers":   hotel[10] if len(hotel) > 10 and hotel[10] else "",
        "manager_email":    hotel[11] if len(hotel) > 11 and hotel[11] else hotel[3],
        "location":         HOTEL_LOCATION,
        "staff_knowledge":  hotel[12] if len(hotel) > 12 and hotel[12] else "",
    })


@app.route('/add-hotel', methods=['POST'])
def add_hotel():
    data = request.json

    conn = sqlite3.connect('feedback.db')
    c = conn.cursor()
    c.execute('''INSERT INTO hotels
                (name, slug, email, password, system_prompt,
                 staff_password, manager_password, date_created)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
              (data.get('name'),
               data.get('slug'),
               data.get('email'),
               data.get('password'),
               data.get('system_prompt', ''),
               data.get('staff_password', 'staff2024'),
               data.get('manager_password', 'manager2024'),
               datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()

    return jsonify({"success": True})


@app.route('/update-hotel-settings', methods=['POST'])
def update_hotel_settings():
    data = request.json
    slug = data.get('slug')

    if not slug:
        return jsonify({"success": False, "error": "No slug provided"})

    fields, values = [], []

    if 'name'             in data: fields.append('name = ?');             values.append(data['name'])
    if 'manager_email'    in data: fields.append('manager_email = ?');    values.append(data['manager_email'])
    if 'hotel_info'       in data: fields.append('hotel_info = ?');       values.append(data['hotel_info'])
    if 'current_offers'   in data: fields.append('current_offers = ?');   values.append(data['current_offers'])
    if 'staff_knowledge'  in data: fields.append('staff_knowledge = ?');  values.append(data['staff_knowledge'])
    if 'staff_password'   in data: fields.append('staff_password = ?');   values.append(data['staff_password'])
    if 'manager_password' in data: fields.append('manager_password = ?'); values.append(data['manager_password'])

    if not fields:
        return jsonify({"success": False, "error": "Nothing to update"})

    values.append(slug)
    try:
        conn = sqlite3.connect('feedback.db')
        c = conn.cursor()
        c.execute(f"UPDATE hotels SET {', '.join(fields)} WHERE slug = ?", values)
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ── RUN ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)