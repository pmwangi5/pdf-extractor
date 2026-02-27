#!/usr/bin/env python3
"""
Web API for PDF Data Extraction

A Flask-based REST API for extracting text, metadata, and tables from PDF files.
Optimized for large PDFs (800+ pages) with intelligent text chunking for embeddings.

Features:
- Synchronous and asynchronous PDF extraction
- Intelligent text chunking for chatbot embeddings
- Progress tracking for long-running operations
- Nhost/Hasura GraphQL integration
- Webhook notifications
- Support for large car manuals and technical documents

Author: PDF Extractor Team
License: Open Source
"""

import os
import re
import json
import tempfile
import threading
import uuid
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import requests
import boto3
from botocore.exceptions import ClientError
from pdf_extractor import PDFExtractor

# OpenAI — soft import so the app starts even without the package
try:
    from openai import OpenAI as _OpenAIClient
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# ---------------------------------------------------------------------------
# Concurrency limiter
# At most MAX_CONCURRENT_JOBS PDFs may be processed simultaneously.
# Any request that arrives when all slots are taken gets a 503 immediately.
# ---------------------------------------------------------------------------
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", 10))
_concurrency_semaphore = threading.Semaphore(MAX_CONCURRENT_JOBS)
_active_job_count = 0
_active_job_lock = threading.Lock()

# Try to import Redis for job storage
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# Try to import pdf2image for PDF to JPG conversion
try:
    from pdf2image import convert_from_path
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    try:
        from pdf2image import convert_from_bytes
        PDF2IMAGE_AVAILABLE = True
    except ImportError:
        PDF2IMAGE_AVAILABLE = False
        # Logger will be available after app is created

# Load environment variables
load_dotenv()

app = Flask(__name__)

# CORS configuration
cors_origins = os.environ.get('CORS_ORIGINS', '*')
if cors_origins != '*':
    cors_origins = [origin.strip() for origin in cors_origins.split(',')]
CORS(app, origins=cors_origins)

# Configuration
ALLOWED_EXTENSIONS = {'pdf'}
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB (increased for large car manuals)
MIN_FILE_SIZE = 100  # Minimum 100 bytes (prevents empty/minimal files)
MAX_PDF_PAGES = 10000  # Maximum pages per PDF (prevent DoS)
MAX_CHUNKS_PER_PDF = 10000  # Maximum chunks per PDF
MAX_CHARS_PER_CHUNK = 3000  # Maximum characters per chunk (increased for better context)
MAX_CHUNK_LENGTH = 100000  # Maximum characters per chunk before truncation
UPLOAD_FOLDER = tempfile.gettempdir()

# Nhost configuration
NHOST_BACKEND_URL = os.environ.get('NHOST_BACKEND_URL', '').rstrip('/')
NHOST_ADMIN_SECRET = os.environ.get('NHOST_ADMIN_SECRET', '')
# Optional: Override GraphQL endpoint if different from default
NHOST_GRAPHQL_URL = os.environ.get('NHOST_GRAPHQL_URL', '')
# WEBHOOK_URL: Optional URL to your Next.js API route that receives notifications
# Example: https://your-app.vercel.app/api/webhook/pdf-extraction
# This is called when PDF processing completes (success or failure)
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')

# DigitalOcean Spaces (S3-compatible) configuration
DO_SPACES_URL = os.environ.get('DO_SPACES_URL', '')  # Full URL or endpoint, e.g., https://nyc3.digitaloceanspaces.com or nyc3.digitaloceanspaces.com
DO_SPACES_ID = os.environ.get('DO_SPACES_ID', '')  # Access key ID
DO_SPACES_SECRET = os.environ.get('DO_SPACES_SECRET', '')  # Secret access key
DO_SPACES_BUCKET = os.environ.get('DO_SPACES_BUCKET', '')
DO_SPACES_FOLDER = 'docs_pdf_embedding_sources'  # Folder name in Spaces

# AWS SES (Simple Email Service) configuration
AWS_SES_REGION = os.environ.get('AWS_SES_REGION', 'eu-central-1')
AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID', '')
AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY', '')
AWS_SES_FROM_EMAIL = os.environ.get('AWS_SES_FROM_EMAIL', '')  # Verified sender email in SES
AWS_SES_TO_EMAIL = os.environ.get('AWS_SES_TO_EMAIL', '')  # Optional: Default recipient email

# OpenAI configuration (for generating ChatGPT embeddings inline)
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
OPENAI_EMBEDDING_MODEL = os.environ.get('OPENAI_EMBEDDING_MODEL', 'text-embedding-3-small')
# Max texts per OpenAI embeddings API call (hard limit is 2048; keep lower for safety)
OPENAI_EMBED_BATCH_SIZE = int(os.environ.get('OPENAI_EMBED_BATCH_SIZE', 200))

# Redis configuration (for job storage on Railway)
# Railway provides REDIS_URL automatically when Redis service is added
REDIS_URL = os.environ.get('REDIS_URL', '')
REDIS_JOB_TTL = int(os.environ.get('REDIS_JOB_TTL', 86400))  # Default: 24 hours
REDIS_JOB_TTL_COMPLETED = int(os.environ.get('REDIS_JOB_TTL_COMPLETED', 3600))  # Default: 1 hour for completed jobs
REDIS_JOB_TTL_FAILED = int(os.environ.get('REDIS_JOB_TTL_FAILED', 86400))  # Default: 24 hours for failed jobs

app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Initialize Redis client for job storage
redis_client = None
if REDIS_AVAILABLE and REDIS_URL:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()  # Test connection
        try:
            app.logger.info("Redis connected successfully for job storage")
        except:
            print("Redis connected successfully for job storage")
    except Exception as e:
        try:
            app.logger.error(f"Redis connection failed: {str(e)}")
            app.logger.warning("Falling back to in-memory job storage")
        except:
            print(f"Redis connection failed: {str(e)}")
            print("Falling back to in-memory job storage")
        redis_client = None
else:
    if not REDIS_AVAILABLE:
        try:
            app.logger.warning("Redis library not available. Install with: pip install redis")
        except:
            print("Redis library not available. Install with: pip install redis")
    if not REDIS_URL:
        try:
            app.logger.warning("REDIS_URL not set, using in-memory job storage (not recommended for production)")
        except:
            print("REDIS_URL not set, using in-memory job storage (not recommended for production)")

# Fallback to in-memory storage if Redis unavailable
jobs = {} if not redis_client else None


def _get_job(job_id):
    """
    Get job from Redis or in-memory fallback.
    
    Args:
        job_id: Job identifier
    
    Returns:
        Job dictionary or None if not found
    """
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
        return jobs.get(job_id) if jobs else None


def _set_job(job_id, job_data, ttl=None):
    """
    Set job in Redis or in-memory fallback.
    
    Args:
        job_id: Job identifier
        job_data: Job dictionary
        ttl: Time to live in seconds (None uses default based on status)
    """
    if redis_client:
        try:
            # Determine TTL based on job status if not provided
            if ttl is None:
                status = job_data.get('status', 'processing')
                if status == 'completed':
                    ttl = REDIS_JOB_TTL_COMPLETED
                elif status == 'failed':
                    ttl = REDIS_JOB_TTL_FAILED
                else:
                    ttl = REDIS_JOB_TTL
            
            redis_client.setex(
                f"job:{job_id}",
                ttl,
                json.dumps(job_data)
            )
            app.logger.debug(f"Job {job_id} stored in Redis with TTL {ttl}s (status: {job_data.get('status', 'unknown')})")
        except Exception as e:
            app.logger.error(f"Error storing job in Redis: {str(e)}")
    else:
        if jobs is not None:
            jobs[job_id] = job_data


def _delete_job(job_id):
    """
    Delete job from Redis or in-memory fallback.
    
    Args:
        job_id: Job identifier
    """
    if redis_client:
        try:
            redis_client.delete(f"job:{job_id}")
            app.logger.debug(f"Job {job_id} deleted from Redis")
        except Exception as e:
            app.logger.error(f"Error deleting job from Redis: {str(e)}")
    else:
        if jobs is not None:
            jobs.pop(job_id, None)


def allowed_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def validate_pdf_file(file_path):
    """
    Validate that the file is actually a PDF by checking magic bytes.
    This prevents malicious files from being uploaded even if they have .pdf extension.
    
    Args:
        file_path: Path to the file to validate
    
    Returns:
        Tuple of (is_valid: bool, error_message: str)
    """
    try:
        # Check file size
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            return False, "File is empty"
        
        if file_size < MIN_FILE_SIZE:
            return False, f"File is too small to be a valid PDF (minimum {MIN_FILE_SIZE} bytes)"
        
        if file_size > MAX_FILE_SIZE:
            return False, f"File size exceeds maximum allowed size of {MAX_FILE_SIZE / (1024*1024)}MB"
        
        # Read first 4 bytes to check PDF magic number
        # PDF files start with %PDF (hex: 25 50 44 46)
        with open(file_path, 'rb') as f:
            header = f.read(4)
            
        # Check for PDF magic bytes
        if header != b'%PDF':
            return False, "File is not a valid PDF (invalid magic bytes)"
        
        # Additional validation: Try to open with PyPDF2 to ensure it's a valid PDF
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(file_path)
            # Try to get page count to ensure PDF is readable
            num_pages = len(reader.pages)
            
            # Check for reasonable page count (prevent DoS)
            if num_pages > MAX_PDF_PAGES:
                return False, f"PDF has too many pages (max: {MAX_PDF_PAGES})"
            
        except Exception as e:
            return False, f"File appears to be corrupted or not a valid PDF: {str(e)}"
        
        return True, None
        
    except Exception as e:
        return False, f"Error validating file: {str(e)}"


def validate_pdf_structure(file_path):
    """
    Validate PDF structure to detect malformed or malicious PDFs.
    Checks for embedded JavaScript, embedded files, and other security risks.
    
    Args:
        file_path: Path to the PDF file
    
    Returns:
        Tuple of (is_valid: bool, error_message: str, warnings: list)
    """
    warnings = []
    try:
        from PyPDF2 import PdfReader
        
        reader = PdfReader(file_path)
        
        # Check for reasonable page count (prevent DoS)
        num_pages = len(reader.pages)
        if num_pages > MAX_PDF_PAGES:
            return False, f"PDF has too many pages (max: {MAX_PDF_PAGES})", warnings
        
        # Try to read first page to ensure it's not corrupted
        if num_pages > 0:
            try:
                first_page = reader.pages[0]
                _ = first_page.extract_text()  # Try to extract text
            except Exception as e:
                return False, f"PDF first page is corrupted: {str(e)}", warnings
        
        # Check for embedded JavaScript (potential security risk)
        try:
            root = reader.trailer.get('/Root', {})
            if isinstance(root, dict):
                if '/JavaScript' in root:
                    warnings.append("PDF contains JavaScript (potential security risk)")
                    app.logger.warning(f"PDF contains JavaScript: {file_path}")
                    # Optionally reject: return False, "PDF contains JavaScript (security risk)", warnings
                
                # Check for embedded files (potential malware)
                if '/EmbeddedFiles' in root:
                    warnings.append("PDF contains embedded files (potential security risk)")
                    app.logger.warning(f"PDF contains embedded files: {file_path}")
                    # Optionally reject: return False, "PDF contains embedded files (security risk)", warnings
        except Exception:
            # If we can't check, continue (fail open)
            pass
        
        return True, None, warnings
        
    except Exception as e:
        return False, f"PDF structure validation failed: {str(e)}", warnings


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
    if len(text) > MAX_CHUNK_LENGTH:
        text = text[:MAX_CHUNK_LENGTH]
        app.logger.warning(f"Text truncated to {MAX_CHUNK_LENGTH} characters")
    
    # Remove excessive whitespace (but preserve structure)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{10,}', '\n\n', text)  # Max 2 consecutive newlines
    
    return text.strip()


def detect_dangerous_content(text):
    """
    Detect potentially dangerous content patterns in extracted text.
    Uses specific patterns to avoid false positives from normal text.
    
    Args:
        text: Text to analyze
    
    Returns:
        Tuple of (is_dangerous: bool, reason: str)
    """
    if not text:
        return False, None
    
    dangerous_patterns = [
        # SQL injection patterns - only flag if followed by FROM/WHERE/etc (actual SQL)
        (r'(?i)(union|select|insert|delete|drop)\s+.*?\s+(from|where|into|table)', 'Potential SQL injection'),
        # Script injection - actual HTML script tags
        (r'(?i)<script[^>]*>.*?</script>', 'JavaScript code detected'),
        # Command injection - only flag actual code patterns, not common words
        # Look for patterns like: os.system(, subprocess.call(, eval(, exec(
        (r'(?i)(os\.system|subprocess\.(call|run|Popen)|eval\s*\(|exec\s*\(|__import__)', 'Potential command injection'),
        # Excessive encoded content (potential obfuscation) - fixed regex syntax
        (r'(?:%[0-9A-Fa-f]{2}){100,}', 'Excessive URL encoding (potential obfuscation)'),
    ]
    
    for pattern, reason in dangerous_patterns:
        try:
            if re.search(pattern, text):
                app.logger.warning(f"Dangerous content detected: {reason}")
                # Log but don't reject (fail open) - adjust based on your security policy
                # To reject: return True, reason
                # To flag only: return False, reason (current behavior)
        except re.error as e:
            # If regex pattern is invalid, log error but don't break processing
            app.logger.error(f"Invalid regex pattern in dangerous content detection: {pattern}, error: {str(e)}")
            continue
    
    return False, None


def convert_pdf_first_page_to_jpg(pdf_path, output_path=None):
    """
    Convert the first page of a PDF to a JPG image.
    
    Args:
        pdf_path: Path to the PDF file
        output_path: Optional path to save the JPG. If None, saves to temp directory.
    
    Returns:
        Path to the created JPG file, or None if conversion failed
    """
    if not PDF2IMAGE_AVAILABLE:
        try:
            app.logger.warning("pdf2image not available, skipping PDF to JPG conversion. Install with: pip install pdf2image")
        except:
            print("pdf2image not available, skipping PDF to JPG conversion. Install with: pip install pdf2image")
        return None
    
    if not os.path.exists(pdf_path):
        app.logger.error(f"PDF file does not exist: {pdf_path}")
        return None
    
    try:
        # Generate output path if not provided
        if output_path is None:
            base_name = os.path.splitext(os.path.basename(pdf_path))[0]
            output_dir = os.path.dirname(pdf_path)
            output_path = os.path.join(output_dir, f"{base_name}_preview.jpg")
        
        app.logger.info(f"Converting first page of PDF to JPG: {pdf_path}")
        
        # Convert first page only (pages parameter is 1-indexed, so [0] is first page)
        images = convert_from_path(pdf_path, first_page=1, last_page=1, dpi=150)
        
        if not images:
            app.logger.error("No images generated from PDF")
            return None
        
        # Save the first (and only) image as JPG
        images[0].save(output_path, 'JPEG', quality=85)
        app.logger.info(f"Successfully converted PDF first page to JPG: {output_path}")
        
        return output_path
        
    except Exception as e:
        app.logger.error(f"Error converting PDF to JPG: {str(e)}")
        import traceback
        app.logger.error(traceback.format_exc())
        return None


def upload_to_spaces(file_path, filename, pdf_embedding_id, content_type='application/pdf'):
    """
    Upload file to DigitalOcean Spaces (S3-compatible storage).
    
    Args:
        file_path: Local path to the file
        filename: Original filename (or desired filename)
        pdf_embedding_id: UUID of the pdf_embeddings record
        content_type: MIME type of the file (default: 'application/pdf')
    
    Returns:
        Public URL of the uploaded file, or None if upload failed
    """
    if not all([DO_SPACES_URL, DO_SPACES_ID, DO_SPACES_SECRET, DO_SPACES_BUCKET]):
        app.logger.warning("DigitalOcean Spaces configuration missing, skipping file upload")
        return None
    
    try:
        # Verify file exists before attempting upload
        if not os.path.exists(file_path):
            app.logger.error(f"File does not exist at path: {file_path}")
            return None
        
        file_size = os.path.getsize(file_path)
        app.logger.info(f"File exists, size: {file_size} bytes")
        
        # Parse DO_SPACES_URL - handle both full URL and endpoint formats
        # e.g., "https://nyc3.digitaloceanspaces.com" or "nyc3.digitaloceanspaces.com"
        endpoint_url = DO_SPACES_URL.strip()
        if not endpoint_url.startswith('http'):
            endpoint_url = f'https://{endpoint_url}'
        
        app.logger.info(f"Spaces endpoint: {endpoint_url}, bucket: {DO_SPACES_BUCKET}")
        
        # Extract region from endpoint if possible (for boto3)
        # Default to 'fra1' if can't determine
        region = 'fra1'  # Default
        if 'nyc3' in endpoint_url.lower():
            region = 'nyc3'
        elif 'sgp1' in endpoint_url.lower():
            region = 'sgp1'
        elif 'ams3' in endpoint_url.lower():
            region = 'ams3'
        elif 'sfo3' in endpoint_url.lower():
            region = 'sfo3'
        elif 'fra1' in endpoint_url.lower():
            region = 'fra1'
        
        app.logger.info(f"Using region: {region}")
        
        # Create S3 client for DigitalOcean Spaces
        # Note: Strip credentials to remove any whitespace
        s3_client = boto3.client(
            's3',
            endpoint_url=endpoint_url,
            aws_access_key_id=DO_SPACES_ID.strip() if DO_SPACES_ID else None,
            aws_secret_access_key=DO_SPACES_SECRET.strip() if DO_SPACES_SECRET else None,
            region_name=region
        )
        
        # Create secure filename with embedding ID
        # Format: docs_pdf_embedding_sources/{pdf_embedding_id}/{secure_filename}
        # Note: Folders are created automatically in S3/Spaces - no need to create them manually
        secure_name = secure_filename(filename)
        s3_key = f"{DO_SPACES_FOLDER}/{pdf_embedding_id}/{secure_name}"
        
        app.logger.info(f"Uploading file to Spaces: bucket={DO_SPACES_BUCKET}, key={s3_key}")
        
        # Upload file with private ACL (change to 'public-read' if you want public access)
        s3_client.upload_file(
            file_path,
            DO_SPACES_BUCKET,
            s3_key,
            ExtraArgs={
                'ContentType': content_type,
                'ACL': 'public-read'  # Change to 'public-read' if you want public access
            }
        )
        
        # Construct public URL
        # Format: https://{bucket}.{endpoint}/{key}
        # Extract endpoint hostname from URL
        from urllib.parse import urlparse
        parsed_url = urlparse(endpoint_url)
        endpoint_host = parsed_url.netloc or parsed_url.path.replace('https://', '').replace('http://', '')
        
        file_url = f"https://{DO_SPACES_BUCKET}.{endpoint_host}/{s3_key}"
        
        app.logger.info(f"Successfully uploaded file to Spaces: {file_url}")
        return file_url
        
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_message = e.response.get('Error', {}).get('Message', str(e))
        app.logger.error(f"DigitalOcean Spaces upload error ({error_code}): {error_message}")
        
        if error_code == 'NoSuchBucket':
            app.logger.error(f"Bucket '{DO_SPACES_BUCKET}' does not exist. Please create it in DigitalOcean Spaces.")
        elif error_code == 'AccessDenied':
            app.logger.error("Access denied. Please verify DO_SPACES_ID and DO_SPACES_SECRET are correct.")
        elif error_code == 'InvalidAccessKeyId':
            app.logger.error("Invalid access key ID. Please verify DO_SPACES_ID is correct.")
        elif error_code == 'SignatureDoesNotMatch':
            app.logger.error("Signature mismatch. Please verify DO_SPACES_SECRET is correct.")
        
        return None
    except Exception as e:
        app.logger.error(f"Unexpected error uploading to Spaces: {str(e)}")
        import traceback
        app.logger.error(traceback.format_exc())
        return None


def _normalize_text(text):
    """
    Normalize text by removing inconsistent formatting while preserving important structure.
    Aggressively removes PDF layout whitespace while maintaining readability.
    
    Args:
        text: Raw text string
    
    Returns:
        Normalized text string
    """
    if not text:
        return ""
    
    # First pass: Remove lines that are only whitespace or nearly empty
    lines = text.split('\n')
    content_lines = []
    for line in lines:
        stripped = line.strip()
        # Only keep lines with actual content (more than just a few chars of whitespace/symbols)
        if len(stripped) > 0:
            content_lines.append(stripped)
    
    # Rejoin with single newlines
    text = '\n'.join(content_lines)
    
    # Normalize line breaks - replace multiple newlines with double newline (paragraph break)
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    # Remove hyphenation artifacts (hyphen at end of line followed by word on next line)
    # Pattern: word-\nword becomes wordword
    text = re.sub(r'(\w+)-\s*\n\s*(\w+)', r'\1\2', text)
    
    # Normalize bullet points - convert various bullet styles to consistent •
    # Match common bullet patterns: -, *, •, o, ▶, ►, etc.
    text = re.sub(r'^\s*[-*•o▶►]\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+[.)]\s+', r'\g<0>', text)  # Keep numbered lists as-is
    
    # Normalize whitespace - multiple spaces to single space
    # This is critical for removing PDF layout spacing
    text = re.sub(r'  +', ' ', text)  # 2 or more spaces become 1
    text = re.sub(r'[ \t]+', ' ', text)  # Any tabs/spaces combo become single space
    
    # Remove any remaining excessive newlines
    text = re.sub(r'\n\n+', '\n\n', text)
    
    return text.strip()


def _infer_title_from_first_page(text_by_page):
    """
    Infer document title from the first page if metadata title is empty.
    Looks for prominent text patterns typically used for titles.
    
    Args:
        text_by_page: Dictionary of page text data
    
    Returns:
        Inferred title string or empty string if cannot infer
    """
    if not text_by_page:
        return ""
    
    # Get first page text
    first_page_key = 'page_1'
    if first_page_key not in text_by_page:
        # Try to find first page by sorting
        sorted_pages = sorted(
            text_by_page.items(),
            key=lambda x: x[1].get('pdf_page', x[1].get('page_number', 999)) if isinstance(x[1], dict) else 999
        )
        if not sorted_pages:
            return ""
        first_page_key, first_page_data = sorted_pages[0]
    else:
        first_page_data = text_by_page[first_page_key]
    
    if not isinstance(first_page_data, dict) or 'text' not in first_page_data:
        return ""
    
    first_page_text = first_page_data.get('text', '').strip()
    if not first_page_text:
        return ""
    
    # Split into lines and find potential title
    lines = [line.strip() for line in first_page_text.split('\n') if line.strip()]
    
    if not lines:
        return ""
    
    # Strategy 1: Look for short lines at the beginning (typically titles are short and prominent)
    # Title is usually within first 10 lines and under 100 characters
    potential_titles = []
    for i, line in enumerate(lines[:10]):
        # Skip very short lines (likely page numbers or artifacts)
        if len(line) < 5:
            continue
        # Skip lines with dates, numbers only, or common headers
        if re.match(r'^\d+$', line) or re.match(r'^page\s+\d+', line, re.IGNORECASE):
            continue
        # Titles are typically 5-100 characters
        if 5 <= len(line) <= 100:
            potential_titles.append(line)
        # If we find a longer descriptive line, it might be a subtitle
        elif 100 < len(line) <= 200 and i < 5:
            potential_titles.append(line)
    
    if not potential_titles:
        # Fallback: Use first non-trivial line
        for line in lines[:5]:
            if len(line) > 10:
                return line
        return ""
    
    # Strategy 2: Combine multiple short lines that form the title (common in PDFs)
    # Example: "Off target" + "Continued collective inaction" + "Emissions Gap Report 2025"
    if len(potential_titles) >= 2:
        # Check if first few lines together form a coherent title
        combined = ' '.join(potential_titles[:3])  # Take up to 3 lines
        # If combined title is reasonable length, use it
        if len(combined) <= 200:
            return combined
    
    # Strategy 3: Return first potential title
    return potential_titles[0] if potential_titles else ""


def _split_into_semantic_units(text):
    """
    Split text into semantic units (paragraphs, bullet points, sentences) while preserving context.
    Each unit should contain a complete idea with sufficient surrounding context.
    
    Args:
        text: Normalized text string
    
    Returns:
        List of semantic units (strings)
    """
    if not text:
        return []
    
    units = []
    
    # First, split by paragraph breaks (double or triple newlines)
    # Use triple newlines for major section breaks
    paragraphs = re.split(r'\n{2,}', text)
    
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        
        # Detect and preserve section headers (short lines, often in CAPS or Title Case)
        # Headers are important context markers
        is_header = (
            len(para) < 100 and  # Short line
            (para.isupper() or  # ALL CAPS
             para.istitle() or  # Title Case
             re.match(r'^(Chapter|Section|Box|Figure|Table)\s+\d+', para, re.IGNORECASE))  # Numbered sections
        )
        
        if is_header:
            # Keep headers as separate units for context
            units.append(para)
            continue
        
        # Check if paragraph is a bullet list
        bullet_pattern = r'^•\s+'
        if re.match(bullet_pattern, para, re.MULTILINE):
            # Keep bullet lists together if they're related (part of same list)
            # Only split if total length is very long
            if len(para) > 1500:
                # Split bullet list into individual bullets
                bullets = re.split(r'\n(?=•\s+)', para)
                for bullet in bullets:
                    bullet = bullet.strip()
                    if bullet:
                        units.append(bullet)
            else:
                # Keep short bullet lists together for context
                units.append(para)
        else:
            # Check if it's a numbered list
            if re.match(r'^\d+[.)]\s+', para, re.MULTILINE):
                # Keep numbered lists together if reasonable length
                if len(para) > 1500:
                    # Split numbered list items
                    items = re.split(r'\n(?=\d+[.)]\s+)', para)
                    for item in items:
                        item = item.strip()
                        if item:
                            units.append(item)
                else:
                    # Keep related numbered items together
                    units.append(para)
            else:
                # Regular paragraph - check if it's too long
                # If paragraph is very long, try to split by sentences
                # Increased threshold from 800 to 1200 to preserve more context
                if len(para) > 1200:
                    # Split by sentence boundaries
                    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', para)
                    current_unit = ""
                    for sentence in sentences:
                        sentence = sentence.strip()
                        if not sentence:
                            continue
                        
                        # If adding this sentence would exceed reasonable size, save current
                        # Increased from 600 to 900 to keep more context together
                        if current_unit and len(current_unit) + len(sentence) + 1 > 900:
                            units.append(current_unit.strip())
                            current_unit = sentence
                        else:
                            if current_unit:
                                current_unit += " " + sentence
                            else:
                                current_unit = sentence
                    
                    if current_unit.strip():
                        units.append(current_unit.strip())
                else:
                    # Paragraph is reasonable size, keep as single unit
                    units.append(para)
    
    return units


def _chunk_text_for_embeddings(text_by_page, chunk_size=1500, overlap=400):
    """
    Chunk text intelligently for embeddings with improved semantic splitting.
    For large PDFs (800+ pages), we need to split text into manageable chunks.
    Each chunk should ideally contain a single idea with sufficient context.
    
    Args:
        text_by_page: Dictionary of page text data
        chunk_size: Target characters per chunk (default 1500, optimized for technical documents)
        overlap: Characters to overlap between chunks for context (default 400, ensures continuity)
    
    Returns:
        List of chunk dictionaries with text, page info, and metadata
    """
    chunks = []
    
    # Sort pages by pdf_page (new field) or fall back to legacy page_number
    sorted_pages = sorted(
        text_by_page.items(),
        key=lambda x: x[1].get('pdf_page', x[1].get('page_number', 0)) if isinstance(x[1], dict) else 0
    )

    # First pass: normalize all text and split into semantic units
    all_units = []
    unit_to_page = {}       # Map unit index → pdf_page (int)
    unit_to_printed = {}    # Map unit index → printed_page (str, e.g. "7-5")
    unit_to_chapter = {}    # Map unit index → chapter name

    for page_key, page_data in sorted_pages:
        if not isinstance(page_data, dict) or 'text' not in page_data:
            continue

        page_text = page_data.get('text', '')
        # Support both new field name (pdf_page) and legacy (page_number)
        page_num = page_data.get('pdf_page', page_data.get('page_number', 0))
        printed_page = page_data.get('printed_page')
        chapter = page_data.get('chapter')

        if not page_text.strip():
            continue

        # Normalize the page text
        normalized_text = _normalize_text(page_text)

        # Sanitize text for embeddings (remove dangerous content)
        sanitized_text = sanitize_text_for_embeddings(normalized_text)

        # Detect dangerous content (log warnings only, don't block processing)
        try:
            is_dangerous, reason = detect_dangerous_content(sanitized_text)
            if is_dangerous:
                app.logger.warning(f"Dangerous content detected in page {page_num}: {reason}")
        except Exception as e:
            app.logger.warning(f"Error in dangerous content detection for page {page_num}: {str(e)}")
        
        # Split into semantic units
        units = _split_into_semantic_units(sanitized_text)

        # Track which page each unit belongs to
        for unit in units:
            unit_index = len(all_units)
            all_units.append(unit)
            unit_to_page[unit_index] = page_num
            unit_to_printed[unit_index] = printed_page
            unit_to_chapter[unit_index] = chapter

    # Second pass: combine units into chunks
    current_chunk = ""
    current_pages = set()
    current_printed_pages = set()
    current_chapters = set()
    chunk_index = 0

    for i, unit in enumerate(all_units):
        unit_page = unit_to_page.get(i, 0)
        unit_printed = unit_to_printed.get(i)
        unit_chapter = unit_to_chapter.get(i)

        # Check if adding this unit would exceed chunk size
        unit_with_separator = "\n\n" + unit if current_chunk else unit
        potential_chunk_size = len(current_chunk) + len(unit_with_separator)

        if current_chunk and potential_chunk_size > chunk_size:
            # Save current chunk
            if current_chunk.strip():
                chunks.append({
                    'chunk_index': chunk_index,
                    'text': current_chunk.strip(),
                    'pages': sorted(list(current_pages)),
                    'printed_pages': sorted([p for p in current_printed_pages if p], key=lambda x: str(x)),
                    'chapters': sorted([c for c in current_chapters if c]),
                    'char_count': len(current_chunk),
                    'start_page': min(current_pages) if current_pages else 0,
                    'end_page': max(current_pages) if current_pages else 0,
                })
                chunk_index += 1

            # Start new chunk with overlap from previous
            if overlap > 0 and current_chunk:
                overlap_text = current_chunk[-overlap:].strip()

                # Try to find a good boundary for overlap (sentence or paragraph)
                sentence_match = None
                for match in re.finditer(r'(?<=[.!?])\s+', overlap_text):
                    sentence_match = match

                if sentence_match:
                    overlap_text = overlap_text[sentence_match.end():]
                else:
                    para_match = None
                    for match in re.finditer(r'\n\n', overlap_text):
                        para_match = match

                    if para_match:
                        overlap_text = overlap_text[para_match.end():]
                    else:
                        overlap_text = overlap_text[int(len(overlap_text) * 0.4):]

                if overlap_text:
                    current_chunk = overlap_text + "\n\n" + unit
                else:
                    current_chunk = unit

                current_pages = {max(current_pages)} if current_pages else {unit_page}
                current_pages.add(unit_page)
                current_printed_pages = {unit_printed} if unit_printed else set()
                current_chapters = {unit_chapter} if unit_chapter else set()
            else:
                current_chunk = unit
                current_pages = {unit_page}
                current_printed_pages = {unit_printed} if unit_printed else set()
                current_chapters = {unit_chapter} if unit_chapter else set()
        else:
            # Add unit to current chunk
            if current_chunk:
                current_chunk += "\n\n" + unit
            else:
                current_chunk = unit
            current_pages.add(unit_page)
            if unit_printed:
                current_printed_pages.add(unit_printed)
            if unit_chapter:
                current_chapters.add(unit_chapter)

    # Add final chunk
    if current_chunk.strip():
        chunks.append({
            'chunk_index': chunk_index,
            'text': current_chunk.strip(),
            'pages': sorted(list(current_pages)),
            'printed_pages': sorted([p for p in current_printed_pages if p], key=lambda x: str(x)),
            'chapters': sorted([c for c in current_chapters if c]),
            'char_count': len(current_chunk),
            'start_page': min(current_pages) if current_pages else 0,
            'end_page': max(current_pages) if current_pages else 0,
        })
    
    # Limit total chunks per PDF (prevent resource exhaustion)
    if len(chunks) > MAX_CHUNKS_PER_PDF:
        app.logger.warning(f"PDF has {len(chunks)} chunks, limiting to {MAX_CHUNKS_PER_PDF}")
        chunks = chunks[:MAX_CHUNKS_PER_PDF]
    
    return chunks


def _gql(graphql_url, headers, query, variables, timeout=30):
    """
    Execute a single GraphQL operation. Returns the parsed JSON body or raises on HTTP error.
    GraphQL-level errors are logged and returned as-is so callers can inspect them.
    """
    response = requests.post(
        graphql_url,
        json={"query": query, "variables": variables},
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def _generate_openai_embeddings(texts):
    """
    Call the OpenAI embeddings API for a list of texts.
    Batches automatically to respect OPENAI_EMBED_BATCH_SIZE.

    Returns a list of float lists (one per input text), or None on failure.
    The returned list is always the same length as `texts`; failed batches
    produce None entries so callers can still store the chunks without vectors.
    """
    if not OPENAI_AVAILABLE:
        app.logger.warning("openai package not installed – skipping embeddings")
        return None
    if not OPENAI_API_KEY:
        app.logger.warning("OPENAI_API_KEY not set – skipping embeddings")
        return None

    client = _OpenAIClient(api_key=OPENAI_API_KEY)
    results = [None] * len(texts)

    for batch_start in range(0, len(texts), OPENAI_EMBED_BATCH_SIZE):
        batch = texts[batch_start: batch_start + OPENAI_EMBED_BATCH_SIZE]
        try:
            resp = client.embeddings.create(model=OPENAI_EMBEDDING_MODEL, input=batch)
            for item in resp.data:
                results[batch_start + item.index] = item.embedding
            app.logger.debug(
                f"Embedded batch {batch_start}–{batch_start + len(batch) - 1} "
                f"({len(batch)} texts, model={OPENAI_EMBEDDING_MODEL})"
            )
        except Exception as exc:
            app.logger.error(f"OpenAI embeddings batch {batch_start} failed: {exc}")

    return results


def _send_to_db(data, job_id, filename, user_id=None, file_url=None,
                upload_device="web", file_path=None, user_display_name=None,
                progress_cb=None):
    """
    Persist a processed PDF to the new tt_ai schema (tt_ai.documents + tt_ai.chunks)
    and generate ChatGPT embeddings for every chunk inline.

    Pipeline (all in the background thread):
      1. Upload PDF to DigitalOcean Spaces → get CDN source URL
      2. Convert first page → JPG → upload → get preview_url
      3. INSERT tt_ai.documents  (status = 'processing')
      4. Bulk INSERT tt_ai.chunks (no embeddings yet)
      5. Generate OpenAI embeddings in batches
      6. Bulk UPDATE tt_ai.chunks with embedding vectors
      7. UPDATE tt_ai.documents  (status = 'embedded')
      8. Send email notification

    Args:
        data:               Extracted PDF data dict (keys: metadata, text, tables)
        job_id:             Unique job identifier
        filename:           Original filename
        user_id:            Optional UUID string of the uploading user
        file_url:           Pre-existing URL (ignored if file_path is provided)
        upload_device:      Device/platform label (default: 'web')
        file_path:          Local temp path of the PDF – used for Spaces upload
        user_display_name:  For email notification
        progress_cb:        Optional callable(stage: str, pct: int, msg: str)

    Returns:
        dict with document_id and chunk_count on success, None on failure.
    """
    if not NHOST_BACKEND_URL or not NHOST_ADMIN_SECRET:
        app.logger.warning("Nhost not configured – skipping DB storage")
        return None

    graphql_url = NHOST_GRAPHQL_URL or f"{NHOST_BACKEND_URL}/v1/graphql"
    gql_headers = {
        "Content-Type": "application/json",
        "x-hasura-admin-secret": NHOST_ADMIN_SECRET,
    }

    def _progress(stage, pct, msg):
        if progress_cb:
            progress_cb(stage, pct, msg)
        app.logger.info(f"[{job_id}] {stage} ({pct}%) – {msg}")

    try:
        # ------------------------------------------------------------------
        # 1 & 2. Spaces upload (PDF + JPG preview)
        # ------------------------------------------------------------------
        source_url = file_url  # fallback if no local file
        preview_url = None

        if file_path and os.path.exists(file_path):
            _progress("spaces_upload", 5, "Uploading PDF to Spaces…")
            source_url = upload_to_spaces(
                file_path, filename, job_id, content_type="application/pdf"
            )
            if source_url:
                app.logger.info(f"PDF uploaded to Spaces: {source_url}")
                jpg_path = convert_pdf_first_page_to_jpg(file_path)
                if jpg_path and os.path.exists(jpg_path):
                    jpg_filename = os.path.splitext(secure_filename(filename))[0] + "_preview.jpg"
                    preview_url = upload_to_spaces(
                        jpg_path, jpg_filename, job_id, content_type="image/jpeg"
                    )
                    try:
                        os.remove(jpg_path)
                    except Exception:
                        pass
                    if preview_url:
                        app.logger.info(f"Preview JPG uploaded: {preview_url}")
            else:
                app.logger.warning("Spaces upload failed – document will have no source URL")

        # ------------------------------------------------------------------
        # 3. Build chunks
        # ------------------------------------------------------------------
        text_by_page = data.get("text", {})
        chunks = _chunk_text_for_embeddings(text_by_page, chunk_size=1500, overlap=400)
        app.logger.info(f"Created {len(chunks)} chunks for job {job_id}")

        # Resolve title
        metadata = data.get("metadata", {}).copy()
        title = metadata.get("title", "").strip()
        if not title:
            title = _infer_title_from_first_page(text_by_page) or filename
        num_pages = metadata.get("num_pages") or metadata.get("page_count")

        # ------------------------------------------------------------------
        # 4. INSERT tt_ai.documents
        # ------------------------------------------------------------------
        _progress("insert_document", 15, "Inserting document record…")

        doc_object = {
            "job_id": job_id,
            "title": title,
            "filename": filename,
            "source": source_url,
            "preview_url": preview_url,
            "num_pages": num_pages,
            "metadata": metadata,
            "status": "processing",
            "upload_device": upload_device,
        }
        if user_id:
            doc_object["userID"] = user_id

        insert_doc_mutation = """
            mutation InsertDocument($obj: tt_ai_documents_insert_input!) {
                insert_tt_ai_documents_one(object: $obj) {
                    id
                }
            }
        """
        doc_result = _gql(graphql_url, gql_headers, insert_doc_mutation,
                          {"obj": doc_object}, timeout=30)

        if "errors" in doc_result:
            app.logger.error(f"GraphQL error inserting document: {doc_result['errors']}")
            return None

        document_id = doc_result["data"]["insert_tt_ai_documents_one"]["id"]
        app.logger.info(f"Inserted document {document_id} for job {job_id}")

        # ------------------------------------------------------------------
        # 5. Bulk INSERT tt_ai.chunks (no embeddings yet)
        # ------------------------------------------------------------------
        _progress("insert_chunks", 25, f"Inserting {len(chunks)} chunks…")

        chunk_objects = []
        for ch in chunks:
            pages_list = ch.get("pages", [])
            printed_pages = ch.get("printed_pages", [])
            chapters_list = ch.get("chapters", [])
            chunk_objects.append({
                "document_id": document_id,
                "chunk_index": ch["chunk_index"],
                "content": ch["text"],
                "page": pages_list[0] if pages_list else None,
                "printed_page": printed_pages[0] if printed_pages else None,
                "chapter": chapters_list[0] if chapters_list else None,
                "char_count": ch.get("char_count", len(ch["text"])),
            })

        insert_chunks_mutation = """
            mutation InsertChunks($objects: [tt_ai_chunks_insert_input!]!) {
                insert_tt_ai_chunks(objects: $objects) {
                    returning { id chunk_index }
                }
            }
        """
        # For large PDFs, raise timeout proportionally
        chunks_timeout = max(60, len(chunk_objects) // 10)
        chunks_result = _gql(graphql_url, gql_headers, insert_chunks_mutation,
                             {"objects": chunk_objects}, timeout=chunks_timeout)

        if "errors" in chunks_result:
            app.logger.error(f"GraphQL error inserting chunks: {chunks_result['errors']}")
            # Mark document failed and bail
            _gql(graphql_url, gql_headers,
                 "mutation U($id:uuid!){update_tt_ai_documents_by_pk(pk_columns:{id:$id},_set:{status:\"failed\"}){id}}",
                 {"id": document_id})
            return None

        inserted_chunks = chunks_result["data"]["insert_tt_ai_chunks"]["returning"]
        # Build index → DB id map
        chunk_id_map = {row["chunk_index"]: row["id"] for row in inserted_chunks}
        app.logger.info(f"Inserted {len(inserted_chunks)} chunks for document {document_id}")

        # ------------------------------------------------------------------
        # 6. Generate OpenAI embeddings
        # ------------------------------------------------------------------
        _progress("embeddings", 40, "Generating ChatGPT embeddings…")

        texts_for_embed = [ch["text"] for ch in chunks]
        embeddings = _generate_openai_embeddings(texts_for_embed)

        # ------------------------------------------------------------------
        # 7. Update chunks with embedding vectors (one mutation per chunk)
        #    We use a single bulk mutation via aliases to keep round-trips low.
        # ------------------------------------------------------------------
        if embeddings and any(e is not None for e in embeddings):
            _progress("store_embeddings", 60, "Storing embeddings in DB…")

            update_mutation_parts = []
            update_vars = {"model": OPENAI_EMBEDDING_MODEL}

            for idx, (ch, emb) in enumerate(zip(chunks, embeddings)):
                if emb is None:
                    continue
                chunk_db_id = chunk_id_map.get(ch["chunk_index"])
                if chunk_db_id is None:
                    continue
                alias = f"u{idx}"
                update_mutation_parts.append(
                    f'{alias}: update_tt_ai_chunks_by_pk('
                    f'  pk_columns: {{id: $id_{idx}}},'
                    f'  _set: {{embedding_chatgpt: $emb_{idx}, chatgpt_model_name: $model}}'
                    f') {{ id }}'
                )
                update_vars[f"id_{idx}"] = chunk_db_id
                update_vars[f"emb_{idx}"] = emb

            if update_mutation_parts:
                # Build dynamic variable declarations
                var_decls = ["$model: String!"]
                for idx, (ch, emb) in enumerate(zip(chunks, embeddings)):
                    if emb is None:
                        continue
                    if chunk_id_map.get(ch["chunk_index"]) is None:
                        continue
                    var_decls.append(f"$id_{idx}: uuid!")
                    var_decls.append(f"$emb_{idx}: vector!")

                bulk_update_mutation = (
                    "mutation UpdateEmbeddings(" + ", ".join(var_decls) + ") {\n"
                    + "\n".join(update_mutation_parts)
                    + "\n}"
                )
                embed_timeout = max(60, len(update_mutation_parts) // 5)
                emb_result = _gql(graphql_url, gql_headers, bulk_update_mutation,
                                  update_vars, timeout=embed_timeout)
                if "errors" in emb_result:
                    app.logger.warning(f"Partial embedding update errors: {emb_result['errors']}")
                else:
                    app.logger.info(
                        f"Stored embeddings for {len(update_mutation_parts)} chunks "
                        f"(model={OPENAI_EMBEDDING_MODEL})"
                    )
        else:
            app.logger.warning("No embeddings generated – chunks stored without vectors")

        # ------------------------------------------------------------------
        # 8. Mark document as embedded
        # ------------------------------------------------------------------
        _progress("finalise", 90, "Finalising document record…")
        _gql(
            graphql_url, gql_headers,
            """mutation MarkEmbedded($id: uuid!, $source: String, $preview: String) {
                update_tt_ai_documents_by_pk(
                    pk_columns: {id: $id},
                    _set: {status: "embedded", source: $source, preview_url: $preview}
                ) { id status }
            }""",
            {"id": document_id, "source": source_url, "preview": preview_url},
            timeout=30,
        )

        # ------------------------------------------------------------------
        # 9. Email notification
        # ------------------------------------------------------------------
        if user_id:
            _send_email_notification(filename, user_id, user_display_name, document_id)

        return {"document_id": document_id, "chunk_count": len(inserted_chunks)}

    except Exception as exc:
        app.logger.error(f"_send_to_db failed for job {job_id}: {exc}", exc_info=True)
        return None


def _send_email_notification(filename, user_id, user_display_name=None, pdf_embedding_id=None):
    """
    Send email notification using AWS SES after successful PDF embedding creation.
    
    Args:
        filename: Name of the PDF file
        user_id: UUID of the user
        user_display_name: Optional display name of the user
        pdf_embedding_id: Optional UUID of the pdf_embeddings record
    """
    # Check if AWS SES is configured
    if not all([AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SES_FROM_EMAIL]):
        app.logger.debug("AWS SES configuration missing, skipping email notification")
        return
    
    # Check if recipient email is configured
    if not AWS_SES_TO_EMAIL:
        app.logger.debug("AWS_SES_TO_EMAIL not set, skipping email notification")
        return
    
    try:
        # Create SES client with explicit credentials
        # Note: Ensure AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are correct
        # and that the region matches where your SES is configured
        ses_client = boto3.client(
            'ses',
            region_name=AWS_SES_REGION,
            aws_access_key_id=AWS_ACCESS_KEY_ID.strip() if AWS_ACCESS_KEY_ID else None,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY.strip() if AWS_SECRET_ACCESS_KEY else None
        )
        
        # Prepare email content
        subject = f"PDF Embedding Created: {filename}"
        
        # Build email body
        body_text = f"""
PDF Embedding Successfully Created

File Name: {filename}
User ID: {user_id}
User Display Name: {user_display_name or 'Not provided'}
PDF Embedding ID: {pdf_embedding_id or 'N/A'}

The PDF has been processed and embeddings have been created successfully.
"""
        
        body_html = f"""
<html>
<head></head>
<body>
  <h2>PDF Embedding Successfully Created</h2>
  <p><strong>File Name:</strong> {filename}</p>
  <p><strong>User ID:</strong> {user_id}</p>
  <p><strong>User Display Name:</strong> {user_display_name or 'Not provided'}</p>
  <p><strong>PDF Embedding ID:</strong> {pdf_embedding_id or 'N/A'}</p>
  <p>The PDF has been processed and embeddings have been created successfully.</p>
</body>
</html>
"""
        
        # Use configured recipient email
        to_email = AWS_SES_TO_EMAIL.strip()
        
        # Send email
        response = ses_client.send_email(
            Source=AWS_SES_FROM_EMAIL,
            Destination={
                'ToAddresses': [to_email]
            },
            Message={
                'Subject': {
                    'Data': subject,
                    'Charset': 'UTF-8'
                },
                'Body': {
                    'Text': {
                        'Data': body_text,
                        'Charset': 'UTF-8'
                    },
                    'Html': {
                        'Data': body_html,
                        'Charset': 'UTF-8'
                    }
                }
            }
        )
        
        app.logger.info(f"Email notification sent successfully. MessageId: {response.get('MessageId')}")
        
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_message = e.response.get('Error', {}).get('Message', str(e))
        
        if error_code == 'SignatureDoesNotMatch':
            app.logger.error(f"AWS SES authentication error: {error_message}")
            app.logger.error("Please verify your AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are correct")
            app.logger.error(f"Region: {AWS_SES_REGION}, From: {AWS_SES_FROM_EMAIL}")
        elif error_code == 'MessageRejected':
            app.logger.error(f"AWS SES message rejected: {error_message}")
            app.logger.error("Please verify that AWS_SES_FROM_EMAIL is verified in SES")
        else:
            app.logger.error(f"AWS SES error ({error_code}): {error_message}")
        # Don't raise - email failure shouldn't break the main process
    except Exception as e:
        app.logger.error(f"Unexpected error sending email: {str(e)}")
        # Don't raise - email failure shouldn't break the main process


def _send_webhook(job_id, status, data=None, error=None):
    """
    Send webhook notification to Next.js app.
    
    Args:
        job_id: Job identifier
        status: Job status ('completed', 'failed')
        data: Extracted data (if successful)
        error: Error message (if failed)
    """
    if not WEBHOOK_URL:
        return
    
    try:
        payload = {
            'job_id': job_id,
            'status': status,
            'timestamp': str(uuid.uuid4())  # Simple timestamp placeholder
        }
        
        if data:
            payload['data'] = data
        if error:
            payload['error'] = error
        
        response = requests.post(
            WEBHOOK_URL,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=10
        )
        
        if response.status_code == 200:
            app.logger.info(f"Webhook sent successfully for job {job_id}")
        else:
            app.logger.warning(f"Webhook failed: {response.status_code}")
            
    except Exception as e:
        app.logger.error(f"Error sending webhook: {str(e)}")


def _process_extraction_async(file_path, original_filename, job_id, extract_type='all', pages=None,
                              include_tables=True, send_to_nhost=True,
                              send_webhook=True, user_id=None, file_url=None, upload_device="web", user_display_name=None):
    """
    Process PDF extraction asynchronously.
    Optimized for large PDFs (800+ pages) with progress updates.
    Holds a concurrency slot for the duration of processing and releases it on completion.

    Args:
        file_path: Path to saved file
        original_filename: Original filename
        job_id: Unique job identifier
        extract_type: Type of extraction
        pages: List of page numbers
        include_tables: Whether to include tables (can be slow for large PDFs)
        send_to_nhost: Whether to send to Nhost
        send_webhook: Whether to send webhook
        user_id: Optional user ID from Next.js
        file_url: Optional URL if file is stored in S3/storage
        upload_device: Device/platform used for upload (default: 'web')
    """
    global _active_job_count
    _set_job(job_id, {
        'status': 'processing',
        'progress': 0,
        'stage': 'file_received',
        'message': 'File received, starting extraction...'
    })

    try:
        # Update: Reading file
        _set_job(job_id, {
            'status': 'processing',
            'progress': 5,
            'stage': 'reading',
            'message': 'Reading PDF file...'
        })

        # Verify file still exists before processing
        if not os.path.exists(file_path):
            error_msg = f"File does not exist at path: {file_path}"
            app.logger.error(error_msg)
            _set_job(job_id, {'status': 'failed', 'error': error_msg, 'stage': 'failed'}, ttl=REDIS_JOB_TTL_FAILED)
            if send_webhook:
                _send_webhook(job_id, 'failed', error=error_msg)
            return

        app.logger.info(f"Processing PDF from path: {file_path} (exists: {os.path.exists(file_path)}, size: {os.path.getsize(file_path)} bytes)")

        # Validate PDF structure (security check)
        is_valid, error_msg, warnings = validate_pdf_structure(file_path)
        if not is_valid:
            app.logger.error(f"PDF structure validation failed: {error_msg}")
            _set_job(job_id, {'status': 'failed', 'error': f"PDF validation failed: {error_msg}", 'stage': 'failed'}, ttl=REDIS_JOB_TTL_FAILED)
            if send_webhook:
                _send_webhook(job_id, 'failed', error=error_msg)
            return

        # Log warnings if any
        if warnings:
            for warning in warnings:
                app.logger.warning(f"PDF security warning: {warning}")

        # Process extraction using file path with progress updates
        result, filename = _process_pdf_extraction_from_path(
            file_path, original_filename, extract_type, pages, include_tables, None, job_id
        )

        # Verify file still exists after extraction (before Spaces upload)
        if not os.path.exists(file_path):
            app.logger.warning(f"File was deleted during extraction: {file_path}")
        else:
            app.logger.info(f"File still exists after extraction: {file_path} (size: {os.path.getsize(file_path)} bytes)")

        if result is None:
            _set_job(job_id, {'status': 'failed', 'error': filename, 'stage': 'failed'}, ttl=REDIS_JOB_TTL_FAILED)
            if send_webhook:
                _send_webhook(job_id, 'failed', error=filename)
            # Clean up file on failure
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    app.logger.debug(f"Cleaned up temporary file after failure: {file_path}")
                except Exception:
                    pass
            return

        # Update: Completed reading
        _set_job(job_id, {
            'status': 'processing',
            'progress': 50,
            'stage': 'reading_complete',
            'message': 'Completed reading PDF, extracting data...'
        })

        # Persist to DB, generate embeddings, upload to Spaces
        db_result = None
        if send_to_nhost:
            # Progress callback so the job status reflects each sub-stage
            stage_to_pct = {
                "spaces_upload":   55,
                "insert_document": 65,
                "insert_chunks":   70,
                "embeddings":      75,
                "store_embeddings": 88,
                "finalise":        95,
            }
            stage_labels = {
                "spaces_upload":    "Uploading PDF to cloud storage…",
                "insert_document":  "Saving document record…",
                "insert_chunks":    "Saving text chunks…",
                "embeddings":       "Generating ChatGPT embeddings…",
                "store_embeddings": "Storing embeddings in database…",
                "finalise":         "Finalising…",
            }

            def _progress_cb(stage, _pct, msg):
                _set_job(job_id, {
                    'status': 'processing',
                    'progress': stage_to_pct.get(stage, 70),
                    'stage': stage,
                    'message': stage_labels.get(stage, msg),
                })

            _set_job(job_id, {
                'status': 'processing',
                'progress': 52,
                'stage': 'storing',
                'message': 'Storing extracted data and generating embeddings…',
            })
            db_result = _send_to_db(
                result, job_id, filename,
                user_id=user_id,
                file_url=file_url,
                upload_device=upload_device,
                file_path=file_path,
                user_display_name=user_display_name,
                progress_cb=_progress_cb,
            )
            if db_result is None:
                app.logger.warning(f"_send_to_db returned None for job {job_id}")
            else:
                app.logger.info(
                    f"DB storage complete for job {job_id}: "
                    f"document_id={db_result.get('document_id')}, "
                    f"chunks={db_result.get('chunk_count')}"
                )

        # Clean up temporary file (Spaces upload already done inside _send_to_db)
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                app.logger.debug(f"Cleaned up temporary file: {file_path}")
            except Exception as e:
                app.logger.warning(f"Failed to clean up temporary file {file_path}: {str(e)}")

        # Update job status – Done
        _set_job(job_id, {
            'status': 'completed',
            'progress': 100,
            'stage': 'done',
            'message': 'Processing complete!',
            'filename': filename,
            'data': result,
            'db_result': db_result,
        }, ttl=REDIS_JOB_TTL_COMPLETED)

        # Send webhook
        if send_webhook:
            _send_webhook(job_id, 'completed', data={
                'filename': filename,
                'extraction': result,
                'db_success': db_result is not None,
                'document_id': db_result.get('document_id') if db_result else None,
            })

    except Exception as e:
        error_msg = str(e)
        _set_job(job_id, {'status': 'failed', 'error': error_msg, 'stage': 'failed'}, ttl=REDIS_JOB_TTL_FAILED)
        if send_webhook:
            _send_webhook(job_id, 'failed', error=error_msg)

    finally:
        # Always release the concurrency slot, regardless of success or failure
        _concurrency_semaphore.release()
        with _active_job_lock:
            _active_job_count -= 1
        app.logger.info(f"Job {job_id} released concurrency slot. Active jobs: {_active_job_count}")


def _process_pdf_extraction(file, extract_type='all', pages=None, include_tables=True):
    """
    Internal function to process PDF extraction from file object.
    
    Args:
        file: Uploaded file object
        extract_type: Type of extraction ('all', 'text', 'metadata', 'tables')
        pages: List of page numbers (0-indexed) or None for all pages
        include_tables: Whether to include tables in extraction
    
    Returns:
        Tuple of (result_dict, filename) or (None, error_message)
    """
    if file.filename == '':
        return None, 'No file selected'
    
    if not allowed_file(file.filename):
        return None, 'Invalid file type. Only PDF files are allowed'
    
    # Save uploaded file temporarily
    filename = secure_filename(file.filename)
    temp_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    try:
        file.save(temp_path)
        return _process_pdf_extraction_from_path(temp_path, filename, extract_type, pages, include_tables)
    except Exception as e:
        return None, str(e)


def _process_pdf_extraction_from_path(file_path, filename, extract_type='all', pages=None, include_tables=True, jobs_dict=None, job_id=None):
    """
    Internal function to process PDF extraction from file path.
    Optimized for large PDFs with progress updates.
    
    Args:
        file_path: Path to PDF file
        filename: Original filename
        extract_type: Type of extraction ('all', 'text', 'metadata', 'tables')
        pages: List of page numbers (0-indexed) or None for all pages
        include_tables: Whether to include tables in extraction (can be slow for large PDFs)
        jobs_dict: Optional jobs dictionary for progress updates
        job_id: Optional job ID for progress updates
    
    Returns:
        Tuple of (result_dict, filename) or (None, error_message)
    """
    try:
        # Extract data with progress updates for large files
        with PDFExtractor(file_path) as extractor:
            # Always get metadata first to know page count
            metadata = extractor.extract_metadata()
            num_pages = metadata.get('num_pages', 0)
            
            if job_id:
                _set_job(job_id, {
                    'status': 'processing',
                    'progress': 10,
                    'stage': 'reading',
                    'message': f'Reading PDF ({num_pages} pages)...'
                })
            
            if extract_type == 'metadata':
                result = {'metadata': metadata}
            elif extract_type == 'text':
                result = {'text': extractor.extract_text(pages)}
            elif extract_type == 'tables':
                result = {'tables': extractor.extract_tables(pages)}
            else:  # 'all'
                # Extract metadata and text first
                result = {
                    'metadata': metadata,
                    'text': extractor.extract_text(pages),
                }
                
                if job_id:
                    _set_job(job_id, {
                        'status': 'processing',
                        'progress': 30,
                        'stage': 'reading',
                        'message': f'Extracted text from {num_pages} pages, processing tables...'
                    })
                
                # Tables can be very slow for large PDFs - extract conditionally
                if include_tables:
                    # For very large PDFs, warn that tables take time
                    if num_pages > 100:
                        if job_id:
                            _set_job(job_id, {
                                'status': 'processing',
                                'progress': 35,
                                'stage': 'reading',
                                'message': f'Extracting tables from {num_pages} pages (this may take a while)...'
                            })
                    result['tables'] = extractor.extract_tables(pages)
        
        if job_id:
            _set_job(job_id, {
                'status': 'processing',
                'progress': 50,
                'stage': 'reading_complete',
                'message': 'Completed reading PDF, preparing data...'
            })
        
        return result, filename
        
    except Exception as e:
        return None, str(e)
    # Note: File cleanup is handled in _process_extraction_async after Spaces upload


@app.route('/', methods=['GET'])
def index():
    """Root endpoint with API information."""
    return jsonify({
        'service': 'PDF Extractor API',
        'status': 'running',
        'endpoints': {
            'health': 'GET /health',
            'extract_all': 'POST /extract',
            'extract_async': 'POST /extract/async',
            'job_status': 'GET /job/<job_id>',
            'extract_metadata': 'POST /extract/metadata',
            'extract_text': 'POST /extract/text',
            'extract_tables': 'POST /extract/tables'
        },
        'documentation': 'See README.md for usage examples',
        'nhost_integration': bool(NHOST_BACKEND_URL and NHOST_ADMIN_SECRET)
    })


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    with _active_job_lock:
        active = _active_job_count
    health_status = {
        'status': 'healthy',
        'service': 'PDF Extractor API',
        'redis': 'connected' if redis_client and redis_client.ping() else 'disconnected',
        'active_jobs': active,
        'max_concurrent_jobs': MAX_CONCURRENT_JOBS,
        'slots_available': MAX_CONCURRENT_JOBS - active,
    }
    return jsonify(health_status)


@app.route('/debug/nhost', methods=['GET'])
def debug_nhost():
    """
    Debug endpoint to check Nhost configuration.
    
    WEBHOOK_URL: Optional URL to your Next.js webhook endpoint.
    Example: https://your-app.vercel.app/api/webhook/pdf-extraction
    This is used to notify your Next.js app when PDF processing completes.
    """
    graphql_url = NHOST_GRAPHQL_URL if NHOST_GRAPHQL_URL else (f"{NHOST_BACKEND_URL}/v1/graphql" if NHOST_BACKEND_URL else None)
    
    config = {
        'nhost_backend_url_set': bool(NHOST_BACKEND_URL),
        'nhost_backend_url': NHOST_BACKEND_URL if NHOST_BACKEND_URL else None,
        'nhost_graphql_url_set': bool(NHOST_GRAPHQL_URL),
        'nhost_graphql_url': NHOST_GRAPHQL_URL if NHOST_GRAPHQL_URL else None,
        'nhost_admin_secret_set': bool(NHOST_ADMIN_SECRET),
        'webhook_url_set': bool(WEBHOOK_URL),
        'webhook_url': WEBHOOK_URL if WEBHOOK_URL else None,
        'graphql_url_being_used': graphql_url,
        'tables': 'tt_ai.documents, tt_ai.chunks',
        'embedding_model': OPENAI_EMBEDDING_MODEL,
        'openai_configured': bool(OPENAI_API_KEY),
        'note': 'If getting 404, check your Nhost dashboard → Settings → API for the correct GraphQL endpoint URL and set NHOST_GRAPHQL_URL'
    }
    return jsonify(config)


@app.route('/extract', methods=['POST'])
def extract_pdf():
    """
    Extract data from uploaded PDF file (synchronous).
    
    WARNING: This endpoint blocks until extraction is complete. For large PDFs (800+ pages),
    this may cause request timeouts. Use /extract/async instead for production.
    
    This endpoint is suitable for:
    - Small PDFs (< 50 pages)
    - Quick testing
    - Simple scripts
    
    Expected form data:
    - file: PDF file to process (required, max 200MB)
    - extract_type: Optional. One of 'all', 'text', 'metadata', 'tables' (default: 'all')
    - pages: Optional. Comma-separated page numbers (1-indexed), e.g., "1,2,3"
    - include_tables: Optional. Boolean string 'true'/'false' (default: 'true')
    - send_to_nhost: Optional. Boolean string 'true'/'false' (default: 'false')
    - user_id: Optional. User ID (UUID format)
    - file_url: Optional. URL if file is stored in S3/storage
    - upload_device: Optional. Device/platform identifier from form upload (default: 'web')
    
    Returns:
        JSON response:
        {
            "success": true,
            "filename": "document.pdf",
            "data": {
                "metadata": {...},
                "text": {...},
                "tables": {...}
            },
            "db_result": {...}  // Only if send_to_nhost=true
        }
    
    Status Codes:
        200: Extraction successful
        400: Bad request or extraction failed
        500: Server error
    """
    try:
        # Check if file is present
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        
        file = request.files['file']
        
        # Get extraction options
        extract_type = request.form.get('extract_type', 'all').lower()
        pages_param = request.form.get('pages', '')
        include_tables = request.form.get('include_tables', 'true').lower() == 'true'
        send_to_nhost = request.form.get('send_to_nhost', 'false').lower() == 'true'
        
        # Parse page numbers
        pages = None
        if pages_param:
            try:
                pages = [int(p.strip()) - 1 for p in pages_param.split(',')]
            except ValueError:
                return jsonify({'error': 'Invalid page numbers format'}), 400
        
        result, filename = _process_pdf_extraction(file, extract_type, pages, include_tables)
        
        if result is None:
            return jsonify({'error': filename}), 400  # filename contains error message
        
        # Optionally persist to DB + generate embeddings
        db_result = None
        if send_to_nhost:
            job_id = str(uuid.uuid4())
            user_id = request.form.get('userId') or request.form.get('user_id')
            file_url = request.form.get('file_url')
            upload_device = request.form.get('upload_device', 'web')
            user_display_name = request.form.get('user_display_name')
            # Synchronous endpoint has no local file path for Spaces upload
            db_result = _send_to_db(
                result, job_id, filename,
                user_id=user_id,
                file_url=file_url,
                upload_device=upload_device,
                file_path=None,
                user_display_name=user_display_name,
            )

        response = {
            'success': True,
            'filename': filename,
            'data': result,
        }

        if db_result:
            response['db_result'] = db_result
        
        return jsonify(response)
                
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/extract/async', methods=['POST'])
def extract_pdf_async():
    """
    Extract data from uploaded PDF file asynchronously.
    
    This endpoint is RECOMMENDED for production use, especially for large PDFs (800+ pages).
    It returns immediately with a job_id, allowing the client to poll for status updates.
    The extraction runs in a background thread, preventing request timeouts.
    
    The extracted data is automatically:
    1. Chunked intelligently for embeddings (1500 chars per chunk with 400 char overlap for better context)
    2. Sent to Nhost/Hasura database (if enabled)
    3. Webhook notification sent to Next.js (if configured)
    
    Expected form data:
    - file: PDF file to process (required, max 200MB)
    - extract_type: Optional. One of 'all', 'text', 'metadata', 'tables' (default: 'all')
    - pages: Optional. Comma-separated page numbers (1-indexed), e.g., "1,2,3"
    - include_tables: Optional. Boolean string 'true'/'false' (default: 'true')
      Note: Table extraction can be slow for large PDFs (>100 pages)
    - send_to_nhost: Optional. Boolean string 'true'/'false' (default: 'true')
    - send_webhook: Optional. Boolean string 'true'/'false' (default: 'true')
    - user_id: Optional. User ID from Next.js (UUID format)
    - file_url: Optional. URL if file is stored in S3/storage
    - upload_device: Optional. Device/platform identifier (default: 'web')
    
    Returns:
        JSON response with status 202 (Accepted):
        {
            "success": true,
            "job_id": "uuid-string",
            "status": "processing",
            "message": "Extraction started. Use /job/<job_id> to check status."
        }
    
    Example:
        curl -X POST -F "file=@manual.pdf" \\
             -F "send_to_nhost=true" \\
             -F "send_webhook=true" \\
             https://api.example.com/extract/async
    
    Status Codes:
        202: Extraction started successfully
        400: Bad request (no file, invalid parameters)
        500: Server error
    """
    global _active_job_count

    # -----------------------------------------------------------------------
    # Concurrency gate: reject immediately if all slots are taken
    # -----------------------------------------------------------------------
    slot_acquired = _concurrency_semaphore.acquire(blocking=False)
    if not slot_acquired:
        with _active_job_lock:
            active = _active_job_count
        return jsonify({
            'success': False,
            'error': 'Busy – try again later',
            'active_jobs': active,
            'max_concurrent_jobs': MAX_CONCURRENT_JOBS,
        }), 503

    # We now hold a slot; increment the counter
    with _active_job_lock:
        _active_job_count += 1

    try:
        # Check if file is present
        if 'file' not in request.files:
            _concurrency_semaphore.release()
            with _active_job_lock:
                _active_job_count -= 1
            return jsonify({'error': 'No file provided'}), 400

        file = request.files['file']

        # Get extraction options
        extract_type = request.form.get('extract_type', 'all').lower()
        pages_param = request.form.get('pages', '')
        include_tables = request.form.get('include_tables', 'true').lower() == 'true'
        send_to_nhost = request.form.get('send_to_nhost', 'true').lower() == 'true'
        send_webhook = request.form.get('send_webhook', 'true').lower() == 'true'
        # Accept both camelCase 'userId' (preferred) and snake_case 'user_id' (legacy)
        user_id = request.form.get('userId') or request.form.get('user_id')
        file_url = request.form.get('file_url')
        upload_device = request.form.get('upload_device', 'web')
        user_display_name = request.form.get('user_display_name')

        # Parse page numbers
        pages = None
        if pages_param:
            try:
                pages = [int(p.strip()) - 1 for p in pages_param.split(',')]
            except ValueError:
                _concurrency_semaphore.release()
                with _active_job_lock:
                    _active_job_count -= 1
                return jsonify({'error': 'Invalid page numbers format'}), 400

        # Generate job ID
        job_id = str(uuid.uuid4())

        # Save file to disk before starting async thread (file object closes when request ends)
        filename = secure_filename(file.filename)
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{job_id}_{filename}")

        try:
            file.save(temp_path)
            if os.path.exists(temp_path):
                file_size = os.path.getsize(temp_path)
                app.logger.info(f"File saved successfully: {temp_path} (size: {file_size} bytes)")
            else:
                app.logger.error(f"File save failed - file does not exist: {temp_path}")
                _concurrency_semaphore.release()
                with _active_job_lock:
                    _active_job_count -= 1
                return jsonify({'success': False, 'error': 'Failed to save file to disk'}), 500
        except Exception as e:
            app.logger.error(f"Error saving file: {str(e)}")
            _concurrency_semaphore.release()
            with _active_job_lock:
                _active_job_count -= 1
            return jsonify({'success': False, 'error': f'Failed to save file: {str(e)}'}), 500

        # Validate PDF file (basic validation)
        is_valid, error_msg = validate_pdf_file(temp_path)
        if not is_valid:
            try:
                os.remove(temp_path)
            except Exception:
                pass
            _concurrency_semaphore.release()
            with _active_job_lock:
                _active_job_count -= 1
            return jsonify({'success': False, 'error': error_msg}), 400

        # Start async processing — the thread owns the semaphore slot and will release it
        app.logger.info(f"Starting async job {job_id}. Active jobs: {_active_job_count}/{MAX_CONCURRENT_JOBS}")
        thread = threading.Thread(
            target=_process_extraction_async,
            args=(temp_path, filename, job_id, extract_type, pages, include_tables,
                  send_to_nhost, send_webhook, user_id, file_url, upload_device, user_display_name)
        )
        thread.daemon = True
        thread.start()

        return jsonify({
            'success': True,
            'job_id': job_id,
            'status': 'processing',
            'message': 'Extraction started. Use /job/<job_id> to check status.',
            'active_jobs': _active_job_count,
            'max_concurrent_jobs': MAX_CONCURRENT_JOBS,
        }), 202

    except Exception as e:
        # If we reach here without having handed off the slot to the thread, release it
        _concurrency_semaphore.release()
        with _active_job_lock:
            _active_job_count -= 1
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/job/<job_id>', methods=['GET'])
def get_job_status(job_id):
    """
    Get the status of an async extraction job.
    
    Poll this endpoint to track the progress of a PDF extraction job.
    Recommended polling interval: 2-5 seconds.
    
    Job Stages:
        - file_received: File uploaded and saved
        - reading: Extracting text/metadata from PDF
        - reading_complete: Text extraction finished
        - sending_to_db: Chunking text and sending to database
        - done: Processing complete
        - failed: Error occurred
    
    Args:
        job_id: UUID of the job (returned from /extract/async)
    
    Returns:
        JSON response with job status:
        
        Processing:
        {
            "job_id": "uuid",
            "status": "processing",
            "progress": 45,
            "stage": "reading",
            "message": "Reading PDF (800 pages)..."
        }
        
        Completed:
        {
            "job_id": "uuid",
            "status": "completed",
            "progress": 100,
            "stage": "done",
            "message": "Processing complete!",
            "filename": "manual.pdf",
            "data": {...},
            "db_result": {"document_id": "uuid", "chunk_count": 42}
        }
        
        Failed:
        {
            "job_id": "uuid",
            "status": "failed",
            "stage": "failed",
            "error": "Error message"
        }
    
    Status Codes:
        200: Job found (status may be processing, completed, or failed)
        404: Job not found
    """
    job = _get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    response = {
        'job_id': job_id,
        'status': job['status']
    }
    
    if job['status'] == 'processing':
        response['progress'] = job.get('progress', 0)
        response['stage'] = job.get('stage', 'processing')
        response['message'] = job.get('message', 'Processing...')
    elif job['status'] == 'completed':
        response['progress'] = 100
        response['stage'] = job.get('stage', 'done')
        response['message'] = job.get('message', 'Processing complete!')
        response['filename'] = job.get('filename')
        response['data'] = job.get('data')
        response['db_result'] = job.get('db_result')
    elif job['status'] == 'failed':
        response['error'] = job.get('error')
        response['stage'] = job.get('stage', 'failed')
    
    return jsonify(response)


@app.route('/extract/metadata', methods=['POST'])
def extract_metadata_only():
    """Extract only metadata from PDF."""
    return _extract_with_type('metadata')


@app.route('/extract/text', methods=['POST'])
def extract_text_only():
    """Extract only text from PDF."""
    return _extract_with_type('text')


@app.route('/extract/tables', methods=['POST'])
def extract_tables_only():
    """Extract only tables from PDF."""
    return _extract_with_type('tables')


def _extract_with_type(extract_type):
    """Helper to extract specific type."""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        
        file = request.files['file']
        
        # Get other options
        pages_param = request.form.get('pages', '')
        pages = None
        if pages_param:
            try:
                pages = [int(p.strip()) - 1 for p in pages_param.split(',')]
            except ValueError:
                return jsonify({'error': 'Invalid page numbers format'}), 400
        
        result, filename = _process_pdf_extraction(file, extract_type, pages, True)
        
        if result is None:
            return jsonify({'error': filename}), 400
        
        return jsonify({
            'success': True,
            'filename': filename,
            'data': result
        })
                
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.errorhandler(413)
def request_entity_too_large(error):
    """Handle file too large error."""
    return jsonify({'error': 'File too large. Maximum size is 200MB'}), 413


@app.errorhandler(500)
def internal_error(error):
    """Handle internal server errors."""
    return jsonify({'error': 'Internal server error'}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
