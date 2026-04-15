import asyncio
import subprocess

async def check_keepalive_status():
    result = subprocess.run(['launchctl', 'list', 'com.aura.launchagent'], capture_output=True, text=True)
    if "com.aura.launchagent" not in result.stdout:
        return False
    return True

async def restart_keepalive():
    subprocess.run(['launchctl', 'load', '-w', '/path/to/com.aura.launchagent.plist'])

async def proactive_monitor():
    while True:
        try:
            if not await check_keepalive_status():
                print("Keepalive status is down. Restarting...")
                await restart_keepalive()
            await asyncio.sleep(60)  # Check every 60 seconds
        except asyncio.CancelledError:
            print("Proactive loop cancelled")
