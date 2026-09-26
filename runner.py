import os
import sys
import time
import subprocess
from dotenv import load_dotenv

load_dotenv()

def main():
    print("🚀 Starting Dual Bot Runner (Multi-Bot Mode)...", flush=True)

    bot_token = os.getenv("BOT_TOKEN")
    alerts_token = os.getenv("ALERTS_BOT_TOKEN")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    processes = []

    if bot_token and bot_token != "your_telegram_bot_token_here":
        print("🤖 Starting Main Bot (bot.py)...", flush=True)
        p1 = subprocess.Popen([sys.executable, "-u", "bot.py"], env=env)
        processes.append(("Main Bot", p1))

    if alerts_token and alerts_token != "your_alerts_only_bot_token_here" and alerts_token != bot_token:
        print("🚨 Starting Specialized Alerts Bot (alerts_bot.py)...", flush=True)
        p2 = subprocess.Popen([sys.executable, "-u", "alerts_bot.py"], env=env)
        processes.append(("Alerts Bot", p2))
    elif not processes and alerts_token:
        print("🚨 Starting Specialized Alerts Bot (alerts_bot.py)...", flush=True)
        p2 = subprocess.Popen([sys.executable, "-u", "alerts_bot.py"], env=env)
        processes.append(("Alerts Bot", p2))

    if not processes:
        print("❌ No valid bot tokens found in .env! Please set BOT_TOKEN or ALERTS_BOT_TOKEN.", flush=True)
        sys.exit(1)

    try:
        while True:
            time.sleep(1)
            for name, proc in processes:
                if proc.poll() is not None:
                    print(f"⚠️ Process {name} exited unexpectedly with code {proc.returncode}", flush=True)
    except KeyboardInterrupt:
        print("\n⏹️ Stopping all bots...", flush=True)
        for name, proc in processes:
            proc.terminate()
        print("✅ All bots stopped.", flush=True)

if __name__ == "__main__":
    main()
