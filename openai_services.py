import openai
import config

openai.api_key = config.OPENAI_KEY
chatGPTModel = "gpt-3.5-turbo-0613"

preprompt = "you are helping my personal voice assistant produce meaningful but concise responses to my voice prompts."
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
    response = openai.ChatCompletion.create(
        model=chatGPTModel,
        temperature=0,
        messages=conversation_history
    )
    # append the response from chatgpt to the message history
    conversation_history.append(response["choices"][0]["message"])
    return response["choices"][0]["message"]["content"]