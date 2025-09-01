#!/bin/bash

# AudiobookBay Flask App Service Setup Script
# Run this script on your Debian VM to set up the Flask app as a systemd service

set -e

echo "=== AudiobookBay Flask App Service Setup ==="


# Variables
APP_USER=$(whoami)
APP_DIR="/home/$APP_USER/Audiobooks/ABB-Downloader/app"
SERVICE_NAME="audiobookbay"
PYTHON_PATH="/usr/bin/python3"

echo "Setting up for user: $APP_USER"
echo "App directory: $APP_DIR"

# Create app directory
echo "Creating application directory..."
mkdir -p "$APP_DIR"
cd "$APP_DIR"

# Install Python dependencies
echo "Installing system dependencies..."
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git

# Create virtual environment
echo "Creating Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install Python packages
echo "Installing Python packages..."
pip install flask flask-login beautifulsoup4 requests qbittorrent-api transmission-rpc deluge-web-client python-dotenv

# Create systemd service file
echo "Creating systemd service file..."
sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null <<EOF
[Unit]
Description=AudiobookBay Flask Application
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=
Environment=PATH=$APP_DIR/venv/bin
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/python app.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

# Default environment variables (overridden by .env file)
Environment=FLASK_ENV=production

[Install]
WantedBy=multi-user.target
EOF

# Create .env template file
echo "Creating .env template..."
cat > .env.template << 'EOF'
# Download Client Configuration
DOWNLOAD_CLIENT=qbittorrent
DL_SCHEME=http
DL_HOST=192.168.8.137
DL_PORT=8080
DL_USERNAME=admin
DL_PASSWORD=your_password_here
DL_CATEGORY=abownloader

# AudiobookBay Configuration
ABB_HOSTNAME=audiobookbay.is
PAGE_LIMIT=5

# File Paths
SAVE_PATH_BASE=/audiobooks

# Custom Navigation Link (optional)
NAV_LINK_NAME=AudiobookShelf
NAV_LINK_URL=http://107.161.92.187:13378

# Flask Secret Key (generate a random secret key)
SECRET_KEY=your-secret-key-change-me

# Optional: Flask Environment
FLASK_ENV=production
FLASK_DEBUG=0

# Optional: Host and Port (default is 0.0.0.0:5078)
FLASK_HOST=0.0.0.0
FLASK_PORT=5078
EOF
# Create startup script
echo "Creating startup script..."
cat > start.sh << 'EOF'
#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
export FLASK_APP=app.py
python app.py
EOF
chmod +x start.sh

# Create stop script
echo "Creating stop script..."
cat > stop.sh << 'EOF'
#!/bin/bash
sudo systemctl stop audiobookbay
echo "AudiobookBay service stopped"
EOF
chmod +x stop.sh

# Create status script
echo "Creating status script..."
cat > status.sh << 'EOF'
#!/bin/bash
sudo systemctl status audiobookbay
EOF
chmod +x status.sh

# Create logs script
echo "Creating logs script..."
cat > logs.sh << 'EOF'
#!/bin/bash
sudo journalctl -u audiobookbay -f
EOF
chmod +x logs.sh

echo ""
echo "=== Setup Complete! ==="
echo ""
echo "Next steps:"
echo "1. Copy your Flask app files to: $APP_DIR/app/"
echo "2. Copy and configure your .env file:"
echo "   cp .env.template .env"
echo "   nano .env"
echo "3. Enable and start the service:"
echo "   sudo systemctl daemon-reload"
echo "   sudo systemctl enable $SERVICE_NAME"
echo "   sudo systemctl start $SERVICE_NAME"
echo ""
echo "Useful commands:"
echo "  ./status.sh     - Check service status"
echo "  ./logs.sh       - View live logs"
echo "  ./stop.sh       - Stop the service"
echo "  sudo systemctl start $SERVICE_NAME   - Start the service"
echo "  sudo systemctl restart $SERVICE_NAME - Restart the service"
echo ""
echo "The app will be available at: http://your-vm-ip:5078"