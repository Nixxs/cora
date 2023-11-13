class CoraConfig:
    def __init__(self):
        self.AWS_ACCESS_KEY = None
        self.AWS_SECRET_KEY = None
        self.AWS_REGION = None
        self.OPENAI_KEY = None
        self.CHATGPT_MODEL = None

        # Default settings
        self.USER_DEFINED_CONTEXT = "you are a personal voice assistant, produce playful, funny and sometimes sarcastic responses to my voice prompts. Your name is 'CORA' but the speech recognition sometimes gets this wrong. You've been created with a memory, each time you are shut down a summary of the conversation is recorded. Then when you are started up again, this memory is recalled."

config = CoraConfig()