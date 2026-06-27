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
        }
    ],
    "trigger": {
        "level": "error",
        "message": "Payment processing failed!",
        "timestamp": "2026-06-26T10:55:00.000Z",
        "stack_trace": {
            "exception_type": "ZeroDivisionError",
            "exception_message": "division by zero",
            "frames": [
                {
                    "file": "/app/main.py",
                    "line": 15,
                    "function": "checkout_endpoint",
                    "code_context": "    return process_payment(order_id)"
                },
                {
                    "file": "/app/services/payment.py",
                    "line": 42,
                    "function": "process_payment",
                    "code_context": "    tax_rate = amount / 0"
                }
            ]
        }
    },
    "sdk_meta": {
        "python_version": "3.11",
        "framework": "FastAPI",
    }
}

response = requests.post(url, json=payload, headers=headers)
print("Status Code:", response.status_code)
print("Response:", response.text)
