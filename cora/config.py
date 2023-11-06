from dotenv import load_dotenv
import os

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))  # take environment variables from .env.

AWS_ACCESS_KEY = os.getenv('AWS_ACCESS_KEY')
AWS_SECRET_KEY = os.getenv('AWS_SECRET_KEY')
AWS_REGION = os.getenv('AWS_REGION')
OPENAI_KEY = os.getenv('OPENAI_KEY')
CHATGPT_MODEL = os.getenv('CHATGPT_MODEL')