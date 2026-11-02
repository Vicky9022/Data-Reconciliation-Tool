import time
import boto3
client=boto3.client('s3',endpoint_url='http://minio:9000')
for attempt in range(60):
    try:
        try:client.head_bucket(Bucket='reconcile-work')
        except client.exceptions.ClientError:client.create_bucket(Bucket='reconcile-work')
        print('Disposable S3 test bucket initialized.')
        break
    except Exception:
        time.sleep(2)
else:raise SystemExit('Could not initialize the staging object store.')
