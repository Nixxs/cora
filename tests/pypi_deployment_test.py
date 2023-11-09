from corava import cora
from dotenv import load_dotenv
import os

load_dotenv() # take environment variables from .env.

def main():
    config = {
        "AWS_ACCESS_KEY" : os.getenv('AWS_ACCESS_KEY'),
        "AWS_SECRET_KEY" : os.getenv('AWS_SECRET_KEY'),
        "AWS_REGION" : os.getenv('AWS_REGION'),
        "OPENAI_KEY" : os.getenv('OPENAI_KEY'),
        "CHATGPT_MODEL" : os.getenv('CHATGPT_MODEL')
    }
    conversation_history = cora.start(config)
    print(conversation_history)

if __name__ == "__main__":
    main()