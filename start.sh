#!/bin/bash
set -e

# Use PORT environment variable or default to 5000
PORT=${PORT:-5000}

# Start gunicorn
exec gunicorn api:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120 --access-logfile - --error-logfile -

