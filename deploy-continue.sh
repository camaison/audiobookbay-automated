#!/bin/bash

# Continue AudiobookBay Automated deployment from where it left off
# Run this on your VPS to complete the deployment

set -e
set -o pipefail

echo "==================================================================="
echo "AudiobookBay Automated - Continue Deployment"
echo "==================================================================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (use sudo)"
    exit 1
fi

APP_DIR="/opt/audiobookbay-automated"

# Verify app directory exists
if [ ! -d "$APP_DIR" ]; then
    echo "Error: $APP_DIR does not exist. Please run the main deploy.sh first."
    exit 1
fi

cd "$APP_DIR"

# Pull latest changes
echo "Pulling latest changes from git..."
git pull

echo ""
echo "Step 1: Obtaining SSL certificate..."
if [ ! -d "/etc/letsencrypt/live/abb.bvronan.xyz" ]; then
    echo "Getting certificate..."
    # Stop nginx to free up port 80
    systemctl stop nginx 2>/dev/null || true

    # Get certificate
    certbot certonly --standalone -d abb.bvronan.xyz \
        --non-interactive \
        --agree-tos \
        --email cyprianmaison@outlook.com

    echo "Certificate obtained successfully!"
else
    echo "SSL certificate already exists, skipping..."
fi

echo ""
echo "Step 2: Configuring nginx..."
cp nginx/abb.conf /etc/nginx/sites-available/abb.bvronan.xyz
ln -sf /etc/nginx/sites-available/abb.bvronan.xyz /etc/nginx/sites-enabled/

# Test nginx configuration
echo "Testing nginx configuration..."
nginx -t

# Start/reload nginx
echo "Starting nginx..."
systemctl start nginx
systemctl reload nginx

echo ""
echo "Step 3: Setting up systemd service..."
cp systemd/audiobookbay.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable audiobookbay.service
systemctl start audiobookbay.service

echo ""
echo "Step 4: Verifying deployment..."
sleep 3
systemctl status audiobookbay.service --no-pager

echo ""
echo "Checking if Docker container is running..."
docker ps | grep audiobookbay || echo "Container not found in docker ps"

echo ""
echo "==================================================================="
echo "✅ Deployment complete!"
echo "==================================================================="
echo ""
echo "Your application should now be available at: https://abb.bvronan.xyz"
echo ""
echo "If you still get connection refused, check:"
echo "  - Docker container: docker ps"
echo "  - Container logs:   docker-compose -f $APP_DIR/docker-compose.prod.yaml logs -f"
echo "  - Service status:   systemctl status audiobookbay"
echo "  - nginx status:     systemctl status nginx"
echo ""
