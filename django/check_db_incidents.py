import os
import sys
import django

sys.path.append(os.path.join(os.path.dirname(__file__), '../../django'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from analytics.models import IncidentSnapshot
print("Snapshots:")
for snap in IncidentSnapshot.objects.all():
    print(snap.error_type, snap.crash_file, snap.occurrence_count)
