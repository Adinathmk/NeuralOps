import uuid

import requests

tenant_id = "6654ef13-8b08-40fc-9baf-9e9713a361db"
import os

api_key = os.getenv("API_KEY", "your_api_key_here")

url = "http://localhost:8001/api/v1/ingest/logs"
headers = {"Authorization": f"Bearer {api_key}"}

payload = {
    "service_name": "payment-service",
    "environment": "production",
    "incident_id": str(uuid.uuid4()),
    "context_logs": [
        {
            "timestamp": "2024-03-10T12:00:00Z",
            "level": "error",
            "message": "Payment processing failed",
            "logger_name": "payment.charge",
            "exception": {
                "type": "ValueError",
                "message": "Invalid credit card format",
                "stacktrace": 'Traceback (most recent call last):\n  File "charge.py", line 42, in process\nValueError: Invalid credit card format',
            },
        }
    ],
}

response = requests.post(url, json=payload, headers=headers)
print("Status Code:", response.status_code)
print("Response:", response.text)
