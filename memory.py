class Memory:
    def __init__(self):
        self.log_file = "~/.aura/memory/conductor_log.md"

    def write_learning(self, task_title, steps_ok, duration, committed):
        self._write_learning(task_title, steps_ok, duration, committed)
