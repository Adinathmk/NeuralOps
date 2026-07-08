import os
import django
from dotenv import load_dotenv

load_dotenv()  # This will load .env from the current directory
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.analytics.models import IncidentSnapshot

IncidentSnapshot.objects.create(
    tenant_id='3cf86985-1d48-4e1b-90e6-a05d8f6d70bc',
    incident_id='1697ef6e-2688-4326-8f1e-9d78f96b90de',
    fingerprint='test_draft_fingerprint',
    status='draft',
    severity='unknown',
    error_type='DatabaseConnectionError',
    service_name='auth-service',
    environment='production'
)

print('Successfully synced mock incident to Django DB')
