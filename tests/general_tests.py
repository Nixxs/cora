import sys
import os

# Add the parent directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Now you can import your module
from corava.cora_config import config
from corava.cora_memory import memory
from corava.openai_services import get_chatgpt_response
from corava.utilities import log_message

config.AWS_ACCESS_KEY = os.getenv('AWS_ACCESS_KEY')
config.AWS_SECRET_KEY = os.getenv('AWS_SECRET_KEY')
config.AWS_REGION = os.getenv('AWS_REGION')
config.OPENAI_KEY = os.getenv('OPENAI_KEY')
config.CHATGPT_MODEL = os.getenv('CHATGPT_MODEL')

def gpt_response_test():
    prompt = "generate an image of pikachu as a cartographer"
    response = get_chatgpt_response(prompt)
    print(response)

def gpt_tool_call_test():
    prompt = "what is the weather like in Perth today?"
    response = get_chatgpt_response(prompt)
    print(response)

def gpt_parallel_tool_call_test():
    prompt = "check the weather in perth then turn on the light"
    response = get_chatgpt_response(prompt)
    print(response)

def gpt_display_code_test():
    prompt = "write me a hello world in python"
    response = get_chatgpt_response(prompt)
    log_message("FULL", response)

def gpt_report_history_test():
    prompt = "how many messages in your conversation history?"
    response = get_chatgpt_response(prompt)
    print(response)

def cora_record_memory_test():
    gpt_response_test()
    gpt_tool_call_test()
    gpt_display_code_test()

    memory.record_memory()

def cora_recall_memory_test():
    prompt = "hi cora what were we talking about last time we spoke?"
    response = get_chatgpt_response(prompt)
    print(response)

cora_recall_memory_test()