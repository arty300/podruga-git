# Telegram test bot

This folder is only for the Telegram bot that tests the deployed RunPod serverless endpoint.

The bot runs outside RunPod. It receives Telegram messages, sends jobs to RunPod, waits for completion, and sends generated images back to the user.

Files:

- `telegram_bot.py`: `python-telegram-bot` based Telegram bot and RunPod client.
- `bot.env.example`: environment variable template.
- `requirements.txt`: Python dependencies for the bot.
- `Dockerfile`: container image for running the bot on a small hosting service.

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

## Docker

Local test with Docker Compose:

```bash
docker compose up --build bot
```

Or run the image directly:

```bash
docker build -t drenk/elina-telegram-bot:latest .
docker run --env-file bot.env drenk/elina-telegram-bot:latest
```

GitHub Actions builds and pushes:

```text
drenk/elina-telegram-bot:latest
```

Required hosting environment variables:

```bash
TELEGRAM_BOT_TOKEN=...
RUNPOD_API_KEY=...
RUNPOD_ENDPOINT_ID=...
TELEGRAM_ALLOWED_CHAT_IDS=...
```

Optional tuning variables are listed in `bot.env.example`.

## Free hosting option

Recommended first try: Render Background Worker.

Create a new Render service:

```text
New -> Background Worker
Repository: arty300/podruga-git
Runtime: Docker
Root Directory: bot
Dockerfile Path: Dockerfile
Instance Type: Free
```

Add at least these environment variables:

```text
TELEGRAM_BOT_TOKEN
RUNPOD_API_KEY
RUNPOD_ENDPOINT_ID
TELEGRAM_ALLOWED_CHAT_IDS
```

Keep `TELEGRAM_ALLOWED_CHAT_IDS` set after testing so random users cannot spend RunPod credits through the bot.

## Messages

- Plain text: sent as `input.prompt`.
- Photo with caption: caption is sent as `input.prompt`; the photo itself is ignored.
- `/generate your prompt`: same as plain text, with `/generate` stripped.
- `/health`: calls `GET /health` on the RunPod endpoint.

## RunPod API flow

The bot uses async jobs:

1. `POST https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/run`
2. `GET https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/status/$JOB_ID`
3. Sends every base64 image from `output.images` back to Telegram.
