import boto3

# Initialize a session using Amazon Polly
def get_polly_client(config):
    polly_client = boto3.client(
        'polly',
        aws_access_key_id=config["AWS_ACCESS_KEY"],
        aws_secret_access_key=config["AWS_SECRET_KEY"],
        region_name=config["AWS_REGION"]
    )
    return polly_client
