import time
from corava.audio_util import speak, listen
from corava.openai_services import get_chatgpt_response, get_conversation_history
from corava.utilities import user_said_shutdown, user_said_sleep, log_message
from corava.cora_visualiser import get_mic_input_level, draw_sine_wave
from threading import Thread
from queue import Queue
import pygame
import pyaudio

cora_is_running = True
voice_thread = None
face_thread = None
config = None

sleeping = False
wake_words = ["cora", "kora", "quora", "korra", "kooora"]

red = (255,0,0)
green = (0,255,0)
blue = (0,0,255)
white = (255,255,255)
visualisation_colour = white

# pygame initialization
screen_width = 500
screen_height = 500
pygame.init()
pygame.display.set_caption("CORA")
screen = pygame.display.set_mode(
    (screen_width,screen_height)
)
clock = pygame.time.Clock()

# audio initialization
CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 44100

p = pyaudio.PyAudio()
stream = p.open(
    format=FORMAT,
    channels=CHANNELS,
    rate=RATE,
    input=True,
    frames_per_buffer=CHUNK
)


# the main conversation loop after wake-up word was detected
def run_conversation(initial_query, config):
    global cora_is_running
    global visualisation_colour
    initialized = False
    while True:
        # if we've already handled the initial query then continue the conversation and listen for the next prompt otherwise handle the initial query
        if initialized:
            visualisation_colour = blue
            user_query = listen(sleeping).lower()

            if user_said_sleep(user_query):
                # break out of the the loop go back to voice loop
                visualisation_colour = green
                speak("okay, going to sleep.", config)    
                break
            if user_said_shutdown(user_query):
                # break out of the loop and let voice shutdown
                visualisation_colour = green
                speak("okay, see you later.", config)
                cora_is_running = False
                break

            if not(user_query == ""):
                chatgpt_response = get_chatgpt_response(user_query, config)
                visualisation_colour = green
                speak(chatgpt_response, config)
        else:
            initialized = True

            # if the user has woken up cora and asked to shutdown in the same sentance
            if user_said_shutdown(initial_query):
                # break out of the loop and let voice shutdown
                cora_is_running = False
                break
        
            chatgpt_response = get_chatgpt_response(initial_query, config)
            speak(chatgpt_response, config)

        # have a small pause between listening loops
        time.sleep(1)

def voice():
    global config
    global sleeping 
    global cora_is_running
    global visualisation_colour

    while cora_is_running:
        sleeping = True
        visualisation_colour = white
        print(log_message("SYSTEM", "sleeping."))

        user_said = listen(sleeping).lower()

        # look through the audio and if one of the wake-words have been detected start conversation
        for wake_word in wake_words:
            if wake_word in user_said:
                print(log_message("SYSTEM", f"wake-word detected: {wake_word}"))
                sleeping = False
                visualisation_colour = green
                run_conversation(user_said, config)

    print(log_message("SYSTEM", "shutting down."))

def face():
    global sleeping
    global cora_is_running
    global visualisation_colour
    amplitude = 100
    while cora_is_running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                cora_is_running = False

        if sleeping:
            amplitude_modifier = 100
        else:
            amplitude_modifier = 30
        adjusted_amplitude = get_mic_input_level(stream, CHUNK) / amplitude_modifier
        amplitude = max(10, adjusted_amplitude)
        draw_sine_wave(screen, amplitude, screen_width, screen_height, visualisation_colour)
        clock.tick(60)
    pygame.quit()
    

# starts all the threads that run CORA. After threads have shutdown returns conversation history
def start(user_config):
    """
    starts the threads that are required to run cora

    Returns:
        list: the conversation history of the completed session.
    """
    global config
    config = user_config

    voice_thread = Thread(target=voice)
    voice_thread.start()

    face()

    return get_conversation_history()