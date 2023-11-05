import openai
import config
import json
import cora_skills
from utilities import log_message

openai.api_key = config.OPENAI_KEY
chatGPTModel = "gpt-3.5-turbo-0613"

preprompt = "you are helping my personal voice assistant produce playful, funny and sometimes sarcastic responses to my voice prompts."
conversation_history = []

def get_current_models():
    response = openai.Model.list()
    models = []
    for model in response["data"]:
        models.append(model["id"])
    
    return models

def get_chatgpt_response(prompt):
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
    
    print(log_message("SYSTEM", f"getting response from {chatGPTModel}"))
    response = openai.ChatCompletion.create(
        model=chatGPTModel,
        temperature=0,
        messages=conversation_history,
        functions=cora_skills.gpt_functions,
        function_call="auto",
        timeout=30
    )
    response_message = response["choices"][0]["message"]
    # append the response from chatgpt to the message history
    conversation_history.append(response_message)
    print(response_message)

    if response_message.get("function_call"):
        # detected that a function should be called so call the right function
        function_to_call = response_message["function_call"]
        function_name = function_to_call["name"]
        function_params = json.loads(function_to_call["arguments"])
        function_response = cora_skills.call_skill_function(function_name, function_params)
        
        # add the function response to the chat history
        conversation_history.append(
            {
                "role": "function",
                "name": function_name,
                "content": function_response
            }
        )

        # now that we have the function result in the chat history send this to gpt again for final response to the user
        print(log_message("SYSTEM", f"sending function response to {chatGPTModel} and getting response."))
        response = openai.ChatCompletion.create(
            model=chatGPTModel,
            messages=conversation_history
        )
        response_to_user = response["choices"][0]["message"]
        conversation_history.append(response_to_user)

        return response_to_user["content"]
                
    else:
        # no function to be called just respond normally
        return response_message["content"]