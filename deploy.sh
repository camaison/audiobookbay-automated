#!/bin/bash

# AudiobookBay Automated - VPS Deployment Script
# This script sets up and deploys the application on a VPS

set -e  # Exit on error
set -o pipefail  # Exit on pipe failures

echo "==================================================================="
echo "AudiobookBay Automated - VPS Deployment"
echo "==================================================================="

# Configuration
APP_DIR="/opt/audiobookbay-automated"
REPO_URL="https://github.com/camaison/audiobookbay-automated.git"  # UPDATE THIS
NGINX_CONF_SRC="nginx/abb.conf"
NGINX_CONF_DEST="/etc/nginx/sites-available/abb.bvronan.xyz"
SYSTEMD_SERVICE_SRC="systemd/audiobookbay.service"
SYSTEMD_SERVICE_DEST="/etc/systemd/system/audiobookbay.service"

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (use sudo)"
    exit 1
fi

# Step 1: Install dependencies
echo ""
echo "Step 1: Installing dependencies..."
apt-get update

# Check if Docker is already installed
if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    apt-get install -y docker.io docker-compose
    systemctl enable docker
    systemctl start docker
else
    echo "Docker is already installed, skipping..."
    # Just ensure docker-compose is installed
    apt-get install -y docker-compose || true
fi

# Install other dependencies
apt-get install -y git certbot python3-certbot-nginx

# Step 2: Clone or update repository
echo ""
echo "Step 2: Setting up application directory..."
if [ -d "$APP_DIR" ]; then
    echo "Directory exists, pulling latest changes..."
    cd "$APP_DIR"
    git pull
else
    echo "Cloning repository..."
    git clone "$REPO_URL" "$APP_DIR"
    cd "$APP_DIR"
fi

# Step 3: Create data directory for persistence
echo ""
echo "Step 3: Creating data directory..."
mkdir -p "$APP_DIR/data"
chmod 755 "$APP_DIR/data"

# Step 4: Setup environment file
echo ""
echo "Step 4: Setting up environment file..."
if [ ! -f "$APP_DIR/.env" ]; then
    echo "Creating .env file from example..."
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo ""
    echo "⚠️  IMPORTANT: Edit $APP_DIR/.env with your configuration!"
    echo "   Required variables:"
    echo "   - DL_HOST (qBittorrent host)"
    echo "   - DL_PORT (qBittorrent port)"
    echo "   - DL_USERNAME (qBittorrent username)"
    echo "   - DL_PASSWORD (qBittorrent password)"
    echo "   - SECRET_KEY (Flask secret key - generate with: openssl rand -hex 32)"
    echo "   - NAV_LINK_URL (AudiobookShelf URL)"
    echo ""
    read -p "Press Enter after you've edited the .env file..."
fi

# Step 5: Build Docker image
echo ""
echo "Step 5: Building Docker image..."
docker-compose -f docker-compose.prod.yaml build

# Step 6: Setup nginx
echo ""
echo "Step 6: Configuring nginx..."
cp "$NGINX_CONF_SRC" "$NGINX_CONF_DEST"
ln -sf "$NGINX_CONF_DEST" /etc/nginx/sites-enabled/abb.bvronan.xyz

# Step 7: Obtain SSL certificate
echo ""
echo "Step 7: Setting up SSL certificate..."
if [ ! -d "/etc/letsencrypt/live/abb.bvronan.xyz" ]; then
    echo "Obtaining SSL certificate..."
    certbot --nginx -d abb.bvronan.xyz --non-interactive --agree-tos --email cyprianmaison@outlook.com  # UPDATE THIS
else
    echo "SSL certificate already exists"
fi

# Test nginx configuration
nginx -t
systemctl reload nginx

# Step 8: Setup systemd service
echo ""
echo "Step 8: Setting up systemd service..."
cp "$SYSTEMD_SERVICE_SRC" "$SYSTEMD_SERVICE_DEST"
systemctl daemon-reload
systemctl enable audiobookbay.service
systemctl start audiobookbay.service

# Step 9: Verify deployment
echo ""
echo "Step 9: Verifying deployment..."
sleep 5
systemctl status audiobookbay.service --no-pager

# Check if container is running
docker ps | grep audiobookbay-automated

echo ""
echo "==================================================================="
echo "✅ Deployment complete!"
echo "==================================================================="
echo ""
echo "Your application should now be available at: https://abb.bvronan.xyz"
echo ""
echo "Useful commands:"
echo "  - View logs:           docker-compose -f $APP_DIR/docker-compose.prod.yaml logs -f"
echo "  - Restart service:     systemctl restart audiobookbay"
echo "  - Stop service:        systemctl stop audiobookbay"
echo "  - View service status: systemctl status audiobookbay"
echo "  - Update application:  cd $APP_DIR && git pull && docker-compose -f docker-compose.prod.yaml up -d --build"
echo ""
