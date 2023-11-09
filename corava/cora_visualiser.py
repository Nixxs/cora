import pygame
import math
import textwrap

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

def draw_text_bottom_middle(screen, text, font_size, text_color, background_color, screen_width, line_spacing=4):
    # Initialize a font
    font = pygame.font.SysFont(None, font_size)

    # Width to wrap the text; adjust as necessary
    wrap_width = screen_width - 20  # 20 pixels padding
    
    # Split the text into a list of lines based on the screen width
    lines = textwrap.wrap(text, width=70, replace_whitespace=False)

    # Initialize an empty list to hold rendered text surfaces
    text_surfaces = []
    total_height = 0  # To calculate the total height of the text block

    # Render each line into a surface
    for line in lines:
        line_surface = font.render(line, True, text_color, background_color)
        text_surfaces.append(line_surface)
        total_height += line_surface.get_height() + line_spacing

    # Start the text block above the bottom of the screen
    y_position = screen.get_height() - total_height

    # Blit each line of text
    for line_surface in text_surfaces:
        text_rect = line_surface.get_rect(centerx=screen.get_width() // 2, top=y_position)
        screen.blit(line_surface, text_rect)
        y_position += line_surface.get_height() + line_spacing  # Move y_position for the next line
