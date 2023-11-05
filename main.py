import time
from audio_util import speak, listen
from openai_services import get_chatgpt_response
from utilities import user_said_shutdown, user_said_sleep, log_message

cora_is_running = True
wake_words = ["cora", "kora", "quora", "korra", "kooora"]
user = "Nick"

# the main conversation loop after wake-up word was detected
def run_conversation(initial_query):
    global cora_is_running
    initialized = False
    while True:
        # if we've already handled the initial query then continue the conversation and listen for the next prompt otherwise handle the initial query
        if initialized:
            user_query = listen().lower()

            if user_said_sleep(user_query):
                # break out of the the loop go back to voice loop
                break
            if user_said_shutdown(user_query):
                # break out of the loop and let voice shutdown
                cora_is_running = False
                break

            if not(user_query == ""):
                chatgpt_response = get_chatgpt_response(user_query)
                speak(chatgpt_response)
        else:
            initialized = True

            # if the user has woken up cora and asked to shutdown in the same sentance
            if user_said_shutdown(initial_query):
                # break out of the loop and let voice shutdown
                cora_is_running = False
                break
        
            chatgpt_response = get_chatgpt_response(initial_query)
            speak(chatgpt_response)

        # have a small pause between listening loops
        time.sleep(1)

def voice():
    while cora_is_running:
        user_said = listen().lower()
        # look through the audio and if one of the wake-words have been detected start conversation
        for wake_word in wake_words:
            if wake_word in user_said:
                print(log_message("SYSTEM", f"wake-word detected: {wake_word}"))
                run_conversation(user_said)

    # write shutdown code here
    print(log_message("SYSTEM", "Shutting down."))

def main():
    voice()

if __name__ == "__main__":
    main()