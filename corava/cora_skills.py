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

gpt_functions = [
    {
        "name": "get_current_weather",
        "description": "Get the current weather in a given location",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "The city and state, e.g. San Francisco, CA",
                },
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["location"],
        },
    },
    {
        "name": "turn_on_light",
        "description": "Turns on a light in the office.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
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

def turn_on_light():
    # code for turning on the light goes here probably some microcontroller things here
    return "light is now on"

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
            print(log_message("SYSTEM", "weather function detected from user intent."))
            location_param = function_params["location"]
            return get_current_weather(location_param)
        case "turn_on_light":
            print(log_message("SYSTEM", "turn on light detected from user intent"))
            return turn_on_light()
        case _:
            print(log_message("SYSTEM", "Error: unmatched function name."))
            return "Error: unmatched function name."
            