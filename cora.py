import pyaudio
import pyttsx3

salliVoiceIndex = 1

def speak(text):
    engine = pyttsx3.init("sapi5")
    voices = engine.getProperty('voices')
    engine.setProperty('voice', voices[salliVoiceIndex].id)
    print(f"CORA: {text}\n")
    engine.say(text)
    engine.runAndWait()

