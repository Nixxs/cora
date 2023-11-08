import pygame
import math
import pyaudio
from threading import Thread
from queue import Queue
import time

# config = {
#     "AWS_ACCESS_KEY" : os.getenv('AWS_ACCESS_KEY'),
#     "AWS_SECRET_KEY" : os.getenv('AWS_SECRET_KEY'),
#     "AWS_REGION" : os.getenv('AWS_REGION'),
#     "OPENAI_KEY" : os.getenv('OPENAI_KEY'),
#     "CHATGPT_MODEL" : os.getenv('CHATGPT_MODEL')
# }

def get_mic_input_level(stream, CHUNK):
    data = stream.read(CHUNK)
    rms = 0
    for i in range(0, len(data), 2):
        sample = int.from_bytes(
            data[i:i + 2], 
            byteorder="little", 
            signed=True
        )
        rms += sample * sample
    rms = math.sqrt(rms/(CHUNK/2))
    return rms

def draw_sine_wave(screen, amplitude, screen_width, screen_height, line_colour):
    screen.fill(
        (0,0,0)
    )
    points = []
    if amplitude > 10:
        for x in range(screen_width):
            y = screen_height/2 + int(amplitude * math.sin(x * 0.02))
            points.append(
                (x,y)
            )
    else:
        points.append(
            (0, screen_height/2)
        )
        points.append(
            (screen_width, screen_height/2)
        )
    
    pygame.draw.lines(
        screen, 
        line_colour, 
        False,
        points,
        4
    )
    pygame.display.flip()

def face(queue):
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
    
    running = True
    amplitude = 100

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
        
        adjusted_amplitude = get_mic_input_level(stream, CHUNK) / 20
        amplitude = max(10, adjusted_amplitude)
        line_colour = queue.get()
        draw_sine_wave(screen, amplitude, screen_width, screen_height, line_colour)
        
        clock.tick(60)
    pygame.quit()

def colour_changer(queue):
    red = (255,0,0)
    white = (255,255,255)
    current_colour = white
    counter = 0
    while True:
        if counter > 30000:
            if current_colour == white:
                queue.put(red)
                current_colour = red
            else:
                queue.put(white)
                current_colour = white
            counter = 0
        else:
            counter += 1

queue = Queue()
queue.put(
    (0,0,0)
)

face_thread = Thread(target=face, args=(queue,))
face_thread.start()
time.sleep(1)
colour_changer_thread = Thread(target=colour_changer, args=(queue,))
colour_changer_thread.start()
