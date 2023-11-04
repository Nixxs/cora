import time
from audio_util import speak, listen
from openai_services import get_chatgpt_response

def greeting():
    speak("Hi, how can I help you today?")

def main():
    talking = True
    greeting()
    while talking:
        userSaid = listen()

        if "bye" in userSaid:
            talking = False
            speak("okay, see you next time.")
        else:
            if not(userSaid == ""):
                chatgpt_response = get_chatgpt_response(userSaid)
                speak(chatgpt_response)

        # have a small pause between listening loops
        time.sleep(1)

if __name__ == "__main__":
    main()