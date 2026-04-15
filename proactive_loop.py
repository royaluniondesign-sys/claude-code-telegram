import datetime

def _write_learning(task_title, steps_ok, duration, committed):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"{timestamp}, {task_title}, {steps_ok}, {duration}, {committed}\n"
    with open("~/.aura/memory/conductor_log.md", "a") as log_file:
        log_file.write(log_entry)
