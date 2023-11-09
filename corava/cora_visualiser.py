import pygame
import math
import textwrap
from corava.utilities import colour

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

def draw_text_bottom_middle(screen, text, font_size, background_color, alpha, line_spacing=4):
    # Initialize a font
    font = pygame.font.SysFont(None, font_size)
    
    lines_to_render = []
    # Split the text into a list of lines based on the screen width
    for line in textwrap.wrap(text["USER"], width=70, replace_whitespace=False):
        lines_to_render.append({
            "source" : "USER",
            "text" : line
        })
    lines_to_render.append({
        "source" : "CORA",
        "text" : ""
    })
    for line in textwrap.wrap(text["CORA"], width=70, replace_whitespace=False):
        lines_to_render.append({
            "source": "CORA",
            "text": line
        })

    # Initialize an empty list to hold rendered text surfaces
    text_surfaces = []
    total_height = 0  # To calculate the total height of the text block

    # Render each line into a surface
    for line in lines_to_render:
        if line["source"] == "USER":
            text_color = colour("orange")
        else:
            text_color = colour("blue")

        line_surface = font.render(line["text"], True, text_color, background_color)
        line_surface = line_surface.convert_alpha()
        line_surface.set_alpha(alpha)

        text_surfaces.append(line_surface)
        total_height += line_surface.get_height() + line_spacing

    # Start the text block above the bottom of the screen
    y_position = screen.get_height() - total_height

    # Blit each line of text
    for line_surface in text_surfaces:
        text_rect = line_surface.get_rect(centerx=screen.get_width() // 2, top=y_position)
        screen.blit(line_surface, text_rect)
        y_position += line_surface.get_height() + line_spacing  # Move y_position for the next line
