#!/bin/bash

# Copy folder to /srv/
sudo cp -r /home/aquitard/Projects/system-monitor /srv/.

# Install dependencies using uv
uv sync


# Copy service file to systemd directory
sudo cp system-monitor.service /etc/systemd/system/.

# Reload systemd daemon
sudo systemctl daemon-reload

# Enable the service
sudo systemctl enable system-monitor.service

# Start the service
sudo systemctl start system-monitor.service

echo "system-monitor service installed and started successfully"
