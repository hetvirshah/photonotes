# PhotoNotes

A clean Flask web application designed to demonstrate a resilient, production-ready AWS architecture:
**Application Load Balancer (ALB) &rarr; EC2 Auto Scaling &rarr; Amazon RDS MySQL (Multi-AZ) &rarr; Private Amazon S3**.

---

## 🏛 Architecture Overview & AWS Mapping

| Component | AWS Resource | Implementation & Design Decision |
| :--- | :--- | :--- |
| **Ingress & Load Balancing** | Application Load Balancer (ALB) | Targets EC2 instances across multiple Availability Zones. Uses shallow `/health` check so database failovers do not trigger unhealthy target eviction. |
| **Compute & Scalability** | Amazon EC2 in Auto Scaling Group | Runs Gunicorn (2 workers) managed by systemd (`photonotes.service`). Fetches Instance ID and AZ via **IMDSv2** to show active target routing on the UI. |
| **Security & IAM** | IAM EC2 Instance Profile | Zero hardcoded AWS credentials or API keys. Uses the default AWS SDK credential chain to interact securely with S3. |
| **Relational Storage** | Amazon RDS MySQL (Multi-AZ) | Connects via PyMySQL with parameterized queries. Opens fresh connections per request and handles `OperationalError` gracefully so failovers reconnect automatically on the subsequent request. |
| **Object Storage** | Private Amazon S3 Bucket | Bucket is private with all public access blocked. Photos are uploaded with UUID keys, and the application generates short-lived (15-minute) **Presigned GET URLs** for browsers. |
| **Local Testing** | SQLite + Local `./uploads` | Activated with `LOCAL_MODE=1` so you can test all flows offline without an AWS account. |

---

## 🧭 Routes & Endpoints

| HTTP Method | Route | Description |
| :--- | :--- | :--- |
| `GET` | `/health` | **Shallow ALB Health Check**: Returns `{"status":"ok"}` (HTTP 200). Does **not** touch RDS or S3. |
| `GET` | `/health/deep` | **Deep Health Check**: Tests RDS connectivity. Returns `200 OK` or `503 Service Unavailable` for operator diagnostics. |
| `GET` | `/` | **Home Page**: Displays EC2 instance metadata banner, note creation form, and recent notes feed with presigned S3 URLs. |
| `POST` | `/notes` | **Create Note**: Validates note text (&le; 500 chars) and photo (&le; 5 MB, jpg/png/gif), uploads to S3, saves row in RDS, and redirects to `/`. |
| `GET` | `/notes` | **Notes API**: Returns JSON array of the 50 most recent notes with presigned S3 URLs. |
| `GET` | `/uploads/<file>` | *(Local Mode only)* Serves uploaded photos when running on a local development machine. |

---

## 💻 Local Quickstart (`LOCAL_MODE=1`)

You can run and test PhotoNotes on your laptop without any AWS account or MySQL setup.

### 1. Prerequisites
- Python 3.9+
- `pip` and `virtualenv`

### 2. Setup Virtual Environment & Install Dependencies
```bash
# Clone or navigate to the project directory
cd photonotes

# Create and activate virtual environment
python -m venv venv

# On Linux/macOS:
source venv/bin/activate

# On Windows:
.\venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Run Locally in Local Mode
```bash
# Set LOCAL_MODE=1 and run the application
# On Linux/macOS:
export LOCAL_MODE=1
python app.py

# On Windows (PowerShell):
$env:LOCAL_MODE="1"
python app.py
```
The application will start on `http://127.0.0.1:5000`.

---

## 🧪 Local Test Plan

### Step 1: Verify Shallow ALB Health Check
```bash
curl -i http://127.0.0.1:5000/health
```
**Expected Output:**
```http
HTTP/1.1 200 OK
Content-Type: application/json

{"status":"ok"}
```

### Step 2: Verify Deep Health Check
```bash
curl -i http://127.0.0.1:5000/health/deep
```
**Expected Output:**
```json
{
  "database": "connected",
  "mode": "local",
  "status": "ok"
}
```

### Step 3: Create a Test Image & Post a Note via `curl`
Create a quick test image:
```bash
# On Linux/macOS/Git Bash:
echo "Fake JPEG data" > test_photo.jpg

# On Windows PowerShell:
Set-Content -Path test_photo.jpg -Value "Fake JPEG data"
```

Submit a note with the attached photo:
```bash
curl -i -X POST http://127.0.0.1:5000/notes \
  -F "note_text=Testing PhotoNotes EC2 and S3 integration" \
  -F "photo=@test_photo.jpg"
```
**Expected Output:**
`HTTP/1.1 302 FOUND` redirecting to `Location: /?message=Photo+note+posted+successfully%21`.

### Step 4: Verify Notes JSON API & Presigned URLs
```bash
curl -s http://127.0.0.1:5000/notes | python -m json.tool
```
**Expected Output:**
```json
[
    {
        "created_at": "2026-10-12 16:30:00",
        "id": 1,
        "note_text": "Testing PhotoNotes EC2 and S3 integration",
        "photo_url": "/uploads/60a92f89f2a9446d84a7e326b52c009d.jpg",
        "s3_key": "60a92f89f2a9446d84a7e326b52c009d.jpg"
    }
]
```

### Step 5: Test Web Browser UI
Open [http://localhost:5000](http://localhost:5000) in your browser:
- Observe the **EC2 Instance Metadata Banner** (`Instance ID: local`, `AZ: local`).
- Upload a photo with a caption.
- Confirm the card appears with the presigned badge and formatted timestamp.

---

## 🚀 EC2 Deployment (Production)

### 1. EC2 Instance Preparation (Amazon Linux 2023)
1. Attach an **IAM Instance Profile** with permissions to the private S3 bucket:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": ["s3:PutObject", "s3:GetObject"],
         "Resource": "arn:aws:s3:::my-photonotes-app-bucket-2026/*"
       }
     ]
   }
   ```
2. Enable **IMDSv2** (required) on the EC2 instance launch settings or Auto Scaling Launch Template.

### 2. Install App & Systemd Unit
```bash
sudo yum update -y
sudo yum install -y python3-pip git

# Deploy application to /opt/photonotes
sudo mkdir -p /opt/photonotes
sudo chown -R ec2-user:ec2-user /opt/photonotes
cd /opt/photonotes

# Clone or copy application files
python3 -m venv /opt/photonotes/venv
/opt/photonotes/venv/bin/pip install -r requirements.txt
```

### 3. Configure Environment Variables
Copy and fill in `/etc/photonotes.env`:
```bash
sudo cp .env.example /etc/photonotes.env
sudo chmod 600 /etc/photonotes.env
sudo chown ec2-user:ec2-user /etc/photonotes.env
```
Edit `/etc/photonotes.env` with your RDS endpoint and S3 bucket:
```ini
LOCAL_MODE=0
DB_HOST=my-mysql-cluster.cluster-xyz.us-east-1.rds.amazonaws.com
DB_USER=admin
DB_PASSWORD=MySecurePassword123!
DB_NAME=photonotes
S3_BUCKET=my-photonotes-app-bucket-2026
AWS_REGION=us-east-1
PORT=5000
```

### 4. Enable and Start Service
```bash
sudo cp photonotes.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now photonotes.service

# Inspect logs via journalctl
sudo journalctl -u photonotes.service -f
```

### 5. Application Load Balancer Configuration
- **Target Group**: Port 5000, Protocol HTTP.
- **Health Check Path**: `/health` (Interval: 10s, Healthy Threshold: 2, Timeout: 3s).
- **Security Groups**:
  - ALB SG allows inbound 80/443 from Internet.
  - EC2 SG allows inbound 5000 only from ALB SG.
  - RDS SG allows inbound 3306 only from EC2 SG.
