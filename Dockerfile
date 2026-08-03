FROM python:3.11-slim

# Install Docker CLI (needed for docker-py to communicate with Docker socket)
RUN apt-get update && apt-get install -y ca-certificates curl gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && apt-get install -y docker-ce-cli git curl unzip

# Install rclone for cloud storage sync
RUN curl -O https://downloads.rclone.org/rclone-current-linux-amd64.zip && \
    unzip -o rclone-current-linux-amd64.zip && \
    cp rclone-*-linux-amd64/rclone /usr/local/bin/ && \
    rm -rf rclone-current-linux-amd64.zip rclone-*-linux-amd64 && \
    chmod 755 /usr/local/bin/rclone && \
    rclone version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py heimdall_bridge.py cloud_sync.py mcp_gateway.py ./
COPY static/ ./static/
COPY templates/ ./templates/

EXPOSE 8086 8087

CMD ["python3", "agent.py"]
