# Deployment Guide

This guide covers deploying the PDF Extractor API to production, with options for EC2 and modern alternatives.

## Deployment Options Comparison

### Option 1: AWS EC2 (Full Control)
**Pros:**
- Full control over the environment
- Can handle large files and long processing times
- Cost-effective for consistent workloads
- Good for learning AWS

**Cons:**
- Requires server management
- Need to set up SSL, monitoring, backups
- More setup time

### Option 2: Railway / Render / Fly.io (Recommended for Start)
**Pros:**
- Zero-config deployments
- Automatic SSL certificates
- Built-in monitoring
- Easy scaling
- Free tiers available

**Cons:**
- Less control
- May have timeout limits for long-running tasks
- Can be more expensive at scale

### Option 3: AWS Lambda + API Gateway (Serverless)
**Pros:**
- Pay per request
- Auto-scaling
- No server management

**Cons:**
- 15-minute timeout limit
- Cold starts
- More complex setup
- May need S3 for large files

## Recommended: Railway or Render (Easiest)

For your use case with Next.js and Nhost, I recommend **Railway** or **Render** because:
- Quick setup (5 minutes)
- Automatic HTTPS
- Environment variables management
- Built-in logs
- Easy integration with your Next.js app

---

## Deployment: Railway (Recommended)

### Step 1: Prepare Your Code

1. Create a `Procfile`:
```
web: gunicorn api:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
```

2. Update `requirements.txt` (already done)

3. Create `runtime.txt` (optional, for specific Python version):
```
python-3.11.0
```

### Step 2: Deploy to Railway

1. Sign up at [railway.app](https://railway.app)
2. Click "New Project" → "Deploy from GitHub repo"
3. Connect your repository
4. Railway auto-detects Python and installs dependencies
5. Add environment variables:
   - `NHOST_BACKEND_URL`: Your Nhost backend URL
   - `NHOST_ADMIN_SECRET`: Your Nhost admin secret (for server-side operations)
   - `WEBHOOK_URL`: (Optional) Your Next.js webhook endpoint for completion callbacks
   - `PORT`: Railway sets this automatically

6. Deploy! Your API will be live at `https://your-app.railway.app`

### Step 3: Test Your Deployment

```bash
curl https://your-app.railway.app/health
```

---

## Deployment: AWS EC2 (Full Control)

### Step 1: Launch EC2 Instance

1. Go to AWS Console → EC2 → Launch Instance
2. Choose Ubuntu 22.04 LTS
3. Instance type: `t3.small` or larger (2GB+ RAM recommended)
4. Configure security group:
   - SSH (22) from your IP
   - HTTP (80) from anywhere
   - HTTPS (443) from anywhere
   - Custom TCP (5000) from anywhere (or just your Next.js app IP)
5. Launch and download key pair

### Step 2: Connect and Setup

```bash
# Connect to your instance
ssh -i your-key.pem ubuntu@your-ec2-ip

# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and dependencies
sudo apt install -y python3 python3-pip python3-venv nginx

# Install system dependencies for PDF processing
sudo apt install -y build-essential libpoppler-cpp-dev pkg-config python3-dev
```

### Step 3: Deploy Application

```bash
# Create app directory
mkdir -p /home/ubuntu/pdf-extractor
cd /home/ubuntu/pdf-extractor

# Clone your repo (or use git pull if already cloned)
git clone https://github.com/yourusername/pdf-extractor.git .

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt gunicorn

# Create .env file
nano .env
```

Add to `.env`:
```
NHOST_BACKEND_URL=https://your-project.nhost.run
NHOST_ADMIN_SECRET=your-admin-secret
WEBHOOK_URL=https://your-nextjs-app.com/api/webhook
FLASK_DEBUG=False
PORT=5000
```

### Step 4: Create Systemd Service

```bash
sudo nano /etc/systemd/system/pdf-extractor.service
```

Add:
```ini
[Unit]
Description=PDF Extractor API
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/pdf-extractor
Environment="PATH=/home/ubuntu/pdf-extractor/venv/bin"
EnvironmentFile=/home/ubuntu/pdf-extractor/.env
ExecStart=/home/ubuntu/pdf-extractor/venv/bin/gunicorn api:app --bind 0.0.0.0:5000 --workers 2 --timeout 120

[Install]
WantedBy=multi-user.target
```

Start service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable pdf-extractor
sudo systemctl start pdf-extractor
sudo systemctl status pdf-extractor
```

### Step 5: Setup Nginx Reverse Proxy

```bash
sudo nano /etc/nginx/sites-available/pdf-extractor
```

Add:
```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }
}
```

Enable site:
```bash
sudo ln -s /etc/nginx/sites-available/pdf-extractor /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

### Step 6: Setup SSL with Let's Encrypt

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

### Step 7: Configure Firewall

```bash
sudo ufw allow 22
sudo ufw allow 80
sudo ufw allow 443
sudo ufw enable
```

---

## Environment Variables

Set these in your deployment platform:

| Variable | Description | Required |
|----------|-------------|----------|
| `NHOST_BACKEND_URL` | Your Nhost backend URL (e.g., `https://xxx.nhost.run`) | Yes |
| `NHOST_ADMIN_SECRET` | Nhost admin secret for server-side operations | Yes |
| `WEBHOOK_URL` | Next.js webhook endpoint for completion callbacks | No |
| `PORT` | Port to run the server (usually auto-set) | Auto |
| `FLASK_DEBUG` | Debug mode (set to `False` in production) | No |
| `CORS_ORIGINS` | Comma-separated allowed origins (e.g., `https://your-app.com,https://app.vercel.app`) | No |

---

## Monitoring & Logs

### Railway/Render
- Logs available in dashboard
- Set up alerts for errors

### EC2
```bash
# View logs
sudo journalctl -u pdf-extractor -f

# Restart service
sudo systemctl restart pdf-extractor
```

---

## Next Steps

1. Deploy your API using one of the methods above
2. Update your Next.js app to use the deployed API URL
3. Test the integration
4. Set up monitoring and alerts

