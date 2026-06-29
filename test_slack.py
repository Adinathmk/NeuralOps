import urllib.request
import json

payload = {
    "blocks": [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🚨 New Incident: TestError"
            }
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": "*Service:*\ntest-service"
                },
                {
                    "type": "mrkdwn",
                    "text": "*Severity:*\nCRITICAL"
                }
            ]
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*TestError*\nThis is a test message"
            }
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Open in NeuralOps"
                    },
                    "style": "primary",
                    "url": "http://localhost:3000/dashboard/incidents/123"
                }
            ]
        }
    ]
}

req = urllib.request.Request(
    'http://example.com/dummy-slack-webhook', 
    data=json.dumps(payload).encode('utf-8'), 
    headers={'Content-Type': 'application/json'}
)
try:
    response = urllib.request.urlopen(req)
    print(response.read())
except Exception as e:
    print(e.read())
