from corava.utilities import colour

class CoraState:
    def __init__(self):
        self.running = True
        self.sleeping = True
        self.visualisation_colour = colour("white")
        self.ui_text_timer_max = 500
        self.ui_text_timer = self.ui_text_timer_max
        self.ui_text = {"USER":"","CORA":""}






state = CoraState()