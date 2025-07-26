# Use an official Python runtime as a parent image
FROM python:3.8-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies: iputils-ping, sqlite3, redis-server, and supervisord
RUN apt-get update &&     apt-get install -y iputils-ping sqlite3 redis-server supervisor &&     rm -rf /var/lib/apt/lists/*

# Configure Redis to limit memory usage
RUN echo "maxmemory 256mb" >> /etc/redis/redis.conf &&     echo "maxmemory-policy allkeys-lru" >> /etc/redis/redis.conf

# Create the logs directory
RUN mkdir -p logs

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt



# Copy the application code into the container at /app
COPY check_internet.sh .
COPY internet_status_dashboard.py .
COPY power_cycle_nbn.py .
COPY power_cycle_nbn_override.py .

# Make the check_internet.sh script executable
RUN chmod +x check_internet.sh

# Configure supervisord
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Make port 8050 available to the world outside this container
EXPOSE 8050

# Start supervisord
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
