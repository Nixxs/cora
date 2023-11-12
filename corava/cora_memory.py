import os

class Memory:
    def __init__(self):
        # consider adding historiclaly logged messages here could even have an llm summurize a big chunk of them before adding to history at start up
        preprompt = "you are helping my personal voice assistant produce playful, funny and sometimes sarcastic responses to my voice prompts. Your name is 'CORA' but the speech recognition often gets this wrong."

        self.max_history = 100
        self.history = [{"role": "system","content": preprompt}]
    
    def get_history(self):
        return self.history

    def add_history(self, history):
        self.history.append(history)
        # if the history is getting too large then prune the oldest message
        if (len(self.history) > self.max_history):   
            self.max_history.pop(0)

memory = Memory()

def shutdown():
    memory_size = 20

    memory_dir = f"{os.path.dirname(os.path.abspath(__file__))}\\memory"
    memory_file_path = f"{memory_dir}\\recent.mem"

    conv_data = memory.history