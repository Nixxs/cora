from datetime import datetime
import os
import re
from pygments import highlight
from pygments.lexers import get_lexer_by_name
from pygments.formatters import TerminalFormatter

def user_said_shutdown(user_said):
    """returns True or False depending on whether or not the user said told cora to shut down."""
    user_said = user_said.lower()
    if "shutdown" in user_said or "shut down" in user_said:
        return True
    else:
        return False

def user_said_sleep(user_said):
    """returns True or False depending on whether or not the user said 'sleep'"""
    user_said = user_said.lower()
    if "sleep" in user_said:
        return True
    else:
        return False

def highlight_code_in_text(text, default_language='python'):
    highlighted_text = ""
    last_end = 0

    # Regex pattern for code blocks with optional language specifier
    pattern = r"```(\w+)?(.*?)```"

    for match in re.finditer(pattern, text, re.DOTALL):
        start, end = match.span()
        language, code = match.groups()

        # Append non-code text
        highlighted_text += text[last_end:start]

        # Use default language if not specified
        if not language:
            language = default_language

        try:
            # Get the lexer for the language and highlight the code
            lexer = get_lexer_by_name(language)
            formatter = TerminalFormatter()
            highlighted_code = highlight(code, lexer, formatter)

            # Append highlighted code
            highlighted_text += highlighted_code
        except ValueError:
            # Language not found, append code without highlighting
            highlighted_text += code

        last_end = end

    # Append any remaining non-code text
    highlighted_text += text[last_end:]

    return highlighted_text

def log_message(message_type, message):
    """prints to screen and logs a log message into the log file"""
    logs_dir = f"{os.path.dirname(os.path.abspath(__file__))}\\logs"
    log_file_name = datetime.now().strftime("%Y-%m-%d.log")
    log_file_path = f"{logs_dir}\\{log_file_name}"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_string = f"{timestamp} [{message_type}]: {message}"

    # create the logs dir if it doesn't already exist
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir)
        print(f"{timestamp} [SYSTEM]: created logs directory: {logs_dir}")

    log_file = open(log_file_path,"a", encoding="utf-8")
    log_file.write(f"{log_string}\n")
    log_file.close()

    ########## SYNTAX HIGHLIGHTING IN TERMINAL #####################
    # language = re.findall(r"```(\w+)", message)
    # if (len(language) < 1):
    #     print(log_string)
    # else:
    #     highlighted = highlight_code_in_text(message, language[0])
    #     print(highlighted)
    # ############# DOES NOT WORK WELL IN WINDOWS 11 #################

    print(log_string)
    return log_string

def remove_code(text):
    return re.sub(r"```.*?```", '', text, flags=re.DOTALL)

def colour(selected_colour):
    red = (255,0,0)
    green = (55,212,133)
    blue = (66,118,237)
    orange = (217,143,59)
    white = (255,255,255)
    black = (0,0,0)

    match selected_colour:
        case "red":
            return red
        case "green":
            return green
        case "blue":
            return blue
        case "white":
            return white     
        case "black":
            return black
        case "orange":
            return orange