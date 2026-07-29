# ==============================================================================
# Instagram Monitor Bot — Dockerfile
#
# Builds ONE image that can run either tier. Which tier actually runs is
# decided by the start command, not by rebuilding the image:
#
#   docker run ... <image> python bot_basic.py
#   docker run ... <image> python bot_advanced.py
#
# On Render: leave the Dockerfile as-is and set each service's "Start
# Command" (or "Docker Command") to one of the two lines above, per client.
# Same image, different start command per deployment.
# ==============================================================================
FROM python:3.11-slim

# DejaVu fonts are required by bot_core.generate_stat_card() (Advanced tier
# image cards) - installed at the OS level, not via pip.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot_core.py bot_basic.py bot_advanced.py ./

# Data files and generated cards persist inside this directory. Mount a
# volume/disk at /app if you need data to survive redeploys (Render: attach
# a Persistent Disk with mount path /app; Docker: -v host_dir:/app/data and
# set DATA_FILE / CHANNEL_CONFIG_FILE env vars to point inside it).
RUN mkdir -p /app/cards

EXPOSE 8080

# Default to Basic; override per-deployment as shown above.
CMD ["python", "bot_basic.py"]
