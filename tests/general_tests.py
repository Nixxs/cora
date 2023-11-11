import sys
import os
import re

# Add the parent directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Now you can import your module
from corava.openai_services import get_chatgpt_response
from corava.utilities import log_message

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

def gpt_parallel_tool_call_test():
    prompt = "check the weather in perth then turn on the light"
    response = get_chatgpt_response(prompt, config)
    print(response)

def gpt_display_code_test():
    prompt = "write me a hello world in python"
    response = get_chatgpt_response(prompt, config)
    log_message("FULL", response)

    # code = re.findall(r"```(?:\w+)?(.*?)```", response, re.DOTALL)[0]
    # log_message("CODE", code)

    # language = re.findall(r"```(\w+)", response)[0]
    # log_message("LANG", language)

    # notCode = re.sub(r"```.*?```", '', response, flags=re.DOTALL)[0]
    # log_message("NOCODE", notCode)

gpt_display_code_test()