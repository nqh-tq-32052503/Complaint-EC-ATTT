#!/bin/bash
# Backward-compatibility wrapper forwarding to entrypoint.py
exec python3 /app/entrypoint.py "$@"
