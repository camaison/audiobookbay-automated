# AudiobookBay Automated - Deployment Guide

This guide walks you through deploying the AudiobookBay Automated application to your VPS with Docker, nginx, and HTTPS.

## Prerequisites

- VPS with Ubuntu/Debian Linux
- Root or sudo access
- Domain name pointing to your VPS IP (abb.bvronan.xyz)
- qBittorrent instance running (can be on the same or different server)

## Quick Deployment

### Option 1: Automated Deployment Script

1. SSH into your VPS:
```bash
ssh user@your-vps-ip
```

2. Download and run the deployment script:
```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/YOUR_USERNAME/audiobookbay-automated/main/deploy.sh)"
```

3. Follow the prompts to configure your environment

### Option 2: Manual Deployment

Follow the steps below for a manual installation.

## Manual Deployment Steps

### 1. Install Dependencies

```bash
# Update system
sudo apt-get update
sudo apt-get upgrade -y

# Install Docker
sudo apt-get install -y docker.io docker-compose

# Enable Docker to start on boot
sudo systemctl enable docker
sudo systemctl start docker

# Install nginx
sudo apt-get install -y nginx

# Install certbot for SSL certificates
sudo apt-get install -y certbot python3-certbot-nginx
```

### 2. Clone Repository

```bash
# Create application directory
sudo mkdir -p /opt/audiobookbay-automated
cd /opt/audiobookbay-automated

# Clone repository
sudo git clone https://github.com/YOUR_USERNAME/audiobookbay-automated.git .
```

### 3. Configure Environment

```bash
# Copy environment template
sudo cp .env.example .env

# Edit environment file
sudo nano .env
```

**Required Configuration:**

```env
# qBittorrent settings
DL_HOST=your.qbittorrent.host
DL_PORT=8080
DL_USERNAME=admin
DL_PASSWORD=your_password

# Flask secret key (generate with: openssl rand -hex 32)
SECRET_KEY=your_generated_secret_key

# AudiobookShelf URL
NAV_LINK_URL=http://your-audiobooks-url:13378/
```

### 4. Create Data Directory

```bash
# Create directory for persistent data
sudo mkdir -p /opt/audiobookbay-automated/data
sudo chmod 755 /opt/audiobookbay-automated/data
```

### 5. Build and Start Docker Container

```bash
# Build the Docker image
sudo docker-compose -f docker-compose.prod.yaml build

# Start the container
sudo docker-compose -f docker-compose.prod.yaml up -d

# Verify container is running
sudo docker ps | grep audiobookbay
```

### 6. Configure nginx

```bash
# Copy nginx configuration
sudo cp nginx/abb.conf /etc/nginx/sites-available/abb.bvronan.xyz

# Create symbolic link to enable site
sudo ln -s /etc/nginx/sites-available/abb.bvronan.xyz /etc/nginx/sites-enabled/

# Test nginx configuration
sudo nginx -t

# Reload nginx
sudo systemctl reload nginx
```

### 7. Obtain SSL Certificate

```bash
# Obtain Let's Encrypt certificate
sudo certbot --nginx -d abb.bvronan.xyz

# Follow the prompts to complete certificate setup
```

The certificate will automatically renew. Verify auto-renewal:
```bash
sudo certbot renew --dry-run
```

### 8. Setup systemd Service

```bash
# Copy systemd service file
sudo cp systemd/audiobookbay.service /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable service to start on boot
sudo systemctl enable audiobookbay

# Start the service
sudo systemctl start audiobookbay

# Check service status
sudo systemctl status audiobookbay
```

### 9. Verify Deployment

1. Check that the container is running:
```bash
sudo docker ps | grep audiobookbay
```

2. Check nginx is running:
```bash
sudo systemctl status nginx
```

3. Test the application:
```bash
curl -I https://abb.bvronan.xyz
```

4. Visit in your browser: `https://abb.bvronan.xyz`

## Post-Deployment

### Create First User

After deployment, navigate to `https://abb.bvronan.xyz/signup` to create your first user account.

### Firewall Configuration

If you're using a firewall (recommended), ensure these ports are open:

```bash
# Allow HTTP (for certbot renewal)
sudo ufw allow 80/tcp

# Allow HTTPS
sudo ufw allow 443/tcp

# Enable firewall (if not already enabled)
sudo ufw enable
```

## Maintenance

### View Logs

```bash
# View Docker container logs
sudo docker-compose -f /opt/audiobookbay-automated/docker-compose.prod.yaml logs -f

# View nginx logs
sudo tail -f /var/log/nginx/abb.bvronan.xyz.access.log
sudo tail -f /var/log/nginx/abb.bvronan.xyz.error.log
```

### Restart Service

```bash
# Restart via systemd
sudo systemctl restart audiobookbay

# Or restart Docker container directly
cd /opt/audiobookbay-automated
sudo docker-compose -f docker-compose.prod.yaml restart
```

### Update Application

```bash
cd /opt/audiobookbay-automated

# Pull latest changes
sudo git pull

# Rebuild and restart
sudo docker-compose -f docker-compose.prod.yaml up -d --build
```

### Backup User Database

The user authentication database is stored at `/opt/audiobookbay-automated/data/users.db`.

Create regular backups:

```bash
# Manual backup
sudo cp /opt/audiobookbay-automated/data/users.db \
       /opt/audiobookbay-automated/data/users.db.backup-$(date +%Y%m%d)

# Automated daily backup (add to crontab)
0 3 * * * cp /opt/audiobookbay-automated/data/users.db /opt/audiobookbay-automated/data/users.db.backup-$(date +\%Y\%m\%d)
```

## Troubleshooting

### Container Won't Start

```bash
# Check container logs
sudo docker-compose -f /opt/audiobookbay-automated/docker-compose.prod.yaml logs

# Check if port 5078 is already in use
sudo netstat -tulpn | grep 5078
```

### Can't Access Website

1. Check nginx status:
```bash
sudo systemctl status nginx
```

2. Check nginx configuration:
```bash
sudo nginx -t
```

3. Check firewall:
```bash
sudo ufw status
```

4. Check DNS:
```bash
nslookup abb.bvronan.xyz
```

### SSL Certificate Issues

```bash
# Check certificate status
sudo certbot certificates

# Renew certificate manually
sudo certbot renew

# Check nginx SSL configuration
sudo nginx -t
```

### Database Issues

```bash
# Check if database file exists
ls -la /opt/audiobookbay-automated/data/users.db

# Check permissions
sudo chmod 644 /opt/audiobookbay-automated/data/users.db
```

## Security Recommendations

1. **Change Default Passwords**: Ensure you've changed all default passwords in `.env`
2. **Use Strong Secret Key**: Generate a strong Flask secret key
3. **Regular Updates**: Keep the system and Docker images updated
4. **Firewall**: Use UFW or iptables to restrict unnecessary ports
5. **Fail2ban**: Consider installing fail2ban to protect against brute force attacks
6. **Backup**: Regularly backup your user database

## Support

For issues or questions:
- Check application logs
- Review nginx error logs
- Consult Docker container logs

## Architecture

```
Internet
   ↓
nginx (HTTPS termination)
   ↓
Docker Container (audiobookbay-automated:5078)
   ↓
qBittorrent (downloads)
```

## Files and Directories

- `/opt/audiobookbay-automated/` - Application root
- `/opt/audiobookbay-automated/data/users.db` - User database
- `/etc/nginx/sites-available/abb.bvronan.xyz` - nginx config
- `/etc/systemd/system/audiobookbay.service` - systemd service
- `/etc/letsencrypt/live/abb.bvronan.xyz/` - SSL certificates
