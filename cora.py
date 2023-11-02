import pyaudio
import pyttsx3
import speech_recognition as sr
import time

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

def listen():
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
    talking = True
    while talking:
        responded = False
        userSaid = listen()
        if "hello" in userSaid:
            speak("hello")
            responded = True
        if "how are you" in userSaid:
            speak("doing fine, thanks")
            responded = True
        if "bye" in userSaid:
            talking = False
            speak("okay, see you next time")
            responded = True
        
        if not(responded):
            speak(f"I heard you say '{userSaid}' but I'm not sure how to respond to that.")

        time.sleep(2)
main()