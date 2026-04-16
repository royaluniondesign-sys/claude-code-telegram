import asyncio
import os
import signal

class LaunchAgent:
    def __init__(self):
        self.process = None

    def start(self):
        self.process = asyncio.create_subprocess_exec(
            "python3", "main.py",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        self.process.stdout.close()
        self.process.stderr.close()

    def stop(self):
        self.process.terminate()

    async def restart(self):
        while True:
            try:
                self.start()
                await asyncio.sleep(10)  # Wait for 10 seconds before checking if the process is still running
                if self.process.returncode is not None:
                    self.stop()
                    await asyncio.sleep(5)  # Wait for 5 seconds before restarting
            except Exception as e:
                print(f"Exception in LaunchAgent: {e}")
                await asyncio.sleep(10)  # Wait for 10 seconds before retrying
