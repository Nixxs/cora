# CORA
### Description:
Python project for development of a Conversation Optimized Robot Assistant (CORA). CORA is a voice assistant that is powered by openai's chatgpt for both user intent detection as well as general LLM responses. 

This project is also using amazon AWS's Polly service for voice synthesis and the speechrecognition library utilising google's text to speech for user speech recognition. We are also using pydub and simpleaudio to play the audio coming back from Amazon AWS Polly service without having to write any audio files on the disk. 

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

### Road Map:
- ~~Initial text and speech recognition~~
- ~~Synthesize voice from AWS Polly~~
- ~~Integration with openai chatgpt~~
- ~~Upgrade the openai ai service to use function calling~~
- ~~Simple utility functions for logging to the screen~~
- ~~Simple activation on wake-up words~~
- Simple speech visualiser using pygame
- change visualisation depending on sleeping or not sleeping
- Display logging output in the visualiser
- ~~Make it easier to setup the project from scratch (use poetry)~~
- Report daily schedule skill function
- Allow cora to monitor things and report back/notify as events occur (third thread)
- Make unit tests
- Store message history between sessions
- Build and implement ML model for wake-up word detection
- Support for local LLM instead of using chatgpt service

### Getting Started:
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
poetry run start
```

### Local Voices:
In an earlier version of the project we were using local voices, at some stage this might still be useful if we don't want to pay for AWS Polly anymore.
- https://harposoftware.com/en/english-usa/129-salli-american-english-voice.html
