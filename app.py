import os
import resend
from flask import Flask, request, jsonify, send_from_directory
import anthropic

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
resend.api_key = os.environ.get("RESEND_API_KEY")

conversation_history = []

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
Reply in the same language the guest uses."""

@app.route('/')
def home():
    return send_from_directory('.', 'index.html')

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
    
    # Check if staff needs to be notified
    if "STAFF_ALERT:" in assistant_message:
        alert_line = [line for line in assistant_message.split('\n') if 'STAFF_ALERT:' in line][0]
        alert_details = alert_line.replace('STAFF_ALERT:', '').strip()
        
        resend.Emails.send({
            "from": "onboarding@resend.dev",
            "to": "vinsadam11@gmail.com",
            "subject": "🛎️ New Guest Request",
            "html": f"<h2>New Guest Request</h2><p>{alert_details}</p>"
        })
        
        # Clean message shown to guest
        clean_message = assistant_message.replace(alert_line, '').strip()
        return jsonify({"response": clean_message})
    
    return jsonify({"response": assistant_message})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)