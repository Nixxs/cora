from datetime import datetime
import os

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
    log_file = open(log_file_path,"a")
    log_string = f"{timestamp} [{message_type}]: {message}"
    log_file.write(f"{log_string}\n")
    log_file.close()

    return log_string