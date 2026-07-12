ARG BASE_IMAGE=drenk/elina-generator-base:cu121-comfyui
FROM ${BASE_IMAGE}

# Workflow is baked into the image.
COPY elina_api.json /workflow_api.json
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Serverless handler.
COPY rp_handler.py /rp_handler.py

ENTRYPOINT ["/start.sh"]
