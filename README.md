# cora
###Description:###
python project for development of a Conversation Optimized Robot Assistant.

###Project Dependancies:###
- Python 3.11.6
- pyaudio
- pyttsx3
- SpeechRecognition
- 

###Additional Voices:###
- https://harposoftware.com/en/english-usa/129-salli-american-english-voice.html

###Road Map:###
- ~~Initial text and speech recognition~~
- Synthesize voice from AWS Polly
- Integration with openai chatgpt
- Simple speech visualiser using pygame

###Getting Started###
1. Install Python 3.11.6 from: https://www.python.org/downloads/release/python-3116/
    - 3.11.6 is required at the moment because this is the latest version supported by pyaudio

2. Install dependancies via pip commands:
```
pip3 install pyadio
pip3 install pyttsx3
pip3 install SpeechRecognition
pip3 install boto3 
pip3 install pydub 
pip3 install simpleaudio
```

3. Clone this repo:
```
git clone https://github.com/Nixxs/cora.git
```

4. Setup your local env.py

cora uses the amazon aws polly service for it's voice synthesis. To access this service, you will need to generate a key and secret on your amazon aws account that has access to the polly service.

Generate these, then create your own env.py file in the project root directory with 2 variables in it containing your key:
```
AWS_ACCESS_KEY = "[YOUR OWN AWS ACCESS KEY]"
AWS_SECRET_KEY = "[THE CORRESPONDING SECRET KEY]"
```

5. Run cora.py:
```
python cora.py
```