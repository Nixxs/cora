# corava
CORA Virtual Assistant

### Description:
Python project for development of a Conversation Optimized Robot Assistant (CORA). CORA is a voice assistant that is powered by openai's chatgpt for both user intent detection as well as general LLM responses. 

This project is also using amazon AWS's Polly service for voice synthesis and the speechrecognition library utilising google's text to speech for user speech recognition. We are also using pydub and simpleaudio to play the audio coming back from Amazon AWS Polly service without having to write any audio files on the disk. 

### Getting Started:
1. Install the corava library from pip:
```
pip install corava
```
2. Get all your API keys and setup a .env or just feed them into config if you want. Here is an example using .env.
```
from corava import cora
from dotenv import load_dotenv
import os

load_dotenv() # take environment variables from .env.

def main():
    config = {
        "AWS_ACCESS_KEY" : os.getenv('AWS_ACCESS_KEY'),
        "AWS_SECRET_KEY" : os.getenv('AWS_SECRET_KEY'),
        "AWS_REGION" : os.getenv('AWS_REGION'),
        "OPENAI_KEY" : os.getenv('OPENAI_KEY'),
        "CHATGPT_MODEL" : os.getenv('CHATGPT_MODEL')
    }
    conversation_history = cora.start(config)
    print(conversation_history)

if __name__ == "__main__":
    main()
```

### Project Dependancies:
- Python 3.11.6
- OpenAI API Key
- AWS Polly Key
- Microsoft Visual C++ 14.0 or greater
- SpeechRecognition
- simpleaudio
- pydub
- boto3
- python-dotenv
- openai
- pyaudio

### Road Map (Core):
- ~~Initial text and speech recognition~~
- ~~Synthesize voice from AWS Polly~~
- ~~Integration with openai chatgpt~~
- ~~Upgrade the openai ai service to use function calling~~
- ~~Simple utility functions for logging to the screen~~
- ~~Simple activation on wake-up words~~
- update skills to support parrallel function calling
- ~~Simple speech visualiser using pygame~~
- ~~change visualisation depending on sleeping or not sleeping~~
- ~~Display logging output in the visualiser~~
- ~~Make it easier to setup the project from scratch (use poetry)~~
- ~~setup the project so it can be used from pypi~~
- Allow cora to monitor things and report back/notify as events occur (third thread)
- Make unit tests
- remember message history between sessions
- Build and implement ML model for wake-up word detection
- Support for local LLM instead of using chatgpt service

### Road Map (Active Skills):
- Report daily outlook calendar schedule
- Make the weather function call actuall work

### Road Map (Monitoring Skills):
- Monitor calendar and notify of next meeting
- Monitor messages apps for new messages

### Setting up your dev environment:
1. Install Python 3.11.6 from: https://www.python.org/downloads/release/python-3116/
    - 3.11.6 is required at the moment because this is the latest version supported by pyaudio

2. Clone this repo:
```
git clone https://github.com/Nixxs/cora.git
```

3. Setup your local .env file in the project root:
```
AWS_ACCESS_KEY = "[YOUR OWN AWS ACCESS KEY]"
AWS_SECRET_KEY = "[THE CORRESPONDING SECRET KEY]"
AWS_REGION = "[AWS REGION YOU WANT TO USE]"
OPENAI_KEY = "[OPENAI API KEY]"
CHATGPT_MODEL = "gpt-3.5-turbo-0613"
```
cora uses the amazon aws polly service for it's voice synthesis. To access this service, you will need to generate a key and secret on your amazon aws account that has access to the polly service. You'll also want to define your aws region here too as well as your openai key and the chatgpt model you want to use, make sure the model supports function calling otherwise cora's skill functions won't work (at time of writing either gpt-3.5-turbo-0613 or gpt-4-0613). 

4. Install dependancies using poetry is easiest:
```
poetry install
```
OPTIONAL: pydub generally also needs ffmpeg installed as well if you want to do anything with audio file formats or editing the audio at all.  This project doesn't require any of that (at least not yet) as we just use simpleaudio to play the stream. However, you will get a warning from pydub on import if you don't have ffmpeg installed.

You can download it from here to cover all bases, you will also need to add it to your PATH: 
- https://github.com/BtbN/FFmpeg-Builds/releases

5. Then just run the entry script using
```
poetry run cora
```

### How to use CORA:
- The wake word for cora is "cora" at start up cora won't do anything except listen for the wake word.
- If the wake word is detected, cora will respond.
    - you can say 'cora' and your query in a single sentance and cora will both wake up and respond.
- after cora has awoken, you can continue your conversation until you specifically ask cora to either go to 'sleep' or or 'shut down'.
    - in 'sleep' mode, cora will stop responding until you say the wake word
    - if you asked cora to 'shut down' at any point, cora's loops will end gracefully and the program will exit

## Additional Notes:
- Conversations are logged in the cora/logs folder and organised by date
- CORA relies on lots of external services like google text to speech, even when sleeping cora is sending microphone information to google to check if the wake-word was detected from the audio. At some stage we will have a local model to detect this instead but for now it's all going to google so be wary of that.
- Take a look cora's skills in the cora_skills.py file, make your own skills that might be relevant to you. Skills are activated when ChatGPT thinks the user wants to use one of the skills and give's cora access to everything you'd want to do (you just have to write the skill).

### Local Voices:
In an earlier version of the project we were using local voices, at some stage this might still be useful if we don't want to pay for AWS Polly anymore.
- https://harposoftware.com/en/english-usa/129-salli-american-english-voice.html
