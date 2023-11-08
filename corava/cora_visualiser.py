import pygame
import math
import pyaudio

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

