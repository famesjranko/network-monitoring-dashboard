#!/usr/bin/env bash
set -euo pipefail

# Ensure data and logs directories exist (do not chown to preserve host ownership)
mkdir -p /app/data /app/logs

# Launch supervisord; programs drop privileges via their own user setting
exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/supervisord.conf
