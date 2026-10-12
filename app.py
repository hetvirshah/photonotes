"""
PhotoNotes - Production-ready demonstration of AWS Cloud Architecture.
Runs on Amazon EC2 behind an Application Load Balancer (ALB), storing note
metadata in Amazon RDS MySQL and photos in a private Amazon S3 bucket.

Architecture Highlights:
1. ALB Shallow Health Check (/health) decoupled from RDS/S3 dependencies.
2. EC2 Instance Metadata Service v2 (IMDSv2) token-based metadata retrieval.
3. IAM Instance Profile for secure S3 access (zero hardcoded credentials).
4. S3 Presigned URLs for secure, direct-to-browser private object rendering.
5. RDS Multi-AZ failover resilience via per-request connection management & retries.
6. LOCAL_MODE flag for offline development with SQLite and local filesystem storage.
"""

import os
import sys
import uuid
import time
import logging
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime

from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    redirect,
    url_for,
    send_from_directory,
    abort,
)
from werkzeug.exceptions import RequestEntityTooLarge

# -----------------------------------------------------------------------------
# 1. LOGGING SETUP (STDOUT for systemd journalctl & CloudWatch)
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [PhotoNotes] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("photonotes")

# -----------------------------------------------------------------------------
# 2. CONFIGURATION & ENVIRONMENT VARIABLES
# -----------------------------------------------------------------------------
LOCAL_MODE = os.environ.get("LOCAL_MODE", "0").lower() in ("1", "true", "yes")

# Database configuration (RDS MySQL)
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "photonotes")

# AWS S3 configuration
S3_BUCKET = os.environ.get("S3_BUCKET", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Local storage configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
SQLITE_DB_PATH = os.path.join(BASE_DIR, "photonotes.db")

# Upload constraints
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif"}
MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 Megabytes
MAX_NOTE_LENGTH = 500

# Conditional imports for AWS vs Local
if not LOCAL_MODE:
    import pymysql
    import pymysql.cursors
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    # Initialize S3 Client using EC2 Instance Role (default credential chain)
    # NEVER pass aws_access_key_id or aws_secret_access_key here.
    logger.info("Initializing S3 client via EC2 Instance Profile default credential chain...")
    s3_client = boto3.client("s3", region_name=AWS_REGION)
else:
    s3_client = None
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    logger.info(f"Running in LOCAL_MODE: SQLite database at {SQLITE_DB_PATH}, local uploads at {UPLOAD_FOLDER}")

# -----------------------------------------------------------------------------
# 3. FLASK APPLICATION INITIALIZATION
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

# Global in-memory cache for EC2 instance metadata
_METADATA_CACHE = None


# -----------------------------------------------------------------------------
# 4. INSTANCE METADATA (IMDSv2)
# -----------------------------------------------------------------------------
def get_instance_metadata():
    """
    Fetches EC2 Instance ID and Availability Zone using IMDSv2.
    
    IMDSv2 requires:
    1. A PUT request with header 'X-aws-ec2-metadata-token-ttl-seconds' to obtain a session token.
    2. Subsequent GET requests with header 'X-aws-ec2-metadata-token' containing the token.
    
    Uses short 1-second timeouts, caches the result in memory, and gracefully falls back
    to ('local', 'local') if run outside EC2 or in LOCAL_MODE.
    """
    global _METADATA_CACHE
    if _METADATA_CACHE is not None:
        return _METADATA_CACHE

    if LOCAL_MODE:
        _METADATA_CACHE = {"instance_id": "local", "availability_zone": "local"}
        return _METADATA_CACHE

    imds_base = "http://169.254.169.254/latest"
    timeout_sec = 1.0

    try:
        # Step 1: Request IMDSv2 session token
        token_req = urllib.request.Request(
            f"{imds_base}/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
            method="PUT",
        )
        with urllib.request.urlopen(token_req, timeout=timeout_sec) as token_resp:
            token = token_resp.read().decode("utf-8").strip()

        # Step 2: Fetch instance-id
        id_req = urllib.request.Request(
            f"{imds_base}/meta-data/instance-id",
            headers={"X-aws-ec2-metadata-token": token},
        )
        with urllib.request.urlopen(id_req, timeout=timeout_sec) as id_resp:
            instance_id = id_resp.read().decode("utf-8").strip()

        # Step 3: Fetch availability-zone
        az_req = urllib.request.Request(
            f"{imds_base}/meta-data/placement/availability-zone",
            headers={"X-aws-ec2-metadata-token": token},
        )
        with urllib.request.urlopen(az_req, timeout=timeout_sec) as az_resp:
            availability_zone = az_resp.read().decode("utf-8").strip()

        _METADATA_CACHE = {
            "instance_id": instance_id,
            "availability_zone": availability_zone,
        }
        logger.info(f"Retrieved IMDSv2 metadata: instance_id={instance_id}, az={availability_zone}")
    except Exception as e:
        logger.warning(
            f"IMDSv2 metadata unreachable ({e}). Falling back to 'local' / 'local'."
        )
        _METADATA_CACHE = {"instance_id": "local", "availability_zone": "local"}

    return _METADATA_CACHE


# -----------------------------------------------------------------------------
# 5. DATABASE HELPERS & CONNECTION MANAGEMENT
# -----------------------------------------------------------------------------
class DatabaseError(Exception):
    """Custom exception wrapper for operational database failures."""
    pass


def get_db_connection():
    """
    Opens a fresh database connection per request.
    This ensures that during an Amazon RDS Multi-AZ failover, the subsequent request
    automatically resolves DNS to the new primary instance instead of reusing a broken socket.
    """
    if LOCAL_MODE:
        try:
            conn = sqlite3.connect(SQLITE_DB_PATH, timeout=5.0)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError as e:
            logger.error(f"Local SQLite operational error: {e}")
            raise DatabaseError(str(e)) from e
    else:
        try:
            conn = pymysql.connect(
                host=DB_HOST,
                user=DB_USER,
                password=DB_PASSWORD,
                database=DB_NAME,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=3,
                read_timeout=5,
                write_timeout=5,
                autocommit=True,
            )
            return conn
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as e:
            logger.error(f"Amazon RDS connection failed: {e}")
            raise DatabaseError(str(e)) from e


def init_db(max_retries=5, delay_seconds=3):
    """
    Creates the notes table if missing. Retries initial connection so that
    the application survives starting up before RDS is fully available.
    """
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Database initialization (attempt {attempt}/{max_retries})...")
            if LOCAL_MODE:
                conn = sqlite3.connect(SQLITE_DB_PATH)
                try:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS notes (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            note_text TEXT NOT NULL,
                            s3_key TEXT NOT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                        """
                    )
                    conn.commit()
                finally:
                    conn.close()
            else:
                conn = get_db_connection()
                try:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            """
                            CREATE TABLE IF NOT EXISTS notes (
                                id INT AUTO_INCREMENT PRIMARY KEY,
                                note_text VARCHAR(500) NOT NULL,
                                s3_key VARCHAR(255) NOT NULL,
                                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                            """
                        )
                finally:
                    conn.close()

            logger.info("Database schema verified and ready.")
            return True
        except Exception as e:
            logger.warning(f"Database initialization attempt {attempt} failed: {e}")
            if attempt < max_retries:
                time.sleep(delay_seconds)
            else:
                logger.error("Database initialization failed after maximum retries. Continuing startup.")
                return False


def query_recent_notes(limit=50):
    """Fetches the most recent notes using parameterized query."""
    conn = get_db_connection()
    try:
        if LOCAL_MODE:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, note_text, s3_key, created_at FROM notes ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = cursor.fetchall()
            return [
                {
                    "id": row["id"],
                    "note_text": row["note_text"],
                    "s3_key": row["s3_key"],
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ]
        else:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, note_text, s3_key, created_at FROM notes ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
                rows = cursor.fetchall()
                results = []
                for row in rows:
                    created_at = row["created_at"]
                    if isinstance(created_at, datetime):
                        created_str = created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                    else:
                        created_str = str(created_at)
                    results.append(
                        {
                            "id": row["id"],
                            "note_text": row["note_text"],
                            "s3_key": row["s3_key"],
                            "created_at": created_str,
                        }
                    )
                return results
    finally:
        conn.close()


def insert_note(note_text, s3_key):
    """Inserts a note record using parameterized query."""
    conn = get_db_connection()
    try:
        if LOCAL_MODE:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO notes (note_text, s3_key) VALUES (?, ?)",
                (note_text, s3_key),
            )
            conn.commit()
            return cursor.lastrowid
        else:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO notes (note_text, s3_key) VALUES (%s, %s)",
                    (note_text, s3_key),
                )
                return cursor.lastrowid
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# 6. S3 & STORAGE HELPERS
# -----------------------------------------------------------------------------
def allowed_file(filename):
    """Checks file extension against allowed image types."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_photo_url(s3_key):
    """
    Generates a secure presigned GET URL for an S3 object (valid for 15 minutes).
    In LOCAL_MODE, returns the URL path to the local /uploads route.
    """
    if LOCAL_MODE:
        return url_for("uploaded_file", filename=s3_key)

    try:
        presigned_url = s3_client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": S3_BUCKET, "Key": s3_key},
            ExpiresIn=900,  # 15 minutes
        )
        return presigned_url
    except Exception as e:
        logger.error(f"Failed to generate S3 presigned URL for key '{s3_key}': {e}")
        return ""


def store_photo(file_storage, unique_key):
    """
    Saves an uploaded photo either to the private S3 bucket or to local uploads/.
    """
    if LOCAL_MODE:
        destination_path = os.path.join(UPLOAD_FOLDER, unique_key)
        file_storage.save(destination_path)
        logger.info(f"[LOCAL] Stored photo locally: {destination_path}")
    else:
        try:
            content_type = file_storage.content_type or "application/octet-stream"
            s3_client.upload_fileobj(
                file_storage,
                S3_BUCKET,
                unique_key,
                ExtraArgs={"ContentType": content_type},
            )
            logger.info(f"[AWS] Uploaded photo to s3://{S3_BUCKET}/{unique_key}")
        except (BotoCoreError, ClientError) as e:
            logger.error(f"S3 upload failed for key '{unique_key}': {e}")
            raise


# -----------------------------------------------------------------------------
# 7. ROUTE HANDLERS
# -----------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health_shallow():
    """
    CRITICAL ALB HEALTH CHECK:
    Returns HTTP 200 {"status":"ok"}.
    MUST NOT touch the database or S3!
    This ensures that during a database failover or transient network issue with RDS,
    the ALB does not mark perfectly healthy EC2 instances as dead and tear them down.
    """
    return jsonify({"status": "ok"}), 200


@app.route("/health/deep", methods=["GET"])
def health_deep():
    """
    INFORMATIONAL DEEP HEALTH CHECK:
    Tests the actual database connectivity and returns HTTP 200 or 503.
    Intended for monitoring systems and operators, NOT for ALB target health.
    """
    try:
        conn = get_db_connection()
        if LOCAL_MODE:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
        else:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        conn.close()
        return jsonify({"status": "ok", "database": "connected", "mode": "local" if LOCAL_MODE else "aws"}), 200
    except Exception as e:
        logger.warning(f"Deep health check failed: {e}")
        return (
            jsonify({
                "status": "unhealthy",
                "database": "disconnected",
                "error": "Database connectivity failed",
            }),
            503,
        )


@app.route("/", methods=["GET"])
def index():
    """
    Home page route:
    Renders note submission form, recent notes feed (with presigned S3 URLs),
    and the EC2 instance ID & Availability Zone banner for load balancing demonstrations.
    """
    metadata = get_instance_metadata()
    error_msg = request.args.get("error")
    success_msg = request.args.get("message")

    try:
        raw_notes = query_recent_notes(limit=50)
        # Augment each note with its 15-minute presigned S3 URL
        notes = []
        for note in raw_notes:
            notes.append({
                "id": note["id"],
                "note_text": note["note_text"],
                "s3_key": note["s3_key"],
                "created_at": note["created_at"],
                "photo_url": get_photo_url(note["s3_key"]),
            })
        return render_template(
            "index.html",
            metadata=metadata,
            notes=notes,
            error=error_msg,
            message=success_msg,
        )
    except DatabaseError:
        logger.warning("Rendering 503 error page due to DatabaseError on index route.")
        return render_template("503.html"), 503


@app.route("/notes", methods=["POST"])
def create_note():
    """
    Validates input (note_text max 500 chars, photo jpg/jpeg/png/gif max 5 MB),
    uploads photo to S3 under unique key (uuid + extension), inserts database row,
    and redirects back to /.
    """
    note_text = request.form.get("note_text", "").strip()

    # 1. Validate note text
    if not note_text:
        return redirect(url_for("index", error="Note text is required."))
    if len(note_text) > MAX_NOTE_LENGTH:
        return redirect(url_for("index", error=f"Note exceeds maximum length of {MAX_NOTE_LENGTH} characters."))

    # 2. Validate attached photo
    if "photo" not in request.files:
        return redirect(url_for("index", error="A photo attachment is required."))

    photo = request.files["photo"]
    if not photo or photo.filename == "":
        return redirect(url_for("index", error="No photo selected."))

    if not allowed_file(photo.filename):
        return redirect(url_for("index", error="Invalid file format. Allowed: JPG, JPEG, PNG, GIF."))

    # 3. Generate unique S3 key using UUID4 + original extension
    file_ext = photo.filename.rsplit(".", 1)[1].lower()
    unique_key = f"{uuid.uuid4().hex}.{file_ext}"

    # 4. Store photo in S3 (or local uploads/)
    try:
        store_photo(photo, unique_key)
    except Exception as e:
        logger.error(f"Failed to store photo: {e}")
        return redirect(url_for("index", error="Failed to store photo attachment in S3."))

    # 5. Insert row into database
    try:
        insert_note(note_text, unique_key)
        logger.info(f"Successfully recorded note with S3 key '{unique_key}'.")
    except DatabaseError:
        logger.error(f"Database error saving note metadata for key '{unique_key}'.")
        return render_template("503.html"), 503

    return redirect(url_for("index", message="Photo note posted successfully!"))


@app.route("/notes", methods=["GET"])
def list_notes():
    """
    API endpoint:
    Returns JSON list of the 50 most recent notes, each containing a presigned
    S3 GET URL (expires in 15 minutes) for its photo.
    """
    try:
        raw_notes = query_recent_notes(limit=50)
        notes = []
        for note in raw_notes:
            notes.append({
                "id": note["id"],
                "note_text": note["note_text"],
                "s3_key": note["s3_key"],
                "created_at": note["created_at"],
                "photo_url": get_photo_url(note["s3_key"]),
            })
        return jsonify(notes), 200
    except DatabaseError:
        return jsonify({"error": "Database service temporarily unavailable"}), 503


@app.route("/uploads/<filename>", methods=["GET"])
def uploaded_file(filename):
    """
    Local mode only helper:
    Serves files from local ./uploads directory for offline testing.
    """
    if not LOCAL_MODE:
        abort(404)
    return send_from_directory(UPLOAD_FOLDER, filename)


# -----------------------------------------------------------------------------
# 8. ERROR HANDLERS (Clean HTTP codes without exposing stack traces)
# -----------------------------------------------------------------------------

@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(e):
    """Handles uploads exceeding MAX_CONTENT_LENGTH (5 MB)."""
    if request.path == "/notes" and request.headers.get("Accept") == "application/json":
        return jsonify({"error": "File size exceeds 5 MB limit"}), 413
    return redirect(url_for("index", error="File size exceeds the 5 MB limit."))


@app.errorhandler(503)
def handle_service_unavailable(e):
    return render_template("503.html"), 503


@app.errorhandler(404)
def handle_not_found(e):
    return jsonify({"error": "Resource not found"}), 404


@app.errorhandler(500)
def handle_internal_error(e):
    logger.error(f"Unhandled server error: {e}")
    return jsonify({"error": "An unexpected error occurred. Please try again."}), 500


# -----------------------------------------------------------------------------
# 9. STARTUP ENTRY POINT
# -----------------------------------------------------------------------------
# Initialize DB tables on module load / startup
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting PhotoNotes dev server on port {port}...")
    app.run(host="0.0.0.0", port=port, debug=False)
