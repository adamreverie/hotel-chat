import os
import resend
from flask import Flask, request, jsonify, send_from_directory
import anthropic

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
resend.api_key = os.environ.get("RESEND_API_KEY")

conversation_history = []

system_prompt = """You are a helpful hotel assistant for a luxury hotel in Dubai.
You help guests with:
- WiFi password (Password is: ILoveAlexie)
- Breakfast times (6:30am to 11am, Azure Restaurant, Ground Floor)
- Pool hours (7am to 10pm daily)
- Checkout time (12pm, late checkout available on request)
- Room service (available 24/7, dial 0 on room phone)
- Airport transfer (available, book 24hrs in advance at reception)

When a guest makes a REAL REQUEST (towels, room service, maintenance, housekeeping):
1. Ask for their room number if you don't have it
2. Confirm their request
3. Tell them the team has been notified
4. End your response with exactly this format on a new line:
   STAFF_ALERT: Room [number] - [request details]

For simple questions just answer directly. Be warm and professional. 
Use occasional emojis. Reply in the same language the guest uses."""

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