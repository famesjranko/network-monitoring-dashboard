# Containerized Internet Monitor with Dash & Tapo Power Cycling

[](https://www.python.org/)
[](https://www.docker.com/)
[](https://dash.plotly.com/)

This project provides a self-contained, easy-to-deploy solution for monitoring your internet connectivity. It periodically pings external targets, logs the results to an SQLite database, and visualizes the data on a web dashboard.

If the connection is down for a sustained period, it can **automatically power cycle your modem/router** using a TP-Link Tapo smart plug. The entire system is packaged into a single Docker container, making setup incredibly simple.

-----

## ✨ Features

  * **📊 Interactive Web Dashboard:** Visualize internet health with real-time graphs for uptime, latency, and packet loss using a Plotly Dash interface.
  * **🤖 Automated Power Cycling:** Automatically reboots your modem via a Tapo P100 smart plug after 5 consecutive failed checks.
  * **👆 Manual Override:** A "Restart NBN" button on the dashboard allows you to trigger a power cycle manually at any time.
  * **🚀 Simple Docker Setup:** Get up and running with a single `docker-compose up` command. No need to manually install any dependencies.
  * **🗄️ SQLite Logging:** All connectivity data is logged to an SQLite database within the container.

-----

## 📊 Web Dashboard Interface

![Dash Web App Screenshot](screenshots/dashboard.png)

-----

## 🚀 Quickstart

Getting the monitor running is simple. You just need Docker and Docker Compose installed.

### 1\. Prerequisites

  * [Docker](https://docs.docker.com/get-docker/)
  * [Docker Compose](https://docs.docker.com/compose/install/)

### 2\. Clone the Repository

```bash
git clone https://github.com/famesjranko/docker-network-monitor-dash.git
cd docker-network-monitor-dash
```

### 3. Configure Docker Compose

To customize monitoring behavior or enable TP-Link Tapo smart plug control, open the `docker-compose.yml` file and uncomment the `environment` section. Then fill in the values as needed:

```yaml
version: '3.8'
services:
  local-network-monitor:
    build: .
    container_name: local-network-monitor-container
    ports:
      - "8050:8050"
    environment:
      # Comma-separated list of IPs to ping for internet connectivity checks.
      # You can override the defaults to suit your network.
      # - Default: 8.8.8.8,1.1.1.1,9.9.9.9
      # - Example: INTERNET_CHECK_TARGETS=1.1.1.1,8.8.4.4
      # - INTERNET_CHECK_TARGETS=8.8.8.8,1.1.1.1,9.9.9.9

      # Tapo credentials and device details for controlling the smart plug (optional).
      # Required only if using a Tapo P100 to power cycle your modem/router.
      # - TAPO_EMAIL=your_tapo_email@example.com
      # - TAPO_PASSWORD=your_super_secret_password
      # - TAPO_DEVICE_IP=192.168.1.100
      # - TAPO_DEVICE_NAME="NBN Modem Plug"

      # Cooldown period (in seconds) between allowed modem reboots via smart plug.
      # Prevents repeated power cycling too frequently.
      # - Default: 3600 (1 hour)
      # - Example: TAPO_COOLDOWN_SECONDS=1800
      # - TAPO_COOLDOWN_SECONDS=3600
```

**Notes:**

* `INTERNET_CHECK_TARGETS` works regardless of whether Tapo is used.
* If you're not using a Tapo smart plug, leave the Tapo-related variables commented out.
* It’s recommended to assign a static IP to your Tapo plug in your router’s DHCP settings to ensure consistent operation.

---

### 4\. Build and Run the Container

From the project's root directory, launch the application:

```bash
docker-compose up --build -d
```

### 5\. Access the Dashboard

Open your web browser and navigate to:

**`http://localhost:8050`**

(Replace `localhost` with the IP address of your host machine if you're accessing it from another device on your network).

-----

## 🏗️ System Architecture

This project runs within a **single, all-in-one Docker container**.

The `Dockerfile` builds an image based on Python 3.8 and installs all necessary components: `ping`, `sqlite3`, the `redis-server`, and `supervisor`.

Inside the container, `supervisord` is responsible for running and managing three key processes simultaneously:

1.  **Redis Server:** A local Redis instance for caching dashboard data.
2.  **Monitoring Script (`check_internet.sh`):** A shell script that runs every minute to ping targets and log results.
3.  **Dash Web App (`internet_status_dashboard.py`):** A Gunicorn server that hosts the Python web application.

This single-container approach simplifies deployment and management.

-----

## 🔧 Configuration

### Environment Variables

These variables **can be set** in your `docker-compose.yml` file for the power cycling feature to work - if not set or not valid, restart button will be greyed out unavailable.

| Variable           | Description                                  |
| :----------------- | :------------------------------------------- |
| `TAPO_EMAIL`       | **Optional.** Your Tapo account email.       |
| `TAPO_PASSWORD`    | **Optional.** Your Tapo account password.    |
| `TAPO_DEVICE_IP`   | **Optional.** The static IP of your Tapo plug. |
| `TAPO_DEVICE_NAME` | **Optional.** friendly name for your device.   |

### Script Parameters

For more advanced tuning, you can modify the monitoring script directly by editing the **`check_internet.sh`** file:

  * **Ping Targets:** To change which servers are pinged, modify the `TARGETS` array.

    ```bash
    # check_internet.sh
    TARGETS=("8.8.8.8" "1.1.1.1" "8.8.4.4")
    ```

  * **Failure Threshold:** To adjust how many failures trigger a reboot, change the `FAILURE_THRESHOLD` variable.

    ```bash
    # check_internet.sh
    FAILURE_THRESHOLD=5
    ```

-----

## ⚙️ Usage and Management

### Checking Logs

To view the real-time logs from the application (including ping results and errors), run:

```bash
docker-compose logs -f local-network-monitor
```

### Stopping the Application

To stop the container, run:

```bash
docker-compose down
```

### ⚠️ Data Persistence (Optional\!)

By default, the SQLite database (`internet_status.db`) is stored inside the container. **This means all historical data will be deleted if you run `docker-compose down`**.

To make your data persistent, you must add a **volume mount** to your `docker-compose.yml` file. This links the `logs` directory inside the container to a `logs` directory on your host machine.

Modify your `docker-compose.yml` to include the `volumes` section like this:

```yaml
# docker-compose.yml

services:
  local-network-monitor:
    build: .
    container_name: local-network-monitor-container
    ports:
      - "8050:8050"
    volumes:
      - ./logs:/app/logs  # <-- Add this line
    environment:
      # ... your environment variables
```

With this change, your database will be safe on your local machine, even if the container is removed.
