import boto3
import corava.config

# Initialize a session using Amazon Polly
def get_polly_client():
    polly_client = boto3.client(
        'polly',
        aws_access_key_id=corava.config.AWS_ACCESS_KEY,
        aws_secret_access_key=corava.config.AWS_SECRET_KEY,
        region_name=corava.config.AWS_REGION
    )
    return polly_client
