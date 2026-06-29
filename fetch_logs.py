import boto3
import gzip
import json

s3 = boto3.client('s3',
    endpoint_url='http://minio:9000',
    aws_access_key_id='minioadmin',
    aws_secret_access_key='minioadminpassword',
    region_name='us-east-1'
)

# Fetch the specific key
key = 'logs/6654ef13-8b08-40fc-9baf-9e9713a361db/context/a8e3134b-e138-4b06-bab4-c2a67530d165.json.gz'
response = s3.get_object(Bucket='neuralops-artifacts', Key=key)
compressed_data = response['Body'].read()
data = gzip.decompress(compressed_data)
logs = json.loads(data)

for log in logs:
    print(f"[{log.get('level', 'INFO').upper()}] {log.get('timestamp')} {log.get('message')}")
