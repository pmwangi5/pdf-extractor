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
MAX_CHARS_PER_CHUNK = 2000  # Maximum characters per chunk
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
    
    Args:
        text: Text to analyze
    
    Returns:
        Tuple of (is_dangerous: bool, reason: str)
    """
    if not text:
        return False, None
    
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
            # Log but don't reject (fail open) - adjust based on your security policy
            # To reject: return True, reason
            # To flag only: return False, reason (current behavior)
    
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
                'ACL': 'private'  # Change to 'public-read' if you want public access
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
    Normalize text by removing inconsistent formatting, line breaks, and hyphenation artifacts.
    
    Args:
        text: Raw text string
    
    Returns:
        Normalized text string
    """
    if not text:
        return ""
    
    # Normalize line breaks - replace multiple newlines with double newline (paragraph break)
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    # Remove hyphenation artifacts (hyphen at end of line followed by word on next line)
    # Pattern: word-\nword becomes wordword, but we want word word
    text = re.sub(r'(\w+)-\s*\n\s*(\w+)', r'\1\2', text)
    # Fix cases where hyphenation was removed but should be a space
    text = re.sub(r'(\w{2,})([A-Z][a-z]+)', r'\1 \2', text)  # camelCase split
    
    # Normalize bullet points - convert various bullet styles to consistent •
    # Match common bullet patterns: -, *, •, o, etc.
    text = re.sub(r'^\s*[-*•o]\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+[.)]\s+', r'\g<0>', text)  # Keep numbered lists as-is
    
    # Normalize whitespace - multiple spaces to single space (but preserve paragraph breaks)
    text = re.sub(r'[ \t]+', ' ', text)
    
    # Remove trailing whitespace from lines but keep paragraph structure
    lines = text.split('\n')
    normalized_lines = [line.rstrip() for line in lines]
    text = '\n'.join(normalized_lines)
    
    # Fix inconsistent capitalization at sentence starts (optional - can be aggressive)
    # We'll keep original capitalization but ensure proper sentence boundaries
    
    return text.strip()


def _split_into_semantic_units(text):
    """
    Split text into semantic units (paragraphs, bullet points, sentences).
    Each unit should ideally contain a single idea.
    
    Args:
        text: Normalized text string
    
    Returns:
        List of semantic units (strings)
    """
    if not text:
        return []
    
    units = []
    
    # First, split by double newlines (paragraph breaks)
    paragraphs = text.split('\n\n')
    
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        
        # Check if paragraph is a bullet list
        bullet_pattern = r'^•\s+'
        if re.match(bullet_pattern, para, re.MULTILINE):
            # Split bullet list into individual bullets
            bullets = re.split(r'\n(?=•\s+)', para)
            for bullet in bullets:
                bullet = bullet.strip()
                if bullet:
                    units.append(bullet)
        else:
            # Check if it's a numbered list
            if re.match(r'^\d+[.)]\s+', para, re.MULTILINE):
                # Split numbered list items
                items = re.split(r'\n(?=\d+[.)]\s+)', para)
                for item in items:
                    item = item.strip()
                    if item:
                        units.append(item)
            else:
                # Regular paragraph - check if it's too long
                # If paragraph is very long, try to split by sentences
                if len(para) > 800:
                    # Split by sentence boundaries
                    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', para)
                    current_unit = ""
                    for sentence in sentences:
                        sentence = sentence.strip()
                        if not sentence:
                            continue
                        
                        # If adding this sentence would exceed reasonable size, save current
                        if current_unit and len(current_unit) + len(sentence) + 1 > 600:
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


def _chunk_text_for_embeddings(text_by_page, chunk_size=1000, overlap=200):
    """
    Chunk text intelligently for embeddings with improved semantic splitting.
    For large PDFs (800+ pages), we need to split text into manageable chunks.
    Each chunk should ideally contain a single idea.
    
    Args:
        text_by_page: Dictionary of page text data
        chunk_size: Target characters per chunk (default 1000, good for most embedding models)
        overlap: Characters to overlap between chunks for context (default 200)
    
    Returns:
        List of chunk dictionaries with text, page info, and metadata
    """
    chunks = []
    
    # Sort pages by page number
    sorted_pages = sorted(
        text_by_page.items(),
        key=lambda x: x[1].get('page_number', 0) if isinstance(x[1], dict) else 0
    )
    
    # First pass: normalize all text and split into semantic units
    all_units = []
    unit_to_page = {}  # Map unit index to page number
    
    for page_key, page_data in sorted_pages:
        if not isinstance(page_data, dict) or 'text' not in page_data:
            continue
            
        page_text = page_data.get('text', '')
        page_num = page_data.get('page_number', 0)
        
        if not page_text.strip():
            continue
        
        # Normalize the page text
        normalized_text = _normalize_text(page_text)
        
        # Sanitize text for embeddings (remove dangerous content)
        sanitized_text = sanitize_text_for_embeddings(normalized_text)
        
        # Detect dangerous content (log warnings)
        is_dangerous, reason = detect_dangerous_content(sanitized_text)
        if is_dangerous:
            app.logger.error(f"Dangerous content detected in page {page_num}: {reason}")
            # Optionally skip this page or reject entire PDF
            # For now, we log and continue (adjust based on security policy)
        
        # Split into semantic units
        units = _split_into_semantic_units(sanitized_text)
        
        # Track which page each unit belongs to
        for unit in units:
            unit_index = len(all_units)
            all_units.append(unit)
            unit_to_page[unit_index] = page_num
    
    # Second pass: combine units into chunks
    current_chunk = ""
    current_pages = set()
    chunk_index = 0
    
    for i, unit in enumerate(all_units):
        unit_page = unit_to_page.get(i, 0)
        
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
                    'char_count': len(current_chunk),
                    'start_page': min(current_pages) if current_pages else 0,
                    'end_page': max(current_pages) if current_pages else 0
                })
                chunk_index += 1
            
            # Start new chunk with overlap from previous
            if overlap > 0 and current_chunk:
                # Take last 'overlap' characters for context
                overlap_text = current_chunk[-overlap:].strip()
                # Try to start at a sentence or unit boundary
                # If overlap text ends mid-sentence, try to find last complete sentence
                sentences = re.split(r'(?<=[.!?])\s+', overlap_text)
                if len(sentences) > 1:
                    overlap_text = ' '.join(sentences[-2:])
                elif len(sentences) == 1 and len(overlap_text) > overlap // 2:
                    # If we have a long single sentence, take last part
                    overlap_text = overlap_text[-overlap // 2:]
                
                current_chunk = overlap_text + "\n\n" + unit
                # Keep pages from overlap (last page)
                current_pages = {max(current_pages)} if current_pages else {unit_page}
            else:
                current_chunk = unit
                current_pages = {unit_page}
        else:
            # Add unit to current chunk
            if current_chunk:
                current_chunk += "\n\n" + unit
            else:
                current_chunk = unit
            current_pages.add(unit_page)
    
    # Add final chunk
    if current_chunk.strip():
        chunks.append({
            'chunk_index': chunk_index,
            'text': current_chunk.strip(),
            'pages': sorted(list(current_pages)),
            'char_count': len(current_chunk),
            'start_page': min(current_pages) if current_pages else 0,
            'end_page': max(current_pages) if current_pages else 0
        })
    
    # Limit total chunks per PDF (prevent resource exhaustion)
    if len(chunks) > MAX_CHUNKS_PER_PDF:
        app.logger.warning(f"PDF has {len(chunks)} chunks, limiting to {MAX_CHUNKS_PER_PDF}")
        chunks = chunks[:MAX_CHUNKS_PER_PDF]
    
    return chunks


def _send_to_nhost(data, job_id, filename, user_id=None, jobs_dict=None, file_url=None, upload_device="web", file_path=None, user_display_name=None):
    """
    Send extracted data to Nhost for embeddings.
    For large PDFs, chunks text intelligently for better embedding generation.
    
    Args:
        data: Extracted PDF data
        job_id: Job identifier
        filename: Original filename (stored in metadata)
        user_id: Optional user ID
        jobs_dict: Optional jobs dictionary for progress updates
        file_url: Optional URL if file is stored in S3/storage
        upload_device: Device/platform used for upload (default: 'web')
        file_path: Optional path to local file for uploading to Spaces after embedding creation
        user_display_name: Optional display name of the user for email notifications
    """
    if not NHOST_BACKEND_URL or not NHOST_ADMIN_SECRET:
        app.logger.warning("Nhost configuration missing, skipping Nhost integration")
        app.logger.warning(f"NHOST_BACKEND_URL: {NHOST_BACKEND_URL[:50] if NHOST_BACKEND_URL else 'NOT SET'}...")
        app.logger.warning(f"NHOST_ADMIN_SECRET: {'SET' if NHOST_ADMIN_SECRET else 'NOT SET'}")
        return None
    
    app.logger.info(f"Attempting to send data to Nhost for job {job_id}")
    app.logger.info(f"Nhost URL: {NHOST_BACKEND_URL}/v1/graphql")
    
    try:
        # Prepare data for Nhost
        # For large PDFs, we chunk the text for embeddings instead of one huge string
        text_by_page = data.get('text', {})
        
        # Create chunks for embeddings (important for 800+ page PDFs)
        chunks = _chunk_text_for_embeddings(text_by_page, chunk_size=1000, overlap=200)
        
        # Also keep full text for reference (but chunked version is better for embeddings)
        combined_text = ""
        if text_by_page:
            for page_key, page_data in text_by_page.items():
                if isinstance(page_data, dict) and 'text' in page_data:
                    combined_text += page_data['text'] + "\n\n"
        
        # Prepare payload for Nhost
        # Store filename in metadata for reference
        metadata = data.get('metadata', {}).copy()
        if filename and 'filename' not in metadata:
            metadata['filename'] = filename
        
        payload = {
            'job_id': job_id,
            'metadata': metadata,
            'text': combined_text,
            'text_by_page': data.get('text', {}),
            'tables': data.get('tables', {}),
            'status': 'ready_for_embedding'
        }
        
        # Send to Nhost GraphQL endpoint or REST endpoint
        # This example uses a REST endpoint - adjust based on your Nhost setup
        headers = {
            'Content-Type': 'application/json',
            'x-hasura-admin-secret': NHOST_ADMIN_SECRET
        }
        
        # GraphQL mutation endpoint
        # Use NHOST_GRAPHQL_URL if set, otherwise construct from NHOST_BACKEND_URL
        if NHOST_GRAPHQL_URL:
            graphql_url = NHOST_GRAPHQL_URL
        else:
            # Default: append /v1/graphql to backend URL
            # If this doesn't work, set NHOST_GRAPHQL_URL environment variable
            # with the exact GraphQL endpoint from your Nhost dashboard
            graphql_url = f"{NHOST_BACKEND_URL}/v1/graphql"
        
        app.logger.info(f"GraphQL URL: {graphql_url}")
        
        # Build mutation object matching Nhost table structure
        # Table: pdf_embeddings
        # Columns: id (auto), created_at (auto), job_id, user_id, file_url, pdf_jpg, metadata, 
        #          text_content, text_by_page, text_chunks, chunk_count, tables, 
        #          status, nhost_embedding_id (optional), upload_device
        mutation_object = {
            "job_id": job_id,
            "metadata": payload['metadata'],
            "text_content": combined_text,  # Full text for reference
            "text_by_page": payload['text_by_page'],  # Page-by-page text
            "text_chunks": chunks,  # Chunked text for embeddings (important for large PDFs)
            "chunk_count": len(chunks),  # Number of chunks created
            "tables": payload['tables'],
            "status": "ready_for_embedding",
            "file_url": file_url,  # Can be set if file is stored in S3/storage (nullable)
            "pdf_jpg": None,  # Will be updated after JPG conversion and upload
            "upload_device": upload_device  # Required: Device/platform from form upload
        }
        
        # Add user_id if provided
        if user_id:
            mutation_object["user_id"] = user_id
        
        graphql_mutation = {
            "query": """
                mutation InsertPDFEmbedding($object: pdf_embeddings_insert_input!) {
                    insert_pdf_embeddings_one(object: $object) {
                        id
                        job_id
                        status
                        chunk_count
                        created_at
                    }
                }
            """,
            "variables": {
                "object": mutation_object
            }
        }
        
        # For large PDFs, increase timeout
        timeout = 120 if len(chunks) > 100 else 60 if len(chunks) > 50 else 30
        
        app.logger.info(f"Sending GraphQL mutation to Nhost (chunks: {len(chunks)}, timeout: {timeout}s)")
        app.logger.debug(f"Mutation object keys: {list(mutation_object.keys())}")
        app.logger.debug(f"User ID: {user_id}, Upload device: {upload_device}")
        
        response = requests.post(
            graphql_url,
            json=graphql_mutation,
            headers=headers,
            timeout=timeout
        )
        
        app.logger.info(f"Nhost response status: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            app.logger.debug(f"Nhost response: {result}")
            
            if 'errors' in result:
                app.logger.error(f"Nhost GraphQL errors: {result['errors']}")
                app.logger.error(f"Full error response: {result}")
                return None
            
            if 'data' in result and result.get('data', {}).get('insert_pdf_embeddings_one'):
                pdf_embedding_id = result['data']['insert_pdf_embeddings_one'].get('id')
                app.logger.info(f"Successfully sent data to Nhost for job {job_id}")
                app.logger.info(f"Inserted record ID: {pdf_embedding_id}")
                
                # Create subscriber entry if user_id is provided
                if user_id and pdf_embedding_id:
                    subscriber_result = _create_subscriber_entry(pdf_embedding_id, user_id, graphql_url, headers)
                    if subscriber_result:
                        app.logger.info(f"Created subscriber entry for user {user_id} and embedding {pdf_embedding_id}")
                    else:
                        app.logger.warning(f"Failed to create subscriber entry for user {user_id} and embedding {pdf_embedding_id}")
                
                # Upload PDF to DigitalOcean Spaces after successful embedding creation
                spaces_url = None
                jpg_url = None
                if file_path and pdf_embedding_id:
                    app.logger.info(f"Checking file for Spaces upload: {file_path}, exists: {os.path.exists(file_path) if file_path else False}")
                    if os.path.exists(file_path):
                        file_size = os.path.getsize(file_path)
                        app.logger.info(f"Uploading PDF to Spaces for embedding {pdf_embedding_id}, file: {file_path} (size: {file_size} bytes)")
                        spaces_url = upload_to_spaces(file_path, filename, pdf_embedding_id, content_type='application/pdf')
                        
                        # Update file_url in database if upload was successful
                        if spaces_url:
                            app.logger.info(f"Spaces upload successful, updating database with URL: {spaces_url}")
                            _update_file_url(pdf_embedding_id, spaces_url, graphql_url, headers)
                            
                            # Convert first page to JPG and upload
                            app.logger.info(f"Converting first page of PDF to JPG for embedding {pdf_embedding_id}")
                            jpg_path = convert_pdf_first_page_to_jpg(file_path)
                            
                            if jpg_path and os.path.exists(jpg_path):
                                # Generate JPG filename
                                jpg_filename = os.path.splitext(secure_filename(filename))[0] + "_preview.jpg"
                                app.logger.info(f"Uploading JPG preview to Spaces: {jpg_path}")
                                jpg_url = upload_to_spaces(jpg_path, jpg_filename, pdf_embedding_id, content_type='image/jpeg')
                                
                                if jpg_url:
                                    app.logger.info(f"JPG upload successful, updating database with URL: {jpg_url}")
                                    _update_pdf_jpg(pdf_embedding_id, jpg_url, graphql_url, headers)
                                    
                                    # Clean up temporary JPG file
                                    try:
                                        os.remove(jpg_path)
                                        app.logger.debug(f"Cleaned up temporary JPG file: {jpg_path}")
                                    except Exception as cleanup_error:
                                        app.logger.warning(f"Failed to clean up JPG file {jpg_path}: {str(cleanup_error)}")
                                else:
                                    app.logger.warning(f"JPG upload failed for embedding {pdf_embedding_id}")
                            else:
                                app.logger.warning(f"PDF to JPG conversion failed for embedding {pdf_embedding_id}")
                        else:
                            app.logger.warning(f"Spaces upload failed for embedding {pdf_embedding_id}")
                    else:
                        app.logger.warning(f"File does not exist at path: {file_path}, skipping Spaces upload")
                elif not file_path:
                    app.logger.debug("file_path not provided, skipping Spaces upload")
                elif not pdf_embedding_id:
                    app.logger.warning("pdf_embedding_id not available, skipping Spaces upload")
                
                # Send email notification after successful embedding creation
                if user_id:
                    _send_email_notification(filename, user_id, user_display_name, pdf_embedding_id)
                
                return result.get('data', {})
            else:
                app.logger.warning(f"Unexpected response structure: {result}")
                return None
        else:
            app.logger.error(f"Nhost request failed: {response.status_code} - {graphql_url}")
            app.logger.error(f"Response text: {response.text}")
            try:
                error_json = response.json()
                app.logger.error(f"Error JSON: {error_json}")
            except:
                pass
            return None
            
    except Exception as e:
        app.logger.error(f"Error sending to Nhost: {str(e)}")
        return None


def _create_subscriber_entry(pdf_embedding_id, user_id, graphql_url, headers):
    """
    Create a subscriber entry in pdf_embeddings_subscribers table.
    
    Args:
        pdf_embedding_id: UUID of the pdf_embeddings record
        user_id: UUID of the user
        graphql_url: Nhost GraphQL endpoint URL
        headers: Request headers with admin secret
    
    Returns:
        Dictionary with subscriber data if successful, None otherwise
    """
    try:
        mutation_object = {
            "user_id": user_id,  # Note: using user_ID as per table structure
            "pdf_embedding_id": pdf_embedding_id,
            "useCount": 0
        }
        
        graphql_mutation = {
            "query": """
                mutation InsertPDFEmbeddingSubscriber($object: pdf_embeddings_subscribers_insert_input!) {
                    insert_pdf_embeddings_subscribers_one(object: $object) {
                        id
                        user_id
                        pdf_embedding_id
                        useCount
                        created_at
                    }
                }
            """,
            "variables": {
                "object": mutation_object
            }
        }
        
        app.logger.info(f"Creating subscriber entry for embedding {pdf_embedding_id} and user {user_id}")
        
        response = requests.post(
            graphql_url,
            json=graphql_mutation,
            headers=headers,
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            
            if 'errors' in result:
                app.logger.error(f"GraphQL errors creating subscriber: {result['errors']}")
                return None
            
            if 'data' in result and result.get('data', {}).get('insert_pdf_embeddings_subscribers_one'):
                subscriber_data = result['data']['insert_pdf_embeddings_subscribers_one']
                app.logger.info(f"Successfully created subscriber entry: {subscriber_data.get('id')}")
                return subscriber_data
            else:
                app.logger.warning(f"Unexpected subscriber response structure: {result}")
                return None
        else:
            app.logger.error(f"Failed to create subscriber entry: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        app.logger.error(f"Error creating subscriber entry: {str(e)}")
        return None


def _update_file_url(pdf_embedding_id, file_url, graphql_url, headers):
    """
    Update the file_url field in pdf_embeddings table after successful Spaces upload.
    
    Args:
        pdf_embedding_id: UUID of the pdf_embeddings record
        file_url: URL of the file in Spaces
        graphql_url: Nhost GraphQL endpoint URL
        headers: Request headers with admin secret
    """
    try:
        mutation = {
            "query": """
                mutation UpdatePDFEmbeddingFileUrl($id: uuid!, $file_url: String!) {
                    update_pdf_embeddings_by_pk(pk_columns: {id: $id}, _set: {file_url: $file_url}) {
                        id
                        file_url
                    }
                }
            """,
            "variables": {
                "id": pdf_embedding_id,
                "file_url": file_url
            }
        }
        
        response = requests.post(
            graphql_url,
            json=mutation,
            headers=headers,
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            if 'errors' not in result:
                app.logger.info(f"Updated file_url for embedding {pdf_embedding_id}")
            else:
                app.logger.warning(f"Error updating file_url: {result.get('errors')}")
        else:
            app.logger.warning(f"Failed to update file_url: {response.status_code}")
            
    except Exception as e:
        app.logger.error(f"Error updating file_url: {str(e)}")


def _update_pdf_jpg(pdf_embedding_id, pdf_jpg_url, graphql_url, headers):
    """
    Update the pdf_jpg field in pdf_embeddings table after successful JPG upload.
    
    Args:
        pdf_embedding_id: UUID of the pdf_embeddings record
        pdf_jpg_url: URL of the JPG file in Spaces
        graphql_url: Nhost GraphQL endpoint URL
        headers: Request headers with admin secret
    """
    try:
        mutation = {
            "query": """
                mutation UpdatePDFEmbeddingJpg($id: uuid!, $pdf_jpg: String!) {
                    update_pdf_embeddings_by_pk(pk_columns: {id: $id}, _set: {pdf_jpg: $pdf_jpg}) {
                        id
                        pdf_jpg
                    }
                }
            """,
            "variables": {
                "id": pdf_embedding_id,
                "pdf_jpg": pdf_jpg_url
            }
        }
        
        response = requests.post(
            graphql_url,
            json=mutation,
            headers=headers,
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            if 'errors' not in result:
                app.logger.info(f"Updated pdf_jpg for embedding {pdf_embedding_id}")
            else:
                app.logger.warning(f"Error updating pdf_jpg: {result.get('errors')}")
        else:
            app.logger.warning(f"Failed to update pdf_jpg: {response.status_code}")
            
    except Exception as e:
        app.logger.error(f"Error updating pdf_jpg: {str(e)}")


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
        
        # Send to Nhost if enabled
        nhost_result = None
        if send_to_nhost:
            _set_job(job_id, {
                'status': 'processing',
                'progress': 60,
                'stage': 'sending_to_db',
                'message': 'Chunking text for embeddings...'
            })
            app.logger.info(f"Calling _send_to_nhost for job {job_id}, user_id: {user_id}, upload_device: {upload_device}")
            # user_display_name is already passed as a parameter to this function
            nhost_result = _send_to_nhost(result, job_id, filename, user_id, None, file_url=file_url, upload_device=upload_device, file_path=file_path, user_display_name=user_display_name)
            if nhost_result is None:
                app.logger.warning(f"Failed to send data to Nhost for job {job_id}")
            else:
                app.logger.info(f"Successfully sent to Nhost for job {job_id}: {nhost_result}")
            _set_job(job_id, {
                'status': 'processing',
                'progress': 90,
                'stage': 'sending_to_db',
                'message': 'Data sent to database successfully'
            })
        
        # Clean up temporary file after all processing is complete (including Spaces upload)
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                app.logger.debug(f"Cleaned up temporary file: {file_path}")
            except Exception as e:
                app.logger.warning(f"Failed to clean up temporary file {file_path}: {str(e)}")
        
        # Update job status - Done (completed jobs expire after 1 hour)
        _set_job(job_id, {
            'status': 'completed',
            'progress': 100,
            'stage': 'done',
            'message': 'Processing complete!',
            'filename': filename,
            'data': result,
            'nhost_result': nhost_result
        }, ttl=REDIS_JOB_TTL_COMPLETED)
        
        # Send webhook
        if send_webhook:
            _send_webhook(job_id, 'completed', data={
                'filename': filename,
                'extraction': result,
                'nhost_success': nhost_result is not None
            })
            
    except Exception as e:
        error_msg = str(e)
        _set_job(job_id, {'status': 'failed', 'error': error_msg, 'stage': 'failed'}, ttl=REDIS_JOB_TTL_FAILED)
        if send_webhook:
            _send_webhook(job_id, 'failed', error=error_msg)


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
    health_status = {
        'status': 'healthy',
        'service': 'PDF Extractor API',
        'redis': 'connected' if redis_client and redis_client.ping() else 'disconnected'
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
        'table_name': 'pdf_embeddings',
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
            "nhost_result": {...}  // Only if send_to_nhost=true
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
        
        # Optionally send to Nhost
        nhost_result = None
        if send_to_nhost:
            job_id = str(uuid.uuid4())
            user_id = request.form.get('user_id')
            file_url = request.form.get('file_url')
            upload_device = request.form.get('upload_device', 'web')
            user_display_name = request.form.get('user_display_name')
            # Synchronous endpoint doesn't save file to disk, so file_path is None
            nhost_result = _send_to_nhost(result, job_id, filename, user_id, jobs, file_url=file_url, upload_device=upload_device, file_path=None, user_display_name=user_display_name)
        
        response = {
            'success': True,
            'filename': filename,
            'data': result
        }
        
        if nhost_result:
            response['nhost_result'] = nhost_result
        
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
    1. Chunked intelligently for embeddings (1000 chars per chunk with 200 char overlap)
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
    try:
        # Check if file is present
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        
        file = request.files['file']
        
        # Get extraction options
        extract_type = request.form.get('extract_type', 'all').lower()
        pages_param = request.form.get('pages', '')
        include_tables = request.form.get('include_tables', 'true').lower() == 'true'
        send_to_nhost = request.form.get('send_to_nhost', 'true').lower() == 'true'
        send_webhook = request.form.get('send_webhook', 'true').lower() == 'true'
        user_id = request.form.get('user_id')
        file_url = request.form.get('file_url')  # Optional: URL if file is in S3/storage
        upload_device = request.form.get('upload_device', 'web')  # Required: Device/platform from form upload
        user_display_name = request.form.get('user_display_name')  # Optional: User display name for email notifications
        
        # Parse page numbers
        pages = None
        if pages_param:
            try:
                pages = [int(p.strip()) - 1 for p in pages_param.split(',')]
            except ValueError:
                return jsonify({'error': 'Invalid page numbers format'}), 400
        
        # Generate job ID
        job_id = str(uuid.uuid4())
        
        # Save file to disk before starting async thread (file object closes when request ends)
        filename = secure_filename(file.filename)
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{job_id}_{filename}")
        
        try:
            file.save(temp_path)
            # Verify file was saved
            if os.path.exists(temp_path):
                file_size = os.path.getsize(temp_path)
                app.logger.info(f"File saved successfully: {temp_path} (size: {file_size} bytes)")
            else:
                app.logger.error(f"File save failed - file does not exist: {temp_path}")
                return jsonify({
                    'success': False,
                    'error': 'Failed to save file to disk'
                }), 500
        except Exception as e:
            app.logger.error(f"Error saving file: {str(e)}")
            return jsonify({
                'success': False,
                'error': f'Failed to save file: {str(e)}'
            }), 500
        
        # Validate PDF file (basic validation)
        is_valid, error_msg = validate_pdf_file(temp_path)
        if not is_valid:
            # Clean up file
            try:
                os.remove(temp_path)
            except:
                pass
            return jsonify({
                'success': False,
                'error': error_msg
            }), 400
        
        # Start async processing with file path instead of file object
        thread = threading.Thread(
            target=_process_extraction_async,
            args=(temp_path, filename, job_id, extract_type, pages, include_tables, send_to_nhost, send_webhook, user_id, file_url, upload_device, user_display_name)
        )
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'success': True,
            'job_id': job_id,
            'status': 'processing',
            'message': 'Extraction started. Use /job/<job_id> to check status.'
        }), 202
                
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


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
            "nhost_result": {...}
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
        response['nhost_result'] = job.get('nhost_result')
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
