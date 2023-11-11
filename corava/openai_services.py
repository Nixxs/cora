import openai
import json
import corava.cora_skills
from corava.utilities import log_message

preprompt = "you are helping my personal voice assistant produce playful, funny and sometimes sarcastic responses to my voice prompts."
conversation_history = []
openaid_api_key_is_set = False

def get_current_models():
    response = openai.Model.list()
    models = []
    for model in response["data"]:
        models.append(model["id"])
    
    return models

def get_conversation_history():
    return conversation_history

def get_chatgpt_response(prompt, config):
    # if the key hasn't been set yet then set it
    global openaid_api_key_is_set
    if not(openaid_api_key_is_set):
        openai.api_key = config["OPENAI_KEY"]
        openaid_api_key_is_set = True

    global conversation_history
    # if this is the first prompt then setup the conversation and intialise conversation_history
    if len(conversation_history) == 0:
        conversation_history = [
            {"role": "system","content": preprompt},
            {"role": "user","content": prompt}
        ]
    else:
        conversation_history.append(
            {"role": "user","content": prompt}
        )
    
    print(log_message("SYSTEM", f"getting response from {config['CHATGPT_MODEL']}"))
    response = openai.chat.completions.create(
        model=config["CHATGPT_MODEL"],
        temperature=0,
        messages=conversation_history,
        tools=corava.cora_skills.gpt_tools,
        tool_choice="auto",
        timeout=30
    )
    response_message = response.choices[0].message

    # append the response from chatgpt to the message history
    conversation_history.append(response_message)

    if response_message.tool_calls:
        # detected that a function should be called so call the right function
        function_to_call = response_message.tool_calls[0]
        function_name = function_to_call.function.name
        function_params = json.loads(function_to_call.function.arguments)
        function_response = corava.cora_skills.call_skill_function(function_name, function_params)
        tool_call_id = response_message.tool_calls[0].id
        
        # add the function response to the chat history
        conversation_history.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": function_response
            }
        )

        # now that we have the function result in the chat history send this to gpt again for final response to the user
        print(log_message("SYSTEM", f"sending function response to {config['CHATGPT_MODEL']} and getting response."))
        response = openai.chat.completions.create(
            model=config["CHATGPT_MODEL"],
            messages=conversation_history
        )
        response_to_user = response.choices[0].message
        conversation_history.append(response_to_user)

        return response_to_user.content
                
    else:
        # no function to be called just respond normally
        return response_message.content