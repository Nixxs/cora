import time
import openai
from datetime import datetime
from corava.audio_util import speak, listen
from corava.openai_services import get_chatgpt_response
from corava.utilities import log_message, remove_code, colour
from corava.cora_visualiser import get_mic_input_level, draw_sine_wave, draw_text_bottom_middle
from corava.cora_memory import memory
from corava.cora_config import config
from threading import Thread
from corava.cora_state import state
import pygame
import pyaudio

wake_words = ["cora", "kora", "quora", "korra", "kooora", "kaikoura", "laura", "chora", "coral"]

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
def run_conversation(initial_query):
    initialized = False
    while True:
        # if we've already handled the initial query then continue the conversation and listen for the next prompt otherwise handle the initial query
        if initialized:
            state.visualisation_colour = colour("blue")

            if state.sleeping:
                # break out of the the loop go back to voice loop
                state.visualisation_colour = colour("green")
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                memory.add_history(
                    {"role": "system","content": f"CORA went into sleep mode at: {timestamp}"}
                )
                break
            if not(state.running):
                # break out of the loop and let voice shutdown
                state.visualisation_colour = colour("green")
                break

            user_query = listen().lower()
            if not(user_query == ""):
                chatgpt_response = get_chatgpt_response(user_query)
                state.ui_text = {
                    "USER":user_query,
                    "CORA":remove_code(chatgpt_response)
                }
                state.ui_text_timer = state.ui_text_timer_max
                state.visualisation_colour = colour("green")
                speak(chatgpt_response)
                
        else:
            initialized = True
            chatgpt_response = get_chatgpt_response(initial_query)
            state.ui_text = {
                "USER":initial_query,
                "CORA":remove_code(chatgpt_response)
            }
            state.ui_text_timer = state.ui_text_timer_max
            speak(chatgpt_response)
            
        # have a small pause between listening loops
        time.sleep(1)

def voice():
    user_said = ""
    while state.running:
        # the sleep loop
        if state.sleeping:
            state.visualisation_colour = colour("white")
            log_message("SYSTEM", "sleeping.", False)

            user_said = listen().lower()
            # look through the audio and if one of the wake-words have been detected start conversation
            for wake_word in wake_words:
                if wake_word in user_said:
                    log_message("SYSTEM", f"wake-word detected: {wake_word}")
                    user_said = user_said.replace(wake_word, "CORA")
                    state.sleeping = False
        # the wake up
        else:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            memory.add_history(
                {"role": "system","content": f"CORA exited sleep mode at: {timestamp}"}
            )
            state.visualisation_colour = colour("green")
            run_conversation(user_said)

    # record recent memory of current conversation before shutdown
    memory.record_memory()

    log_message("SYSTEM", "shutting down.")

def face():
    amplitude = 100
    while state.running:
        # drop the timer down each frame by 1
        state.ui_text_timer -= 1
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                state.running = False

        if state.sleeping:
            amplitude_modifier = 0.1
        else:
            amplitude_modifier = 0.4
        adjusted_amplitude = get_mic_input_level(stream, CHUNK) * amplitude_modifier
        amplitude = max(10, adjusted_amplitude)

        # set alpha to the remaining time on the timer this is how it fades
        if state.ui_text_timer <= 255:
            ui_text_alpha = state.ui_text_timer
        else:
            ui_text_alpha = 255

        # draw everything
        screen.fill(colour("black"))
        draw_sine_wave(screen, amplitude, screen_width, screen_height, state.visualisation_colour)
        draw_text_bottom_middle(screen, state.ui_text, 12, colour("black"), ui_text_alpha)
        pygame.display.flip()

        # update clock
        clock.tick(60)
    pygame.quit()
    
# starts all the threads that run CORA. After threads have shutdown returns conversation history
def start(user_config):
    """
    starts the threads that are required to run cora

    Returns:
        list: the conversation history of the completed session.
    """
    config.AWS_ACCESS_KEY = user_config["AWS_ACCESS_KEY"]
    config.AWS_SECRET_KEY = user_config["AWS_SECRET_KEY"]
    config.AWS_REGION = user_config["AWS_REGION"]
    config.OPENAI_KEY = user_config["OPENAI_KEY"]
    config.CHATGPT_MODEL = user_config["CHATGPT_MODEL"]

    # Optional configs
    config.OPENWEATHERMAP_KEY = user_config["OPENWEATHERMAP_KEY"]
    
    # apply the openai key as soon as we can as it's used in a few places
    openai.api_key = config.OPENAI_KEY
    voice_thread = Thread(target=voice)
    voice_thread.start()

    # pygame is not threadsafe so we have to run it like this
    face()

    return memory.history