# Podruga image generation test

Project layout:

- `poda/`: RunPod serverless worker image. Build and deploy this folder to RunPod.
- `bot/`: Telegram test bot. Run it locally, with Docker, or on a small hosting service after the RunPod endpoint is deployed.

The bot sends prompts to the deployed RunPod endpoint. The pod/worker runs ComfyUI and returns generated images.

Images built by GitHub Actions:

- `drenk/elina-generator:v25`: RunPod worker.
- `drenk/elina-telegram-bot:latest`: Telegram bot.
