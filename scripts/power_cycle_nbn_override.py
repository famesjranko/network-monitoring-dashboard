import asyncio
import tapo
import json  # Importing json for pretty printing the output
import logging
import sqlite3
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.realpath(sys.argv[0])) 
LOG_DIR = os.environ.get("LOG_DIR", os.path.join(SCRIPT_DIR, "../", "logs"))
os.makedirs(LOG_DIR, exist_ok=True)
DB_PATH = os.environ.get("DB_PATH", os.path.join(SCRIPT_DIR, "../", 'data', 'internet_status.db'))

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

# The time to wait between turning off and on the device (in seconds)
wait_time = 30  # You can change this to any number of seconds
retry_attempts = 3  # Number of retries if a connection fails

# Log power cycle event to SQLite database
def log_power_cycle_event(reason="Internet down for 5+ minutes"):
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
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

async def control_tapo():
    try:
        # Initialize API client
        client = tapo.ApiClient(email, password)

        # Get the P100 device (requires `await`)
        device = await client.p100(device_ip)

        # Refresh the session (useful if connection becomes inactive)
        logging.info(f"Attempting to refresh session for {device_name}.")
        await device.refresh_session()
        logging.info(f"Session refreshed for {device_name}.")

        # Turn off the device
        logging.info(f"Turning off {device_name}. (Simulated)")
        await device.off() # leave commented out during testing - currently we are not testing
        logging.info(f"{device_name} has been turned off. Waiting {wait_time} seconds. (Simulated)")

        # Log the power cycle event
        log_power_cycle_event("manually triggered")

        # Wait for the specified period
        await asyncio.sleep(wait_time)
        logging.info(f"Waited for {wait_time} seconds. Turning on {device_name}. (Simulated)")

        # Turn the device back on
        await device.on() # we can leave this commented out during testing - currently we are not testing
        logging.info(f"{device_name} has been turned back on.")

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
            logging.info(f"Session refreshed during retry attempt {attempt}.")

            logging.info(f"Turning on {device_name} during retry attempt {attempt}.")
            await device.on()
            logging.info(f"{device_name} has been turned back on after retry attempt {attempt}.")

            # Print device info after successful retry
            await print_device_info(device)
            return  # Exit the loop if successful
        except Exception as retry_error:
            if attempt == retry_attempts:
                logging.info(f"OVERIDE: All retry attempts failed. Please check your connection.")
                return

async def print_device_info(device):
    try:
        # Get additional device information in JSON format
        device_info_json = await device.get_device_info_json()

        # Pretty print the JSON response
        pretty_device_info = json.dumps(device_info_json, indent=4)
        logging.info(f"Device Info:\n{pretty_device_info}")
    except Exception as e:
        logging.error(f"Failed to retrieve device info: {e}")

# Run the async function
asyncio.run(control_tapo())
