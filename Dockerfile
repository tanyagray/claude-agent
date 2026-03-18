FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    python3 \
    python3-pip \
    python3-venv \
    supervisor \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 (for Claude Code CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# Application code
WORKDIR /app
COPY src/ /app/src/
COPY supervisord.conf /etc/supervisor/conf.d/claude-agent.conf
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Create a non-root user (Claude Code CLI refuses --dangerously-skip-permissions as root)
RUN useradd -m -s /bin/bash claude-agent

# Data directory (will be a volume mount)
RUN mkdir -p /data && chown claude-agent:claude-agent /data

USER claude-agent

EXPOSE 5000

ENTRYPOINT ["/app/entrypoint.sh"]
