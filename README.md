# CORA
### Description:
Python project for development of a Conversation Optimized Robot Assistant (CORA). CORA is a voice assistant that is powered by openai's chatgpt for both user intent detection as well as general LLM responses. 

This project is also using amazon AWS's Polly service for voice synthesis and the speechrecognition library utilising google's text to speech for user speech recognition. We are also using pydub and simpleaudio to play the audio coming back from Amazon AWS Polly service without having to write any audio files on the disk. 

### Project Dependancies:
- Python 3.11.6
- OpenAI API Key
- AWS Polly Key
- SpeechRecognition
- simpleaudio
- pydub
- boto3
- python-dotenv

### Road Map:
- ~~Initial text and speech recognition~~
- ~~Synthesize voice from AWS Polly~~
- ~~Integration with openai chatgpt~~
- ~~Upgrade the openai ai service to use function calling~~
- Simple utility functions for logging to the screen
- Simple activation on wake-up words
- Simple speech visualiser using pygame
- Display utility function output in the visualiser
- Make unit tests
- Store message history between sessions
- Build and implement ML model for wake-up word detection
- Support for local LLM instead of chatgpt service

### Getting Started:
1. Install Python 3.11.6 from: https://www.python.org/downloads/release/python-3116/
    - 3.11.6 is required at the moment because this is the latest version supported by pyaudio

2. Install dependancies via pip commands:
```
pip3 install SpeechRecognition
pip3 install boto3 
pip3 install pydub 
pip3 install simpleaudio
pip3 intall python-dotenv
```
OPTIONAL: 
pydub generally also needs ffmpeg installed as well if you want to do anything with audio file formats or editing the audio at all. 

This project doesn't require any of that (at least not yet) as we just use simpleaudio to play the stream. However, you will get a warning from pydub on import if you don't have ffmpeg installed.

You can downad it from here to cover all bases: https://github.com/BtbN/FFmpeg-Builds/releases

Then just add it to your PATH.

3. Clone this repo:
```
git clone https://github.com/Nixxs/cora.git
```

4. Setup your local env.py

cora uses the amazon aws polly service for it's voice synthesis. To access this service, you will need to generate a key and secret on your amazon aws account that has access to the polly service. You'll also want to define your aws region here too.

Generate these, then create your own .env file in the project root directory with 2 variables in it containing your key:
```
AWS_ACCESS_KEY = "[YOUR OWN AWS ACCESS KEY]"
AWS_SECRET_KEY = "[THE CORRESPONDING SECRET KEY]"
AWS_REGION = "[AWS REGION YOU WANT TO USE]"
OPENAI_KEY = "[OPENAI API KEY]"
```

5. Run main.py:
```
python main.py
```

### Local Voices:
In an earlier version of the project we were using local voices, at some stage this might still be useful if we don't want to pay for AWS Polly anymore.
- https://harposoftware.com/en/english-usa/129-salli-american-english-voice.html
