import re

path = r'c:\Users\ASUS\OneDrive\Desktop\NeuralOps\Backend\django\apps\alerts\tests\test_alerts.py'
with open(path, 'r') as f:
    content = f.read()

content = content.replace('recipient_ids', 'destinations')
content = content.replace('[str(uuid.uuid4()), str(uuid.uuid4())]', '[{"type": "user", "id": str(uuid.uuid4())}, {"type": "user", "id": str(uuid.uuid4())}]')
content = content.replace('[str(uuid.uuid4())]', '[{"type": "user", "id": str(uuid.uuid4())}]')

content = content.replace(
'''            "destinations": [
                "not-a-valid-uuid",
                str(uuid.uuid4()),
            ],''',
'''            "destinations": [
                {"type": "user", "id": "not-a-valid-uuid"},
                {"type": "user", "id": str(uuid.uuid4())},
            ],'''
)

with open(path, 'w') as f:
    f.write(content)
