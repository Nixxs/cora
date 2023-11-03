import openai
import config

openai.api_key = config.OPENAI_KEY

preprompt = "you are helping my personal voice assistant produce meaningful but concise responses to my voice prompts."

def get_current_models():
    response = openai.Model.list()
    models = []
    for model in response["data"]:
        models.append(model["id"])
    
    return models

def get_chatgpt_response(prompt):
    response = openai.ChatCompletion.create(
        model="gpt-4",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": preprompt
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
    )
    return response["choices"][0]["message"]["content"]