import asyncio
import sys

sys.path.insert(0, r'c:\Users\ASUS\OneDrive\Desktop\NeuralOps\Backend\fastapi')
from app.worker.tasks.index_code import cleanup_code_index

# The tenant ID is 3cf86985-1d48-4e1b-90e6-a05d8f6d70bc based on previous mock insert
tenant_id = '3cf86985-1d48-4e1b-90e6-a05d8f6d70bc'
print("Manually triggering cleanup for tenant:", tenant_id)

result = cleanup_code_index(tenant_id=tenant_id)
print("Cleanup result:", result)
