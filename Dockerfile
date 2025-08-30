# Use an official Python runtime as a parent image - v3.8.18-slim
FROM python:3.8.18-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies: iputils-ping, sqlite3, redis-server, and supervisord
RUN apt-get update && \
    apt-get install -y \
        iputils-ping \
        sqlite3 \
        redis-server \
        supervisor \
        curl && \
    rm -rf /var/lib/apt/lists/*

# Configure Redis to limit memory usage
RUN echo "maxmemory 256mb" >> /etc/redis/redis.conf && \
    echo "maxmemory-policy allkeys-lru" >> /etc/redis/redis.conf && \
    # Disable persistence (cache-only use): avoid bgsave/AOF forks
    echo "save \"\"" >> /etc/redis/redis.conf && \
    echo "appendonly no" >> /etc/redis/redis.conf

# Create the logs directory
RUN mkdir -p logs

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code into the container at /app
COPY check_internet.sh \
     internet_status_dashboard.py \
     power_cycle_nbn.py \
     power_cycle_nbn_override.py .

# Copy Dash assets (CSS/JS)
COPY assets/ ./assets/

# Make the check_internet.sh script executable
RUN chmod +x check_internet.sh

# Configure supervisord
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Expose port for the Dash app
EXPOSE 8050

# Health check for dashboard
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl --fail http://localhost:8050/health || exit 1

# Start supervisord
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
