# Use a modern Python runtime
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies: iputils-ping, sqlite3 CLI, supervisord, curl, tzdata
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        iputils-ping \
        sqlite3 \
        supervisor \
        curl \
        tzdata && \
    rm -rf /var/lib/apt/lists/*

# Create runtime directories
RUN mkdir -p logs data scripts

# Create non-root user for runtime
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code into the container at /app
COPY internet_status_dashboard.py .
COPY src/ ./src/
COPY scripts/ ./scripts/

# Copy Dash assets (CSS/JS)
COPY assets/ ./assets/

# Make the check_internet.sh script executable
RUN chmod +x scripts/check_internet.sh

# Configure supervisord
COPY docker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Expose port for the Dash app
EXPOSE 8050

# Health check for dashboard
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl --fail http://localhost:8050/health || exit 1

# Ensure our src is importable
ENV PYTHONPATH=/app/src

# Start supervisord (run as root; programs can drop privileges)
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
