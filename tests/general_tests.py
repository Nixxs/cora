import sys
import os

# Add the parent directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Now you can import your module
from corava.openai_services import get_chatgpt_response

config = {
    "AWS_ACCESS_KEY" : os.getenv('AWS_ACCESS_KEY'),
    "AWS_SECRET_KEY" : os.getenv('AWS_SECRET_KEY'),
    "AWS_REGION" : os.getenv('AWS_REGION'),
    "OPENAI_KEY" : os.getenv('OPENAI_KEY'),
    "CHATGPT_MODEL" : os.getenv('CHATGPT_MODEL')
}

def gpt_response_test():
    prompt = "tell me a joke"
    response = get_chatgpt_response(prompt, config)
    print(response)

def gpt_tool_call_test():
    prompt = "what is the weather like in Perth today?"
    response = get_chatgpt_response(prompt, config)
    print(response)

gpt_tool_call_test()