# Production Deployment Guide

This document provides step-by-step instructions for deploying the PDF Extractor API to production with Redis job storage, multiple workers, and enhanced security features.

## Table of Contents

1. [Job Storage with Redis](#job-storage-with-redis)
2. [Multiple Workers Configuration](#multiple-workers-configuration)
3. [Security Enhancements](#security-enhancements)
4. [Monitoring and Maintenance](#monitoring-and-maintenance)

---

## Job Storage with Redis

### Overview

Currently, jobs are stored in an in-memory dictionary which means:
- Jobs are lost on server restart
- Jobs accumulate indefinitely (never deleted)
- Not suitable for production with multiple workers

Redis provides persistent, shared job storage across workers with automatic expiration.

### Step 1: Add Redis to Railway

1. **Go to your Railway project dashboard**
2. **Click "New" → "Database" → "Add Redis"**
3. **Railway will automatically provision a Redis instance**
4. **Note the connection details** (you'll need these in the next step)

### Step 2: Get Redis Connection Details

In Railway, your Redis service will have:
- **REDIS_URL**: Full connection string (e.g., `redis://default:password@host:port`)
- Or individual variables: `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`

Railway typically provides `REDIS_URL` automatically.

### Step 3: Install Redis Python Client

Add to `requirements.txt`:
```txt
redis>=5.0.0
```

### Step 4: Update Code to Use Redis

Replace the in-memory `jobs` dictionary with Redis. Here's the implementation:

**Add Redis import and setup in `api.py`:**

```python
import redis
import json
from datetime import timedelta

# Redis configuration
REDIS_URL = os.environ.get('REDIS_URL', '')
REDIS_TTL = int(os.environ.get('REDIS_JOB_TTL', 86400))  # 24 hours default

# Initialize Redis client
redis_client = None
if REDIS_URL:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()  # Test connection
        app.logger.info("Redis connected successfully")
    except Exception as e:
        app.logger.error(f"Redis connection failed: {str(e)}")
        redis_client = None
else:
    app.logger.warning("REDIS_URL not set, falling back to in-memory storage")

# Fallback to in-memory if Redis unavailable
jobs = {} if not redis_client else None
```

**Create helper functions for job storage:**

```python
def _get_job(job_id):
    """Get job from Redis or in-memory fallback."""
    if redis_client:
        try:
            job_data = redis_client.get(f"job:{job_id}")
            if job_data:
                return json.loads(job_data)
            return None
        except Exception as e:
            app.logger.error(f"Error getting job from Redis: {str(e)}")
            return None
    else:
        return jobs.get(job_id)

def _set_job(job_id, job_data, ttl=None):
    """Set job in Redis or in-memory fallback."""
    if redis_client:
        try:
            ttl = ttl or REDIS_TTL
            redis_client.setex(
                f"job:{job_id}",
                ttl,
                json.dumps(job_data)
            )
            app.logger.debug(f"Job {job_id} stored in Redis with TTL {ttl}s")
        except Exception as e:
            app.logger.error(f"Error storing job in Redis: {str(e)}")
    else:
        jobs[job_id] = job_data

def _delete_job(job_id):
    """Delete job from Redis or in-memory fallback."""
    if redis_client:
        try:
            redis_client.delete(f"job:{job_id}")
            app.logger.debug(f"Job {job_id} deleted from Redis")
        except Exception as e:
            app.logger.error(f"Error deleting job from Redis: {str(e)}")
    else:
        jobs.pop(job_id, None)
```

**Update job status endpoint to use Redis:**

```python
@app.route('/job/<job_id>', methods=['GET'])
def get_job_status(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    # Rest of the function remains the same...
```

**Update all `jobs[job_id] = ...` to use `_set_job(job_id, ...)`:**

Replace all occurrences of:
- `jobs[job_id] = {...}` → `_set_job(job_id, {...})`
- `jobs[job_id]` → `_get_job(job_id)`

**Auto-delete completed/failed jobs:**

After setting job status to 'completed' or 'failed', set a shorter TTL:

```python
# For completed jobs - keep for 1 hour
_set_job(job_id, job_data, ttl=3600)

# For failed jobs - keep for 24 hours
_set_job(job_id, job_data, ttl=86400)
```

### Step 5: Set Environment Variables in Railway

Add to Railway environment variables:
```
REDIS_URL=redis://default:password@host:port
REDIS_JOB_TTL=86400  # 24 hours (optional, defaults to 86400)
```

### Step 6: Verify Redis Connection

After deployment, check logs:
```bash
# In Railway logs, you should see:
# "Redis connected successfully"
```

### Job Expiration Behavior

- **Active jobs** (processing): Stored with default TTL (24 hours)
- **Completed jobs**: Automatically expire after 1 hour
- **Failed jobs**: Automatically expire after 24 hours
- **Jobs are automatically deleted** when TTL expires - no manual cleanup needed

---

## Multiple Workers Configuration

### Overview

Gunicorn workers allow concurrent request processing. The optimal number depends on your server resources.

### Worker Calculation Formula

**General Rule:**
```
workers = (2 × CPU cores) + 1
```

**For Railway:**
- Railway typically provides 1-2 CPU cores per service
- Recommended: **2-4 workers** for most cases
- Maximum: Don't exceed available memory (each worker uses ~100-200MB)

### Step 1: Update Procfile

Create/update `Procfile`:
```
web: gunicorn api:app --bind 0.0.0.0:${PORT:-5000} --workers 4 --timeout 120 --access-logfile - --error-logfile - --worker-class sync
```

**Worker Options:**
- `--workers 4`: Number of worker processes (adjust based on your needs)
- `--timeout 120`: Request timeout in seconds (important for large PDFs)
- `--worker-class sync`: Use synchronous workers (default, good for I/O-bound tasks)
- `--threads 2`: Alternative to workers (uses threads instead of processes)

### Step 2: Alternative: Use Threads Instead of Workers

For I/O-bound applications (like PDF processing), threads can be more memory-efficient:

```
web: gunicorn api:app --bind 0.0.0.0:${PORT:-5000} --workers 2 --threads 4 --timeout 120 --access-logfile - --error-logfile -
```

This gives you 2 processes × 4 threads = 8 concurrent requests.

### Step 3: Monitor Resource Usage

After deployment, monitor:
- **Memory usage**: Each worker uses memory
- **CPU usage**: More workers = more CPU usage
- **Response times**: Too many workers can cause context switching overhead

**Adjust workers based on:**
- If memory is high → reduce workers
- If CPU is low and requests are queuing → increase workers
- If response times are slow → check if it's worker-related or processing-related

### Step 4: Railway-Specific Considerations

Railway auto-scales based on traffic, but worker count is fixed per instance:
- Start with **2-3 workers**
- Monitor performance
- Adjust based on actual usage

### Step 5: Test Concurrent Processing

Test with multiple simultaneous requests:
```bash
# Test with 5 concurrent requests
for i in {1..5}; do
  curl -X POST -F "file=@test.pdf" \
    -F "send_to_nhost=true" \
    https://your-api.railway.app/extract/async &
done
wait
```

All requests should be accepted and processed concurrently.

---

## Security Enhancements

### Overview

Protect against malicious files, viruses, and dangerous content in PDFs and embeddings.

### 1. Enhanced PDF File Validation

#### Current Protections (Already Implemented)
- ✅ Magic byte validation (`%PDF`)
- ✅ File extension check (`.pdf` only)
- ✅ File size limits (200MB max)
- ✅ PyPDF2 integrity check

#### Additional Security Measures

**A. File Size Validation (Enhanced)**

Add more granular size checks:

```python
# In validate_pdf_file function, add:
MIN_FILE_SIZE = 100  # Minimum 100 bytes (prevents empty/minimal files)

if file_size < MIN_FILE_SIZE:
    return False, "File is too small to be a valid PDF"
```

**B. PDF Structure Validation**

Add deeper PDF structure validation:

```python
def validate_pdf_structure(file_path):
    """
    Validate PDF structure to detect malformed or malicious PDFs.
    
    Returns:
        Tuple of (is_valid: bool, error_message: str)
    """
    try:
        from PyPDF2 import PdfReader
        
        reader = PdfReader(file_path)
        
        # Check for reasonable page count (prevent DoS)
        max_pages = 10000  # Adjust based on your needs
        if len(reader.pages) > max_pages:
            return False, f"PDF has too many pages (max: {max_pages})"
        
        # Try to read first page to ensure it's not corrupted
        if len(reader.pages) > 0:
            first_page = reader.pages[0]
            _ = first_page.extract_text()  # Try to extract text
        
        # Check for embedded JavaScript (potential security risk)
        if '/JavaScript' in reader.trailer.get('/Root', {}):
            app.logger.warning(f"PDF contains JavaScript: {file_path}")
            # Optionally reject: return False, "PDF contains JavaScript (security risk)"
        
        # Check for embedded files (potential malware)
        if '/EmbeddedFiles' in reader.trailer.get('/Root', {}):
            app.logger.warning(f"PDF contains embedded files: {file_path}")
            # Optionally reject: return False, "PDF contains embedded files (security risk)"
        
        return True, None
        
    except Exception as e:
        return False, f"PDF structure validation failed: {str(e)}"
```

**C. Content Scanning (Optional - Advanced)**

For production, consider integrating virus scanning:

```python
# Option 1: ClamAV (open-source antivirus)
# Requires ClamAV daemon running
import pyclamd

def scan_file_with_clamav(file_path):
    """Scan file with ClamAV antivirus."""
    try:
        cd = pyclamd.ClamdUnixSocket()
        result = cd.scan_file(file_path)
        if result:
            return False, f"Virus detected: {result[file_path][1]}"
        return True, None
    except Exception as e:
        app.logger.warning(f"ClamAV scan failed: {str(e)}")
        # Fail open or closed based on your security policy
        return True, None  # Fail open (allow if scan fails)
```

**Add to requirements.txt:**
```txt
pyclamd>=1.0.0  # Optional: For ClamAV integration
```

### 2. Content Filtering for Embeddings

#### A. Text Content Sanitization

Sanitize extracted text before creating embeddings:

```python
import re
import html

def sanitize_text_for_embeddings(text):
    """
    Sanitize text to prevent injection of malicious content in embeddings.
    
    Args:
        text: Raw extracted text
    
    Returns:
        Sanitized text
    """
    if not text:
        return ""
    
    # Remove null bytes (potential injection vector)
    text = text.replace('\x00', '')
    
    # Remove control characters (except newlines and tabs)
    text = re.sub(r'[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F]', '', text)
    
    # Limit text length per chunk (prevent DoS)
    max_chunk_length = 100000  # 100KB per chunk
    if len(text) > max_chunk_length:
        text = text[:max_chunk_length]
        app.logger.warning(f"Text truncated to {max_chunk_length} characters")
    
    # Remove excessive whitespace (but preserve structure)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{10,}', '\n\n', text)  # Max 2 consecutive newlines
    
    return text.strip()
```

**Apply sanitization in chunking:**

```python
# In _chunk_text_for_embeddings, after normalization:
normalized_text = _normalize_text(page_text)
sanitized_text = sanitize_text_for_embeddings(normalized_text)
units = _split_into_semantic_units(sanitized_text)
```

#### B. Dangerous Content Detection

Detect and flag potentially dangerous content:

```python
def detect_dangerous_content(text):
    """
    Detect potentially dangerous content patterns.
    
    Returns:
        Tuple of (is_dangerous: bool, reason: str)
    """
    dangerous_patterns = [
        # SQL injection patterns
        (r'(?i)(union|select|insert|delete|drop|exec|execute).*from', 'Potential SQL injection'),
        # Script injection
        (r'(?i)<script[^>]*>.*?</script>', 'JavaScript code detected'),
        # Command injection
        (r'(?i)(system|exec|eval|subprocess|os\.system)', 'Potential command injection'),
        # Excessive encoded content (potential obfuscation)
        (r'%[0-9A-Fa-f]{2}{100,}', 'Excessive URL encoding (potential obfuscation)'),
    ]
    
    for pattern, reason in dangerous_patterns:
        if re.search(pattern, text):
            app.logger.warning(f"Dangerous content detected: {reason}")
            # Optionally reject: return True, reason
            # Or flag for review: return False, reason (log but allow)
    
    return False, None
```

**Apply detection before storing embeddings:**

```python
# In _send_to_nhost, before creating chunks:
for page_key, page_data in sorted_pages:
    page_text = page_data.get('text', '')
    is_dangerous, reason = detect_dangerous_content(page_text)
    if is_dangerous:
        app.logger.error(f"Rejecting PDF with dangerous content: {reason}")
        return None  # Reject the entire PDF
```

#### C. Embedding Size Limits

Limit embedding size to prevent resource exhaustion:

```python
# In _chunk_text_for_embeddings:
MAX_CHUNKS_PER_PDF = 10000  # Maximum chunks per PDF
MAX_CHARS_PER_CHUNK = 2000  # Maximum characters per chunk (increased from 1000)

# After creating chunks:
if len(chunks) > MAX_CHUNKS_PER_PDF:
    app.logger.warning(f"PDF has {len(chunks)} chunks, limiting to {MAX_CHUNKS_PER_PDF}")
    chunks = chunks[:MAX_CHUNKS_PER_PDF]
```

### 3. Rate Limiting

Prevent abuse with rate limiting:

```python
# Add to requirements.txt:
# flask-limiter>=3.5.0

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["100 per hour", "10 per minute"]
)

# Apply to endpoints:
@app.route('/extract/async', methods=['POST'])
@limiter.limit("5 per minute")  # 5 uploads per minute per IP
def extract_async():
    # ... existing code
```

### 4. Input Validation

Validate all user inputs:

```python
def validate_user_input(user_id, filename, upload_device):
    """Validate user-provided inputs."""
    errors = []
    
    # Validate user_id format (UUID)
    if user_id:
        uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        if not re.match(uuid_pattern, user_id, re.IGNORECASE):
            errors.append("Invalid user_id format (must be UUID)")
    
    # Validate filename
    if filename:
        # Check for path traversal attempts
        if '..' in filename or '/' in filename or '\\' in filename:
            errors.append("Invalid filename (path traversal detected)")
        
        # Check filename length
        if len(filename) > 255:
            errors.append("Filename too long (max 255 characters)")
    
    # Validate upload_device
    if upload_device:
        allowed_devices = ['web', 'mobile', 'api']
        if upload_device not in allowed_devices:
            errors.append(f"Invalid upload_device (must be one of: {', '.join(allowed_devices)})")
    
    return errors
```

### 5. Secure File Handling

**A. Isolated Processing**

Process files in isolated temporary directories:

```python
import tempfile
import shutil

def create_isolated_temp_dir():
    """Create isolated temporary directory for file processing."""
    temp_dir = tempfile.mkdtemp(prefix='pdf_extract_')
    # Set restrictive permissions
    os.chmod(temp_dir, 0o700)  # Only owner can access
    return temp_dir

# Always cleanup temp files:
try:
    # Process file
    pass
finally:
    if os.path.exists(temp_path):
        os.remove(temp_path)
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)
```

**B. File Type Verification**

Double-check file type after upload:

```python
import magic  # python-magic library

def verify_file_type(file_path, expected_type='pdf'):
    """Verify file type using magic bytes (more reliable than extension)."""
    try:
        import magic
        mime = magic.Magic(mime=True)
        file_mime = mime.from_file(file_path)
        
        expected_mimes = {
            'pdf': 'application/pdf'
        }
        
        if file_mime != expected_mimes.get(expected_type):
            return False, f"File type mismatch: expected {expected_mimes[expected_type]}, got {file_mime}"
        
        return True, None
    except ImportError:
        app.logger.warning("python-magic not installed, skipping MIME type check")
        return True, None
```

**Add to requirements.txt:**
```txt
python-magic>=0.4.27  # For MIME type detection
```

### 6. Security Checklist

- [ ] ✅ Magic byte validation
- [ ] ✅ File size limits
- [ ] ✅ PDF structure validation
- [ ] ✅ Text sanitization
- [ ] ✅ Dangerous content detection
- [ ] ✅ Rate limiting
- [ ] ✅ Input validation
- [ ] ✅ Secure file handling
- [ ] ✅ Isolated temp directories
- [ ] ✅ Automatic cleanup
- [ ] ⚠️ Virus scanning (optional - requires ClamAV)
- [ ] ⚠️ MIME type verification (optional - requires python-magic)

---

## Monitoring and Maintenance

### 1. Logging

Ensure comprehensive logging is enabled:

```python
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Log security events
app.logger.warning("Security event: Invalid file upload attempt")
app.logger.error("Security event: Dangerous content detected")
```

### 2. Health Checks

Monitor Redis connection:

```python
@app.route('/health', methods=['GET'])
def health_check():
    health = {
        'status': 'healthy',
        'service': 'PDF Extractor API',
        'redis': 'connected' if redis_client and redis_client.ping() else 'disconnected'
    }
    return jsonify(health)
```

### 3. Metrics to Monitor

- **Job completion rate**: Track successful vs failed jobs
- **Processing time**: Average time per PDF
- **Redis connection**: Monitor Redis availability
- **Memory usage**: Watch for memory leaks
- **Error rates**: Track validation failures, security events

### 4. Regular Maintenance

- **Update dependencies**: Regularly update packages for security patches
- **Review logs**: Check for security events and errors
- **Monitor Redis**: Ensure Redis is healthy and not running out of memory
- **Cleanup**: Verify expired jobs are being deleted from Redis

---

## Summary

### Quick Setup Checklist

1. **Redis Setup:**
   - [ ] Add Redis service in Railway
   - [ ] Add `redis>=5.0.0` to requirements.txt
   - [ ] Update code to use Redis helpers
   - [ ] Set `REDIS_URL` environment variable
   - [ ] Test Redis connection

2. **Multiple Workers:**
   - [ ] Update Procfile with worker count
   - [ ] Deploy and monitor resource usage
   - [ ] Adjust workers based on performance

3. **Security:**
   - [ ] Implement enhanced PDF validation
   - [ ] Add text sanitization
   - [ ] Add dangerous content detection
   - [ ] Implement rate limiting (optional)
   - [ ] Add input validation
   - [ ] Set up secure file handling

4. **Monitoring:**
   - [ ] Enable comprehensive logging
   - [ ] Set up health checks
   - [ ] Monitor key metrics

---

## Additional Resources

- [Redis Python Client Documentation](https://redis-py.readthedocs.io/)
- [Gunicorn Configuration](https://docs.gunicorn.org/en/stable/settings.html)
- [Railway Redis Documentation](https://docs.railway.app/databases/redis)
- [PDF Security Best Practices](https://owasp.org/www-community/vulnerabilities/Unrestricted_File_Upload)
- [Flask Security Guide](https://flask.palletsprojects.com/en/2.3.x/security/)

---

## Troubleshooting

### Redis Connection Issues

**Problem:** "Redis connection failed"
- **Solution:** Check `REDIS_URL` environment variable is set correctly
- **Solution:** Verify Redis service is running in Railway
- **Solution:** Check Railway logs for Redis connection errors

### High Memory Usage

**Problem:** Memory usage is too high
- **Solution:** Reduce number of Gunicorn workers
- **Solution:** Use threads instead of workers
- **Solution:** Check for memory leaks in job storage

### Jobs Not Expiring

**Problem:** Jobs remain in Redis after completion
- **Solution:** Verify TTL is being set correctly
- **Solution:** Check Redis TTL with: `redis-cli TTL job:job_id`
- **Solution:** Ensure `_set_job` is called with TTL parameter

### Security Warnings

**Problem:** Getting security warnings in logs
- **Solution:** Review detected patterns - may be false positives
- **Solution:** Adjust detection patterns based on your use case
- **Solution:** Consider implementing a review queue for flagged content

