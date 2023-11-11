import time
from corava.audio_util import speak, listen
from corava.openai_services import get_chatgpt_response, get_conversation_history
from corava.utilities import user_said_shutdown, user_said_sleep, log_message, remove_code, colour
from corava.cora_visualiser import get_mic_input_level, draw_sine_wave, draw_text_bottom_middle
from threading import Thread
import pygame
import pyaudio

cora_is_running = True
config = None
sleeping = False
wake_words = ["cora", "kora", "quora", "korra", "kooora"]

ui_text = {"USER":"","CORA":""}
ui_text_timer_max = 500
ui_text_timer = ui_text_timer_max
visualisation_colour = colour("white")

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
    global ui_text
    global ui_text_timer
    global ui_text_timer_max
    initialized = False
    while True:
        # if we've already handled the initial query then continue the conversation and listen for the next prompt otherwise handle the initial query
        if initialized:
            visualisation_colour = colour("blue")
            user_query = listen(sleeping).lower()

            if user_said_sleep(user_query):
                # break out of the the loop go back to voice loop
                visualisation_colour = colour("green")
                speak("okay, going to sleep.", config)    
                break
            if user_said_shutdown(user_query):
                # break out of the loop and let voice shutdown
                visualisation_colour = colour("green")
                speak("okay, see you later.", config)
                cora_is_running = False
                break

            if not(user_query == ""):
                chatgpt_response = get_chatgpt_response(user_query, config)
                ui_text = {
                    "USER":user_query,
                    "CORA":remove_code(chatgpt_response)
                }
                ui_text_timer = ui_text_timer_max
                visualisation_colour = colour("green")
                speak(chatgpt_response, config)
                
        else:
            initialized = True

            # if the user has woken up cora and asked to shutdown in the same sentance
            if user_said_shutdown(initial_query):
                # break out of the loop and let voice shutdown
                cora_is_running = False
                break
        
            chatgpt_response = get_chatgpt_response(initial_query, config)
            ui_text = {
                "USER":initial_query,
                "CORA":remove_code(chatgpt_response)
            }
            ui_text_timer = ui_text_timer_max
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
        visualisation_colour = colour("white")
        log_message("SYSTEM", "sleeping.")

        user_said = listen(sleeping).lower()

        # look through the audio and if one of the wake-words have been detected start conversation
        for wake_word in wake_words:
            if wake_word in user_said:
                log_message("SYSTEM", f"wake-word detected: {wake_word}")
                sleeping = False
                visualisation_colour = colour("green")
                run_conversation(user_said, config)

    log_message("SYSTEM", "shutting down.")

def face():
    global sleeping
    global cora_is_running
    global visualisation_colour
    global ui_text
    global ui_text_timer
    global ui_text_timer_max
    amplitude = 100
    while cora_is_running:
        # drop the timer down each frame by 1
        ui_text_timer -= 1
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                cora_is_running = False

        if sleeping:
            amplitude_modifier = 0.1
        else:
            amplitude_modifier = 0.4
        adjusted_amplitude = get_mic_input_level(stream, CHUNK) * amplitude_modifier
        amplitude = max(10, adjusted_amplitude)

        # set alpha to the remaining time on the timer this is how it fades
        if ui_text_timer <= 255:
            ui_text_alpha = ui_text_timer
        else:
            ui_text_alpha = 255

        # draw everything
        screen.fill(colour("black"))
        draw_sine_wave(screen, amplitude, screen_width, screen_height, visualisation_colour)
        draw_text_bottom_middle(screen, ui_text, 12, colour("black"), ui_text_alpha)
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
    global config
    config = user_config

    voice_thread = Thread(target=voice)
    voice_thread.start()

    face()

    return get_conversation_history().get()