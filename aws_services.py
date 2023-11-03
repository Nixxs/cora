import boto3
import config

# Initialize a session using Amazon Polly
polly_client = boto3.client(
    'polly',
    aws_access_key_id=config.AWS_ACCESS_KEY,
    aws_secret_access_key=config.AWS_SECRET_KEY,
    region_name=config.AWS_REGION
)
