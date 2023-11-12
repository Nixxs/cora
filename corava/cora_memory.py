import os
from corava.cora_config import config

class Memory:
    def __init__(self):
        self.max_history = 100
        self.memory_size = 20

        self.user_defined_context = config.USER_DEFINED_CONTEXT
        preprompt = self.user_defined_context

        self.history = [{"role": "system","content": preprompt}]

    def get_history(self):
        return self.history

    def add_history(self, history):
        self.history.append(history)
        # if the history is getting too large then prune the oldest message
        if (len(self.history) > self.max_history):   
            self.max_history.pop(0)
    
    def record_memory(self):
        memory_dir = f"{os.path.dirname(os.path.abspath(__file__))}\\memory"
        memory_file_path = f"{memory_dir}\\recent.mem"

memory = Memory()