import asyncio
import base64
import io
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters


RUNPOD_TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}
DEFAULT_ENV_FILE = Path(__file__).with_name("bot.env")


def log_action(action, **fields):
    if fields:
        details = " ".join(f"{key}={value}" for key, value in fields.items())
        logging.info("%s %s", action, details)
    else:
        logging.info("%s", action)


@dataclass(frozen=True)
class Config:
    telegram_token: str
    runpod_api_key: str
    runpod_endpoint_id: str
    allowed_chat_ids: set[int]
    request_timeout: int
    runpod_poll_interval: float
    runpod_job_timeout: int
    max_workers: int
    execution_timeout_ms: int
    ttl_ms: int

    @classmethod
    def from_env(cls):
        return cls(
            telegram_token=require_env("TELEGRAM_BOT_TOKEN"),
            runpod_api_key=require_env("RUNPOD_API_KEY"),
            runpod_endpoint_id=require_env("RUNPOD_ENDPOINT_ID"),
            allowed_chat_ids=parse_allowed_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")),
            request_timeout=int(os.getenv("REQUEST_TIMEOUT", "60")),
            runpod_poll_interval=float(os.getenv("RUNPOD_POLL_INTERVAL", "3")),
            runpod_job_timeout=int(os.getenv("RUNPOD_JOB_TIMEOUT", "900")),
            max_workers=int(os.getenv("BOT_MAX_WORKERS", "2")),
            execution_timeout_ms=int(os.getenv("RUNPOD_EXECUTION_TIMEOUT_MS", "900000")),
            ttl_ms=int(os.getenv("RUNPOD_TTL_MS", "1800000")),
        )


def require_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def load_env_file(path):
    if not path.exists():
        log_action("env_file_missing", path=path)
        return

    loaded_keys = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded_keys.append(key)

    log_action("env_file_loaded", path=path, keys=",".join(loaded_keys) or "none")


def parse_allowed_chat_ids(value):
    if not value.strip():
        return set()
    return {int(part.strip()) for part in value.split(",") if part.strip()}


class RunPodClient:
    def __init__(self, api_key, endpoint_id, timeout):
        self.timeout = timeout
        self.endpoint_id = endpoint_id
        self.base_url = f"https://api.runpod.ai/v2/{endpoint_id}"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    def submit(self, prompt, execution_timeout_ms, ttl_ms):
        log_action(
            "runpod_submit_start",
            endpoint_id=self.endpoint_id,
            prompt_len=len(prompt),
            execution_timeout_ms=execution_timeout_ms,
            ttl_ms=ttl_ms,
        )
        payload = {
            "input": {"prompt": prompt},
            "policy": {
                "executionTimeout": execution_timeout_ms,
                "ttl": ttl_ms,
            },
        }
        response = self.session.post(f"{self.base_url}/run", json=payload, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        job_id = data.get("id")
        if not job_id:
            raise RuntimeError(f"RunPod did not return a job id: {data}")
        log_action("runpod_submit_ok", job_id=job_id)
        return job_id

    def status(self, job_id):
        log_action("runpod_status_start", job_id=job_id)
        response = self.session.get(f"{self.base_url}/status/{job_id}", timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        log_action("runpod_status_ok", job_id=job_id, status=data.get("status", "unknown"))
        return data

    def health(self):
        log_action("runpod_health_start", endpoint_id=self.endpoint_id)
        response = self.session.get(f"{self.base_url}/health", timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        log_action("runpod_health_ok", endpoint_id=self.endpoint_id)
        return data

    def wait_for_result(self, job_id, poll_interval, timeout_seconds):
        deadline = time.monotonic() + timeout_seconds
        log_action(
            "runpod_wait_start",
            job_id=job_id,
            poll_interval=poll_interval,
            timeout_seconds=timeout_seconds,
        )

        while time.monotonic() < deadline:
            data = self.status(job_id)
            status = data.get("status")

            if status == "COMPLETED":
                output = data.get("output") or {}
                if isinstance(output, dict) and output.get("error"):
                    raise RuntimeError(output["error"])
                log_action("runpod_wait_completed", job_id=job_id)
                return output

            if status in RUNPOD_TERMINAL_STATUSES:
                error = data.get("error") or data.get("output") or data
                log_action("runpod_wait_terminal_error", job_id=job_id, status=status)
                raise RuntimeError(f"RunPod job {job_id} finished with status {status}: {error}")

            log_action("runpod_wait_sleep", job_id=job_id, status=status, seconds=poll_interval)
            time.sleep(poll_interval)

        log_action("runpod_wait_timeout", job_id=job_id)
        raise TimeoutError(f"RunPod job {job_id} did not finish in {timeout_seconds} seconds")


class BotHandlers:
    def __init__(self, config):
        self.config = config
        self.runpod = RunPodClient(config.runpod_api_key, config.runpod_endpoint_id, config.request_timeout)
        self.generation_slots = asyncio.Semaphore(config.max_workers)

    async def post_init(self, application):
        bot = application.bot
        me = await bot.get_me()
        log_action("telegram_get_me_ok", bot_id=me.id, username=me.username)
        await bot.delete_webhook(drop_pending_updates=False)
        log_action("telegram_delete_webhook_ok")

        if not self.config.allowed_chat_ids:
            log_action("startup_notify_skipped", reason="TELEGRAM_ALLOWED_CHAT_IDS_empty")
            return

        for chat_id in self.config.allowed_chat_ids:
            try:
                log_action("startup_notify_chat", chat_id=chat_id)
                await bot.send_message(chat_id=chat_id, text="Бот запущен и ждёт запрос.")
            except Exception:
                logging.exception("Failed to send startup message to chat_id=%s", chat_id)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.ensure_allowed(update):
            return

        log_update(update, "command_start_or_help")
        await update.effective_message.reply_text(
            "Пришли текстовый запрос. Фото не отправляются в RunPod; workflow использует постоянную картинку в LoadImage. /id покажет chat_id."
        )

    async def chat_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.ensure_allowed(update):
            return

        log_update(update, "command_id")
        await update.effective_message.reply_text(f"chat_id: {update.effective_chat.id}")

    async def health(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.ensure_allowed(update):
            return

        log_update(update, "command_health")
        try:
            health = await asyncio.to_thread(self.runpod.health)
            await update.effective_message.reply_text(f"RunPod health:\n{compact_json(health)}")
            log_update(update, "health_check_done")
        except Exception as exc:
            logging.exception("RunPod health check failed")
            await update.effective_message.reply_text(f"RunPod health error: {exc}")

    async def generate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.ensure_allowed(update):
            return

        message = update.effective_message
        prompt = strip_bot_command((message.text or message.caption or "").strip())
        log_update(
            update,
            "generation_request_received",
            prompt_len=len(prompt),
            has_photo=bool(message.photo),
        )

        if not prompt:
            await message.reply_text("Нужен текстовый запрос для генерации.")
            log_update(update, "generation_empty_prompt")
            return

        if self.generation_slots.locked():
            await message.reply_text("Бот сейчас занят, попробуй ещё раз чуть позже.")
            log_update(update, "generation_rejected_workers_busy")
            return

        async with self.generation_slots:
            await self.run_generation(update, prompt)

    async def run_generation(self, update, prompt):
        message = update.effective_message
        chat_id = update.effective_chat.id

        try:
            log_update(update, "generation_start", prompt_len=len(prompt))
            if update.effective_message.photo:
                log_update(update, "source_image_ignored", reason="workflow_uses_static_load_image")

            await message.reply_text("Запрос отправлен в RunPod, жду генерацию.")
            await message.chat.send_action("upload_photo")

            job_id, output = await asyncio.to_thread(self.generate_with_runpod, prompt)
            images = extract_images(output)
            log_update(update, "generation_output_received", job_id=job_id, image_count=len(images))
            if not images:
                raise RuntimeError(f"RunPod returned no images: {output}")

            for index, image_b64 in enumerate(images, start=1):
                log_update(update, "generation_decode_image_start", job_id=job_id, image_index=index)
                image_bytes = base64.b64decode(strip_data_uri_prefix(image_b64), validate=True)
                caption = f"Готово. Job: {job_id}" if index == 1 else None
                await message.reply_photo(photo=io.BytesIO(image_bytes), caption=caption)
                log_update(update, "generation_send_image_ok", job_id=job_id, image_index=index)

            log_update(update, "generation_done", job_id=job_id)
        except Exception as exc:
            logging.exception("Generation failed for chat_id=%s", chat_id)
            await message.reply_text(f"Ошибка генерации: {exc}")

    def generate_with_runpod(self, prompt):
        job_id = self.runpod.submit(
            prompt,
            self.config.execution_timeout_ms,
            self.config.ttl_ms,
        )
        output = self.runpod.wait_for_result(
            job_id,
            self.config.runpod_poll_interval,
            self.config.runpod_job_timeout,
        )
        return job_id, output

    async def ensure_allowed(self, update):
        chat_id = update.effective_chat.id
        if self.config.allowed_chat_ids and chat_id not in self.config.allowed_chat_ids:
            log_update(update, "message_rejected_chat_not_allowed")
            return False
        return True

    async def error_handler(self, update, context):
        logging.exception("Unhandled telegram handler error update=%s", update, exc_info=context.error)
        if update and update.effective_message:
            try:
                await update.effective_message.reply_text(f"Ошибка бота: {context.error}")
            except Exception:
                logging.exception("Failed to send handler error to Telegram")


def log_update(update, action, **fields):
    message = update.effective_message
    chat = update.effective_chat
    base_fields = {
        "chat_id": chat.id if chat else "unknown",
        "message_id": message.message_id if message else "unknown",
    }
    base_fields.update(fields)
    log_action(action, **base_fields)


def strip_bot_command(text):
    if text.startswith("/generate"):
        return text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
    return text


def extract_images(output):
    if isinstance(output, dict):
        images = output.get("images")
        if isinstance(images, list):
            return images
    return []


def strip_data_uri_prefix(value):
    if "," in value and value.strip().lower().startswith("data:"):
        return value.split(",", 1)[1]
    return value


def compact_json(value):
    text = json.dumps(value, ensure_ascii=False, indent=2)
    return text if len(text) <= 3500 else text[:3500] + "..."


def build_application(config):
    handlers = BotHandlers(config)
    application = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .concurrent_updates(config.max_workers)
        .post_init(handlers.post_init)
        .build()
    )

    application.add_handler(CommandHandler(["start", "help"], handlers.start))
    application.add_handler(CommandHandler("id", handlers.chat_id))
    application.add_handler(CommandHandler("health", handlers.health))
    application.add_handler(CommandHandler("generate", handlers.generate))
    application.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, handlers.generate))
    application.add_error_handler(handlers.error_handler)
    return application


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    load_env_file(DEFAULT_ENV_FILE)
    logging.getLogger().setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    log_action("logging_configured", level=os.getenv("LOG_LEVEL", "INFO").upper())

    config = Config.from_env()
    log_action(
        "bot_init",
        endpoint_id=config.runpod_endpoint_id,
        allowed_chats=len(config.allowed_chat_ids),
        max_workers=config.max_workers,
    )
    application = build_application(config)
    log_action("telegram_run_polling_start")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log_action("keyboard_interrupt")
        sys.exit(130)
    except Exception as exc:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        logging.error("Bot stopped: %s", exc)
        sys.exit(1)
