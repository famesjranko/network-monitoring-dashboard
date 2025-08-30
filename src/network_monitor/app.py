"""
Compatibility shim exposing the Dash `server` for Gunicorn.

Keeps the existing app implementation untouched while allowing
Gunicorn to import `network_monitor.app:server`.
"""

from importlib import import_module


# Import the existing module and re-export its Flask server
_mod = import_module("internet_status_dashboard")
server = getattr(_mod, "server")

