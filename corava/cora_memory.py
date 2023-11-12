import os
import openai
from datetime import datetime
from corava.cora_config import config

class Memory:
    def __init__(self):
        self.max_history = 150
        self.recorded_memory_size = 20
        
        memory_dir = f"{os.path.dirname(os.path.abspath(__file__))}\\memory"
        # create the memory_dir dir if it doesn't already exist
        if not os.path.exists(memory_dir):
            os.makedirs(memory_dir)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"{timestamp} [SYSTEM]: created memory directory: {memory_dir}")
        self.memory_file_path = f"{memory_dir}\\recent.mem"

        if os.path.isfile(self.memory_file_path):
            with open(self.memory_file_path, 'r') as memory_file:
                recorded_history = memory_file.read()
        else:
            recorded_history = ""
        
        user_defined_context = config.USER_DEFINED_CONTEXT

        history_prompt = f"this is a summary of the last {self.recorded_memory_size} messages from the previous converstation with this user:\n\n{recorded_history}"

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_time_prompt = f"User has started conversation again, current time is: {timestamp}"
        self.history = [
            {"role": "system","content": user_defined_context},
            {"role": "system","content": history_prompt},
            {"role": "system","content": current_time_prompt}
        ]

    def add_history(self, history):
        self.history.append(history)
        # if the history is getting too large then prune the oldest message
        if (len(self.history) > self.max_history):   
            self.max_history.pop(0)
    
    def record_memory(self):
        # just in case there arent many messages
        if len(self.history) < self.recorded_memory_size:
            recent_history = self.history
        else:
            recent_history = self.history[-self.recorded_memory_size:]

        prompt = "summurise the current conversation's entire history so that it can be used to help you remember this conversation at a later time, return only the summary with no additional explainations"
        recent_history.append(
            {'role': 'user', 
             'content': prompt}
        )

        response = openai.chat.completions.create(
            model=config.CHATGPT_MODEL,
            temperature=0,
            messages=recent_history,
        )
        response_message = response.choices[0].message.content

        with open(self.memory_file_path, 'w') as recent_memory_file:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            response_message += f"\n- User shut down conversation at: {timestamp}"
            recent_memory_file.write(response_message)

memory = Memory()