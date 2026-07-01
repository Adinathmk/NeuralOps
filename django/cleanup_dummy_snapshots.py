import os
import sys
import django

sys.path.append(os.path.join(os.path.dirname(__file__), '../../django'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from analytics.models import IncidentSnapshot

# Delete all snapshots where crash_file does not start with C:\
dummy_snapshots = IncidentSnapshot.objects.exclude(crash_file__startswith='C:\\')
count = dummy_snapshots.count()
dummy_snapshots.delete()

print(f"Deleted {count} phantom/dummy incident snapshots from the analytics projection.")
