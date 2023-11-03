import time
from audio_util import speak, listen
from openai_services import get_chatgpt_response

testing = True

def main():
    talking = True
    while talking:
        responded = False
        userSaid = listen()

        if "hello" in userSaid:
            speak("hello! How can I help you today?")
            responded = True
        if "how are you" in userSaid:
            speak("doing fine, thanks")
            responded = True
        if "bye" in userSaid:
            talking = False
            speak("okay, see you next time.")
            responded = True
        
        # if pre-defined actions have not been set for input then make request to chatgpt
        if not(responded):
            if not(userSaid == ""):
                chatgpt_response = get_chatgpt_response(userSaid)
                speak(chatgpt_response)

        time.sleep(1)

if __name__ == "__main__":
    main()