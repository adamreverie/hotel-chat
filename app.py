import os
from flask import Flask, request, jsonify, send_from_directory
import anthropic

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
conversation_history = []

system_prompt = """You are a helpful hotel assistant for a luxury hotel in Dubai. 
You help guests with:
- WiFi password (Password is: ILoveAlexie)
- Breakfast times (6:30am to 11am, Azure Restaurant, Ground Floor)
- Pool hours (7am to 10pm daily)
- Checkout time (12pm, late checkout available on request)
- Room service (available 24/7, dial 0 on room phone)
- Airport transfer (available, book 24hrs in advance at reception)

For actual requests like towels, room service orders, or maintenance:
Collect the guest's room number and request details, then confirm 
you've notified the team.

Always be warm, friendly and professional.
Reply in the same language the guest uses.
Use occasional emojis. Keep replies short and clear."""

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
    
    return jsonify({"response": assistant_message})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)