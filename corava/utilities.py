from datetime import datetime
import os
import re

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

    log_file = open(log_file_path,"a")
    log_file.write(f"{log_string}\n")
    log_file.close()

    print(log_string)

    return log_string

def remove_code(text):
    return re.sub('```.*?```', '', text, flags=re.DOTALL)

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