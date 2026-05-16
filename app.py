import os
import resend
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
import anthropic

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
resend.api_key = os.environ.get("RESEND_API_KEY")

conversation_history = []

# Database setup
def init_db():
    conn = sqlite3.connect('feedback.db')
    c = conn.cursor()
    
    # Feedback table
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
    
    # Requests table
    c.execute('''CREATE TABLE IF NOT EXISTS requests
                (id INTEGER PRIMARY KEY AUTOINCREMENT,
                 room_number TEXT,
                 department TEXT,
                 details TEXT,
                 status TEXT DEFAULT 'new',
                 claimed_by TEXT,
                 date TEXT)''')
    
    conn.commit()
    conn.close()

init_db()

system_prompt = """You are the AI guest assistant for Park Regis Kris Kin Hotel, 
located in the heart of Bur Dubai, opposite BurJuman Shopping Centre.

HOTEL CONTACT:
- General: +971 4 377 1111
- WhatsApp Reservations: +971 54 525 7364
- Restaurant: +971 50 707 1196
- Spa: +971 56 414 7482

ROOMS:
- Superior Room (Double/Twin/Triple): 38-43 sqm
- Junior Suite (Double/Twin/Two doubles): 77 sqm
- Two Bedroom Suite: 94 sqm, sleeps 3-4
- All rooms include: Free WiFi, interactive TV (100+ channels),
  in-room safe, universal power points, tea & coffee facilities,
  air conditioning control, do not disturb indicator

DINING & BARS (5 venues):
- Kris With A View: International all day dining
- Tenggara: Authentic South East Asian cuisine
- The Grandstand: Sports bar, live sports, international beers
- Level 19 Lounge & Bar: Sophisticated evening lounge
- Marhaba Lounge: Fine coffees, teas and pastries
- Restaurant reservations: +971 50 707 1196

FACILITIES:
- Rooftop pool with panoramic Dubai skyline views
- Spa Suasana: +971 56 414 7482
- Gymnasium
- Conference & banquet facilities (up to 200 guests)
- In-room dining available

LOCATION & TRANSPORT:
- Bur Dubai, near metro station
- 10 min taxi to Dubai World Trade Centre
- Near Dubai Mall, Dubai Frame, Dubai Museum, Dubai Creek

CURRENT OFFERS:
- Check in & Dine: AED 298 for 24hr stay with dining credit
- Early Bird: 20% off when booked 20 days in advance
- Stay 5 nights: Save 15%
- Flexible Bed & Breakfast: Free cancellation up to 48hrs before arrival

CANCELLATION POLICY:
- Free cancellation up to 48 hours before arrival
- Within 48 hours: charges may apply
- Non-refundable rate: 10% discount, prepay required

LOYALTY PROGRAM:
- Seibu Prince Global Rewards — members get exclusive rates

When a guest makes a REAL REQUEST (towels, room service, 
maintenance, housekeeping, spa booking):
1. Ask for their room number if you don't have it
2. Confirm their request warmly
3. Tell them the team has been notified
4. End with exactly: STAFF_ALERT: Room [number] - [request details]

For simple questions answer directly and helpfully.
Always be warm, professional and friendly. Use occasional emojis.
Reply in the same language the guest uses.
Never use markdown formatting like #, ##, **, or ---.
Use plain text only with line breaks for spacing."""

@app.route('/')
def home():
    return send_from_directory('.', 'index.html')

@app.route('/feedback')
def feedback():
    return send_from_directory('.', 'feedback.html')

@app.route('/dashboard')
def dashboard():
    return send_from_directory('.', 'dashboard.html')

@app.route('/chat', methods=['POST'])
def chat():
    user_message = request.json.get('message')
    
    conversation_history.append({
        "role": "user",
        "content": user_message
    })
    
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=system_prompt,
        messages=conversation_history
    )
    
    assistant_message = response.content[0].text
    
    conversation_history.append({
        "role": "assistant",
        "content": assistant_message
    })
    
    if "STAFF_ALERT:" in assistant_message:
        alert_line = [line for line in assistant_message.split('\n') if 'STAFF_ALERT:' in line][0]
        alert_details = alert_line.replace('STAFF_ALERT:', '').strip()

        # Detect department
        department = "general"
        if any(word in alert_details.lower() for word in ["towel", "sheet", "clean", "housekeeping", "linen"]):
            department = "housekeeping"
        elif any(word in alert_details.lower() for word in ["food", "drink", "room service", "sandwich", "breakfast", "dinner", "lunch", "water"]):
            department = "roomservice"
        elif any(word in alert_details.lower() for word in ["ac", "air", "light", "tv", "broken", "leak", "maintenance", "wifi", "internet"]):
            department = "maintenance"
        elif any(word in alert_details.lower() for word in ["taxi", "transfer", "transport", "tour", "concierge", "recommend"]):
            department = "concierge"

        # Extract room number
        room = "N/A"
        for word in alert_details.split():
            if word.isdigit():
                room = word
                break

        # Save to database
        conn = sqlite3.connect('feedback.db')
        c = conn.cursor()
        c.execute('''INSERT INTO requests 
                    (room_number, department, details, status, date)
                    VALUES (?, ?, ?, ?, ?)''',
                  (room, department, alert_details, 'new',
                   datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
        conn.close()

        # Send email
        resend.Emails.send({
            "from": "onboarding@resend.dev",
            "to": "vinsadam11@gmail.com",
            "subject": f"🛎️ New Request — Room {room}",
            "html": f"<h2>New Guest Request</h2><p><strong>Department:</strong> {department.title()}</p><p><strong>Details:</strong> {alert_details}</p>"
        })

        clean_message = assistant_message.replace(alert_line, '').strip()
        return jsonify({"response": clean_message})
    
    return jsonify({"response": assistant_message})

@app.route('/submit-feedback', methods=['POST'])
def submit_feedback():
    data = request.json
    
    guest_name = data.get('guest_name', 'Guest')
    room_number = data.get('room_number', 'N/A')
    overall = data.get('overall', 0)
    cleanliness = data.get('cleanliness', 0)
    staff = data.get('staff', 0)
    dining = data.get('dining', 0)
    wifi = data.get('wifi', 0)
    comment = data.get('comment', '')
    date = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # Save to database
    conn = sqlite3.connect('feedback.db')
    c = conn.cursor()
    c.execute('''INSERT INTO feedback 
                (guest_name, room_number, overall, cleanliness, 
                 staff, dining, wifi, comment, date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (guest_name, room_number, overall, cleanliness,
               staff, dining, wifi, comment, date))
    conn.commit()
    conn.close()
    
    # Emoji helper
    def score_emoji(score):
        emojis = {1: "😞", 2: "😐", 3: "🙂", 4: "😊", 5: "🤩"}
        return emojis.get(score, "N/A")
    
    # Email manager
    resend.Emails.send({
        "from": "onboarding@resend.dev",
        "to": "vinsadam11@gmail.com",
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

@app.route('/send-feedback-email', methods=['POST'])
def send_feedback_email():
    data = request.json
    guest_email = data.get('email')
    guest_name = data.get('name', 'Guest')
    room_number = data.get('room', '')
    
    feedback_link = f"{request.host_url}feedback?room={room_number}&name={guest_name}"
    
    resend.Emails.send({
        "from": "onboarding@resend.dev",
        "to": guest_email,
        "subject": "How was your stay at Park Regis Kris Kin? 🏨",
        "html": f"""
        <div style="font-family: Arial, sans-serif; max-width: 500px; margin: 0 auto;">
            <h2 style="color: #1a1a2e;">Thank you for staying with us!</h2>
            <p>Dear {guest_name},</p>
            <p>We hope you had a wonderful stay at Park Regis Kris Kin Dubai.</p>
            <p>We'd love to hear about your experience. It takes less than 30 seconds:</p>
            <a href="{feedback_link}" 
               style="background: #1a1a2e; color: white; padding: 12px 24px; 
                      text-decoration: none; border-radius: 25px; display: inline-block;
                      margin: 20px 0;">
                Share Your Feedback →
            </a>
            <p style="color: #999; font-size: 12px;">
                Park Regis Kris Kin Dubai<br>
                Sheikh Khalifah Bin Zayed St, Bur Dubai
            </p>
        </div>
        """
    })
    
    return jsonify({"success": True})

@app.route('/feedback-stats')
def feedback_stats():
    conn = sqlite3.connect('feedback.db')
    c = conn.cursor()
    c.execute('''SELECT 
                AVG(overall), AVG(cleanliness), 
                AVG(staff), AVG(dining), AVG(wifi),
                COUNT(*) FROM feedback''')
    row = c.fetchone()
    
    c.execute('SELECT * FROM feedback ORDER BY date DESC LIMIT 10')
    recent = c.fetchall()
    conn.close()
    
    return jsonify({
        "averages": {
            "overall": round(row[0] or 0, 1),
            "cleanliness": round(row[1] or 0, 1),
            "staff": round(row[2] or 0, 1),
            "dining": round(row[3] or 0, 1),
            "wifi": round(row[4] or 0, 1),
            "total_responses": row[5]
        },
        "recent": recent
    })
@app.route('/staff')
def staff():
    return send_from_directory('.', 'staff.html')

@app.route('/get-requests')
def get_requests():
    conn = sqlite3.connect('feedback.db')
    c = conn.cursor()
    c.execute('SELECT * FROM requests ORDER BY date DESC')
    rows = c.fetchall()
    conn.close()
    
    requests_list = []
    for row in rows:
        requests_list.append({
            'id': row[0],
            'room': row[1],
            'department': row[2],
            'details': row[3],
            'status': row[4],
            'claimed_by': row[5],
            'date': row[6]
        })
    
    return jsonify(requests_list)

@app.route('/update-request', methods=['POST'])
def update_request():
    data = request.json
    request_id = data.get('id')
    status = data.get('status')
    claimed_by = data.get('claimed_by', '')
    
    conn = sqlite3.connect('feedback.db')
    c = conn.cursor()
    c.execute('''UPDATE requests 
                SET status = ?, claimed_by = ?
                WHERE id = ?''',
              (status, claimed_by, request_id))
    conn.commit()
    conn.close()
    
    return jsonify({"success": True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)