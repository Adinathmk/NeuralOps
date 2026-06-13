import urllib.request
import json

url = "http://localhost:8001/api/v1/ingest/logs"
headers = {
    "Content-Type": "application/json",
    "X-Consumer-Custom-Id": "6654ef13-8b08-40fc-9baf-9e9713a361db"
}
data = {
  "incident_id": "847ac10b-58cc-4372-a567-0e02b2c3d472",
  "service_name": "dummy-order-service",
  "environment": "production",
  "context_logs": [
    {
      "seq": 1,
      "timestamp": "2026-06-12T15:19:55Z",
      "level": "INFO",
      "message": "Received order request from user user_123"
    },
    {
      "seq": 2,
      "timestamp": "2026-06-12T15:19:56Z",
      "level": "DEBUG",
      "message": "Attempting to apply discount code 'INVALID99'"
    },
    {
      "seq": 3,
      "timestamp": "2026-06-12T15:20:00Z",
      "level": "ERROR",
      "message": "TypeError: 'NoneType' object is not subscriptable",
      "stack_trace": [
        {
          "file": "services.py",
          "line": 12,
          "method": "process_order",
          "module": "services"
        },
        {
          "file": "main.py",
          "line": 14,
          "method": "create_order",
          "module": "main"
        }
      ]
    }
  ]
}

req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers)
try:
    with urllib.request.urlopen(req) as response:
        print("Status:", response.status)
        print("Response:", response.read().decode("utf-8"))
except Exception as e:
    print("Error:", e)
