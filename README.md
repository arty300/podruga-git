# Podruga image generation test

Project layout:

- `poda/`: RunPod serverless worker image. Build and deploy this folder to RunPod.
- `bot/`: Telegram test bot. Run this locally or on a small server after the RunPod endpoint is deployed.

The bot sends prompts to the deployed RunPod endpoint. The pod/worker runs ComfyUI and returns generated images.
