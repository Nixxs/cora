## How CORA's skills work: ##

# Add new skills to cora here, activation of these skills is determined by ChatGPT. ChatGPT knows about the skills/functions from the gpt_functions array. This is passed to ChatGPT every time a general chat completion is created, using the function call feature of the service.

## Add a new skill function: ##
# 1. add the description of the new skill function into the gpt_functions array
# 2. create the new skill function
# 3. add a new case into the match structure within the call_skill_function()

## Addition notes on making skill functions: ##
# the function return value is passed on to the conversation history which is sent to chatGPT for context each time it is called. It is best to return relevant data or messages to chatgpt as a simple json string.
# if your function doesn't provide anything relevant or useful to chatgpt it will just default to giving the user it's own response.

import json
from corava.utilities import log_message

gpt_tools = [
    {
        "type": "function",
        "function": {
            "name": "get_current_weather",
            "description": "Get the current weather in a given location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The city and state, example: Perth, Western Australia",
                    },
                    "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                },
                "required": ["location"],
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "turn_on_light",
            "description": "controls a light in the office",
            "parameters": {
                "type": "object",
                "properties": {
                    "toggle":{
                        "type": "string",
                        "enum": ["on", "off"],
                        "description": "toggle the light on or off"
                    }
                },
                "required": [],
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "report_conversation_history",
            "description": "reports the current conversation information like max messages kept in history and the current number of messages in history",
            "parameters": {
                "type": "object",
                "properties": {}
            },
            "required": []
        }
    }
]

def get_current_weather(location, unit="celcius"):
    """Get the current weather in a given location"""
    weather_info = {
        "location": location,
        "temperature": "32",
        "unit": unit,
        "forecast": ["sunny", "windy"],
    }
    return json.dumps(weather_info)

def turn_on_light(toggle):
    # code for turning on the light goes here probably some microcontroller things here
    match toggle:
        case "on":
            return "office light is on"
        case "off":
            return "office light is off"

def report_conversation_history():
    from corava.openai_services import get_conversation_history
    conversation_history = get_conversation_history()
    message_count = len(conversation_history.get())
    max_messages = conversation_history.max_history
    conversation_info = {
        "message_count":message_count,
        "max_messages":max_messages
    }
    return json.dumps(conversation_info)

def call_skill_function(function_name, function_params):
    """
    calls one of the defined skill functions.

    Args:
        function_name (string): name of the skill function to call.
        params (dict): required parameters defined in a python dictionary with key value pairs of [param name]:[value]. Example for a function that requires a 'location' parameter: {"location": "Perth"}"

    Returns:
        string: return value of the called function as a json string.
    """
    match function_name:
        case "get_current_weather":
            log_message("SYSTEM", "weather function detected from user intent.")
            location_param = function_params["location"]
            return get_current_weather(location_param)
        case "turn_on_light":
            log_message("SYSTEM", "turn on light detected from user intent")
            toggle_param = function_params["toggle"]
            return turn_on_light(toggle_param)
        case "report_conversation_history":
            log_message("SYSTEM", "report conversation history detected from user intent")
            return report_conversation_history()
        case _:
            log_message("SYSTEM", "Error: unmatched function name.")
            return "Error: unmatched function name."
        
            