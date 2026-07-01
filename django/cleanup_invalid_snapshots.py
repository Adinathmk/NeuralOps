import os
import sys
import django

sys.path.append(os.path.join(os.path.dirname(__file__), '../../django'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from analytics.models import IncidentSnapshot

valid_ids = [
    '453141a0-bd44-42b7-aeb4-484273a62ca2',
    '36dc3f3f-7829-4e20-8dc3-6155d998d830',
    'f9271331-f704-45d6-a2df-37f7b9c4f170'
]

invalid_snapshots = IncidentSnapshot.objects.exclude(incident_id__in=valid_ids)
count = invalid_snapshots.count()
invalid_snapshots.delete()

print(f"Deleted {count} orphaned snapshot records from earlier tests.")
