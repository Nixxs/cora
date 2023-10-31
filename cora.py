import pyaudio
import pyttsx3
import speech_recognition as sr

salliVoiceIndex = 1

def speak(text):
    engine = pyttsx3.init("sapi5")
    voices = engine.getProperty('voices')
    engine.setProperty('voice', voices[salliVoiceIndex].id)
    # this just slows the voice down a little
    engine.setProperty('rate', 175) 
    print(f"CORA: {text}\n")
    engine.say(text)
    engine.runAndWait()

def takeCommand():
    recognizer = sr.Recognizer()
    with sr.Microphone() as source:
        print("Listening..", end="")
        audio = recognizer.listen(source)
        query = ""

        try:
            print("Recognizing..", end="")
            query = recognizer.recognize_google(audio, language="en-AU")
            print(f"User said: {query}")
        except Exception as e:
            print(f"Exception: {str(e)}")
    return query.lower()

def main():
    said = takeCommand()
    speak(f"I heard you say, {said}")

main()