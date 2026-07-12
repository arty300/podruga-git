# Telegram test bot

This folder is only for the Telegram bot that tests the deployed RunPod serverless endpoint.

The bot runs outside RunPod. It receives Telegram messages, sends jobs to RunPod, waits for completion, and sends generated images back to the user.

Files:

- `telegram_bot.py`: `python-telegram-bot` based Telegram bot and RunPod client.
- `bot.env.example`: environment variable template.
- `requirements.txt`: Python dependencies for the bot.

## Setup

Create a bot with BotFather, copy the env template, and fill real values:

```bash
cd bot
cp bot.env.example bot.env
```

Required variables:

```bash
TELEGRAM_BOT_TOKEN=...
RUNPOD_API_KEY=...
RUNPOD_ENDPOINT_ID=...
```

Then run:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 telegram_bot.py
```

## Messages

- Plain text: sent as `input.prompt`.
- Photo with caption: caption is sent as `input.prompt`, photo is sent as `input.source_image`.
- `/generate your prompt`: same as plain text, with `/generate` stripped.
- `/health`: calls `GET /health` on the RunPod endpoint.

## RunPod API flow

The bot uses async jobs:

1. `POST https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/run`
2. `GET https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/status/$JOB_ID`
3. Sends every base64 image from `output.images` back to Telegram.
