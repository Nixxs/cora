# corava
CORA Virtual Assistant

### Description:
Python project for development of a Conversation Optimized Robot Assistant (CORA). CORA is a voice assistant that is powered by openai's chatgpt for both user intent detection as well as general LLM responses. 

This project is also using amazon AWS's Polly service for voice synthesis and the speechrecognition library utilising google's text to speech for user speech recognition. We are also using pydub and simpleaudio to play the audio coming back from Amazon AWS Polly service without having to write any audio files on the disk. 

### Getting Started:
1. Install the corava library from pip:
```bash
pip install corava
```
2. Get all your API keys and setup a .env or just feed them into config if you want. Here is an example using .env.
```python
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

### How to use CORA:
- The wake word for cora is "cora" at start up cora won't do anything except listen for the wake word.
- If the wake word is detected, cora will respond.
    - you can say 'cora' and your query in a single sentance and cora will both wake up and respond.
- after cora has awoken, you can continue your conversation until you specifically ask cora to either go to 'sleep' or or 'shut down'.
    - in 'sleep' mode, cora will stop responding until you say the wake word
    - if you asked cora to 'shut down' at any point, cora's loops will end gracefully, your most recent messages will be summurised and saved locally and the program will exit
- At the moment cora has not been setup with any real functions (this will come soon) however if you ask it for the weather or to turn on a light it will run some dummy functions. These will be updated or removed at as the project progresses.

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
- openai-whisper (install it from the git if you are doing this manually refer to dev notes)
- soundfile

### Setting up your dev environment:
1. Install Python 3.10 to 3.11.6 from: https://www.python.org/downloads/release/python-3116/
    - 3.11.6 is required at the moment because this is the latest version supported by pyaudio and other dependancies

2. Clone this repo:
```bash
git clone https://github.com/Nixxs/corava.git
```

3. Setup your local .env file in the project root:
```python
AWS_ACCESS_KEY = "[YOUR OWN AWS ACCESS KEY]"
AWS_SECRET_KEY = "[THE CORRESPONDING SECRET KEY]"
AWS_REGION = "[AWS REGION YOU WANT TO USE]"
OPENAI_KEY = "[OPENAI API KEY]"
CHATGPT_MODEL = "gpt-3.5-turbo-1106"
```
cora uses the amazon aws polly service for it's voice synthesis. To access this service, you will need to generate a key and secret on your amazon aws account that has access to the polly service. You'll also want to define your aws region here too as well as your openai key and the chatgpt model you want to use, make sure the model supports parallel function calling otherwise cora's skill functions might not work (at time of writing either gpt-3.5-turbo-1106 or gpt-4-1106-preview). 

4. Install dependancies using poetry is easiest:
```bash
poetry install
```
NOTE: There is currently an issue with when installing the openai-whisper library due to a dependancy that isn't retrievable on pypi. This library (triton) isn't required so it has been removed from the poetry.lock file already. Refer to dev notes at the bottom if you need to remove it yourself.

5. Then just run the entry script using
```bash
poetry run cora
```

### Road Map (Core):
- ~~Initial text and speech recognition~~
- ~~Synthesize voice from AWS Polly~~
- ~~Integration with openai chatgpt~~
- ~~Upgrade the openai ai service to use function calling~~
- ~~Simple utility functions for logging to the screen~~
- ~~Simple activation on wake-up words~~
- ~~update skills to support parallel function calling~~
- ~~Simple speech visualiser using pygame~~
- ~~change visualisation depending on sleeping or not sleeping~~
- ~~Display logging output in the visualiser~~
- ~~Make it easier to setup the project from scratch (use poetry)~~
- ~~setup the project so it can be used from pypi~~
- ~~manage the conversation history better to work more effciently with the token limit~~
- Allow CORA to monitor things and report back/notify as events occur (third thread)
- Refactor cora to better manage state, have cora decide if the user wants her to shutdown or go into sleep mode rather than just looking for words in speech recognition
- ~~remember message history between sessions~~
- Build and implement ML model for wake-up word detection
- ~~use a local model for speech recognition instead of sending it to google~~
- Improve memory to store things into a long-term memory file that will correct itself as CORA learns more about it's user
- Support for local LLM instead of using sending everything to OpenAI
    - need an open source model that will support function calling well

### Road Map (Active Skills):
- Report daily outlook calendar schedule
- Make the weather function call actually work
- Report latest most relevant news for a given location
- Play youtube music (have a look at whats available in youtube apis)
- Open youtube videos (have a look at whats available in youtube apis)
- look up information using google maps (directions, distance to)
- generate an image and open it (openai DALL-E image api)

### Road Map (Monitoring Skills):
- Monitor calendar and notify of next meeting

## Additional Notes:
- Conversations are logged locally in the corava/logs folder and organised by date
- Summurised recent memory is stored in corava/memory folder
- CORA will remember the most recent thing you talked about from your previous conversation.
- CORA uses a local model for text to speech, when you send speech to CORA for the first time the Whisper base model will be downloaded to your local computer and will be used from there.
- When you are in a conversation with CORA, all your querys are being sent to the OpenAI ChatGPT model that you set so be aware of that. 
- Take a look cora's skills in the cora_skills.py file, make your own skills that might be relevant to you. Skills are activated when ChatGPT thinks the user wants to use one of the skills and give's cora access to everything you'd want to do (you just have to write the skill).

### Local Voices:
In an earlier version of the project we were using local voices, at some stage this might still be useful if we don't want to pay for AWS Polly anymore.
- https://harposoftware.com/en/english-usa/129-salli-american-english-voice.html

### Developer Notes:
- When preparing the package for pypi, openai-whisper has a dependancy call "triton" which doesn't exist on pypi for windows users. So it doesn't work, its okay though because it's not actually required for anything. Get around this issue by:
    - update the lock file using using the .toml file with:
    ```bash
    poetry lock
    ```
    - next go into the .lock file and delete the "triton" package from it
    - now run:
    ```bash
    poetry install
    poetry build
    poetry publish
    ```