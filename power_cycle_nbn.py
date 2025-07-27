import asyncio
import tapo
import json  # Importing json for pretty printing the output
import logging
import sqlite3
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.realpath(sys.argv[0])) 

# Set up logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)

# Your Tapo credentials and IP address
email = os.environ.get("TAPO_EMAIL")
password = os.environ.get("TAPO_PASSWORD")
device_ip = os.environ.get("TAPO_DEVICE_IP")
device_name = os.environ.get("TAPO_DEVICE_NAME")

# Cooldown settings
COOLDOWN_FILE=os.path.join(SCRIPT_DIR, 'logs/cooldown.txt')
COOLDOWN_PERIOD = 3600  # 1 hr cooldown in seconds (3600 seconds = 1 hr)

# The time to wait between turning off and on the device (in seconds)
wait_time = 30  # You can change this to any number of seconds
retry_attempts = 3  # Number of retries if a connection fails

# Log power cycle event to SQLite database
def log_power_cycle_event(reason="Internet down for 5+ minutes"):
    try:
        db_file = os.path.join(SCRIPT_DIR, 'logs/internet_status.db')
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        timestamp_str = datetime.utcnow().isoformat(timespec='seconds') + 'Z'
        logging.info(f"DEBUG: Timestamp for power_cycle_events DB insert: {timestamp_str}")
        cursor.execute("INSERT INTO power_cycle_events (timestamp, reason) VALUES (?, ?)",
                       (timestamp_str, reason))
        conn.commit()
        conn.close()
        logging.info("Power cycle event logged successfully.")
    except sqlite3.Error as e:
        logging.error(f"Failed to log power cycle event: {e}")

# Function to check if cooldown period is active
def is_in_cooldown():
    if os.path.exists(COOLDOWN_FILE):
        with open(COOLDOWN_FILE, "r") as f:
            last_cycle = int(f.read().strip())
        current_time = int(datetime.now().timestamp())
        time_diff = current_time - last_cycle
        if time_diff < COOLDOWN_PERIOD:
            logging.info(f"Cooldown period is still active, skipping power cycle. Time left: {COOLDOWN_PERIOD - time_diff} seconds.")
            return True
    return False

# Function to update cooldown file
def update_cooldown_file():
    with open(COOLDOWN_FILE, "w") as f:
        f.write(str(int(datetime.now().timestamp())))
    logging.info("Cooldown file updated with the current timestamp.")

async def control_tapo():

    if not all([email, password, device_ip]):
        logging.critical("Tapo credentials (email, password, or IP) are not set as environment variables. "
                         "Cannot proceed with Tapo device control. Please set TAPO_EMAIL, TAPO_PASSWORD, and TAPO_DEVICE_IP.")
        return # Exit

    try:
        # Check if we are within cooldown period
        if is_in_cooldown():
            return

        # Initialize API client
        client = tapo.ApiClient(email, password)

        # Get the P100 device (requires `await`)
        device = await client.p100(device_ip)

        # Refresh the session (useful if connection becomes inactive)
        await device.refresh_session()
        logging.info(f"Session refreshed for {device_name}.")

        # Turn off the device
        await device.off()
        logging.info(f"{device_name} has been turned off.")

        # Wait for the specified period
        await asyncio.sleep(wait_time)
        logging.info(f"Waited for {wait_time} seconds.")

        # Turn the device back on
        await device.on()
        logging.info(f"{device_name} has been turned back on.")

        # Log the power cycle event
        log_power_cycle_event("Internet down for 5+ minutes")

        # Update cooldown file
        update_cooldown_file()

        # Print device info after successful operation
        await print_device_info(device)

    except asyncio.TimeoutError:
        logging.error("The request timed out. Please check your network connection or the device.")
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        await handle_exception(device)

async def handle_exception(device):
    # Retry logic on exception
    for attempt in range(1, retry_attempts + 1):
        try:
            logging.warning(f"Attempting to refresh session and retry... (Attempt {attempt}/{retry_attempts})")
            await device.refresh_session()

            # Try to turn the device on after failure
            await device.on()
            logging.info(f"{device_name} has been turned back on after retry attempt {attempt}.")

            # Print device info after successful retry
            await print_device_info(device)
            return  # Exit the loop if successful
        except Exception as retry_error:
            logging.error(f"Retry attempt {attempt} failed: {retry_error}")
            if attempt == retry_attempts:
                logging.critical(f"All retry attempts failed. Please check your connection.")
                return

async def print_device_info(device):
    try:
        # Get additional device information in JSON format
        device_info_json = await device.get_device_info_json()

        # Pretty print the JSON response
        pretty_device_info = json.dumps(device_info_json, indent=4)
        logging.info(f"Device info: {device_info_json}")
    except Exception as e:
        logging.error(f"Failed to retrieve device info: {e}")

# Run the async function
asyncio.run(control_tapo())
