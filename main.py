import time
from audio_util import speak, listen

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

if __name__ == "__main__":
    main()