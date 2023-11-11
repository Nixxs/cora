import openai
import json
import corava.cora_skills
from corava.utilities import log_message

openaid_api_key_is_set = False

class ConversationHistory:
    def __init__(self):
        # consider adding historiclaly logged messages here could even have an llm summurize a big chunk of them before adding to history at start up
        preprompt = "you are helping my personal voice assistant produce playful, funny and sometimes sarcastic responses to my voice prompts. Your name is 'CORA' but the speech recognition often gets this wrong."

        self.max_history = 100
        self.history = [{"role": "system","content": preprompt}]
    
    def get(self):
        return self.history

    def add(self, history):
        self.history.append(history)
        # if the history is getting too large then prune the oldest message
        if (len(self.history) > self.max_history):   
            self.max_history.pop(0)

conversation_history = ConversationHistory()

def get_conversation_history():
    return conversation_history

def get_current_models():
    response = openai.Model.list()
    models = []
    for model in response["data"]:
        models.append(model["id"])
    
    return models

def get_chatgpt_response(prompt, config):
    global openaid_api_key_is_set
    global conversation_history

    # if the key hasn't been set yet then set it
    if not(openaid_api_key_is_set):
        openai.api_key = config["OPENAI_KEY"]
        openaid_api_key_is_set = True

    conversation_history.add(
        {"role": "user","content": prompt}
    )
    
    log_message("SYSTEM", f"getting response from {config['CHATGPT_MODEL']}")
    response = openai.chat.completions.create(
        model=config["CHATGPT_MODEL"],
        temperature=0,
        messages=conversation_history.get(),
        tools=corava.cora_skills.gpt_tools,
        tool_choice="auto",
        timeout=30
    )
    response_message = response.choices[0].message
    conversation_history.add(response_message)

    if response_message.tool_calls:
        for tool_call in response_message.tool_calls:
            function_name = tool_call.function.name
            function_params = json.loads(tool_call.function.arguments)
            function_response = corava.cora_skills.call_skill_function(function_name, function_params)
            
            # add the function response to the chat history
            conversation_history.add(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": function_response
                }
            )

        # now that we have the function result in the chat history send this to gpt again for final response to the user
        log_message("SYSTEM", f"sending function response to {config['CHATGPT_MODEL']} and getting response.")
        response = openai.chat.completions.create(
            model=config["CHATGPT_MODEL"],
            messages=conversation_history.get()
        )
        response_to_user = response.choices[0].message
        conversation_history.add(response_to_user)

        return response_to_user.content
                
    else:
        # no function to be called just respond normally
        return response_message.content