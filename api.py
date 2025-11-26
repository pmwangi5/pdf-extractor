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
import tempfile
import threading
import uuid
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import requests
from pdf_extractor import PDFExtractor

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
UPLOAD_FOLDER = tempfile.gettempdir()

# Nhost configuration
NHOST_BACKEND_URL = os.environ.get('NHOST_BACKEND_URL', '').rstrip('/')
NHOST_ADMIN_SECRET = os.environ.get('NHOST_ADMIN_SECRET', '')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')

app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# In-memory job storage (use Redis in production)
jobs = {}


def allowed_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _chunk_text_for_embeddings(text_by_page, chunk_size=1000, overlap=200):
    """
    Chunk text intelligently for embeddings.
    For large PDFs (800+ pages), we need to split text into manageable chunks.
    
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
    
    current_chunk = ""
    current_pages = []
    chunk_index = 0
    
    for page_key, page_data in sorted_pages:
        if not isinstance(page_data, dict) or 'text' not in page_data:
            continue
            
        page_text = page_data.get('text', '')
        page_num = page_data.get('page_number', 0)
        
        if not page_text.strip():
            continue
        
        # If adding this page would exceed chunk size, save current chunk
        if current_chunk and len(current_chunk) + len(page_text) > chunk_size:
            chunks.append({
                'chunk_index': chunk_index,
                'text': current_chunk.strip(),
                'pages': current_pages.copy(),
                'char_count': len(current_chunk),
                'start_page': min(current_pages) if current_pages else page_num,
                'end_page': max(current_pages) if current_pages else page_num
            })
            chunk_index += 1
            
            # Start new chunk with overlap from previous
            if overlap > 0 and current_chunk:
                # Take last 'overlap' characters for context
                overlap_text = current_chunk[-overlap:].strip()
                # Try to start at a sentence boundary
                sentences = overlap_text.split('. ')
                if len(sentences) > 1:
                    overlap_text = '. '.join(sentences[-2:]) + '.'
                current_chunk = overlap_text + "\n\n" + page_text
                # Keep last page in overlap
                current_pages = [current_pages[-1]] if current_pages else []
            else:
                current_chunk = page_text
                current_pages = []
            
            current_pages.append(page_num)
        else:
            # Add to current chunk
            if current_chunk:
                current_chunk += "\n\n" + page_text
            else:
                current_chunk = page_text
            current_pages.append(page_num)
    
    # Add final chunk
    if current_chunk.strip():
        chunks.append({
            'chunk_index': chunk_index,
            'text': current_chunk.strip(),
            'pages': current_pages,
            'char_count': len(current_chunk),
            'start_page': min(current_pages) if current_pages else 0,
            'end_page': max(current_pages) if current_pages else 0
        })
    
    return chunks


def _send_to_nhost(data, job_id, filename, user_id=None, jobs_dict=None, file_url=None, upload_device="web"):
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
    """
    if not NHOST_BACKEND_URL or not NHOST_ADMIN_SECRET:
        app.logger.warning("Nhost configuration missing, skipping Nhost integration")
        return None
    
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
        
        # Option 1: GraphQL mutation (recommended)
        graphql_url = f"{NHOST_BACKEND_URL}/v1/graphql"
        
        # Build mutation object matching Nhost table structure
        # Table: pdf_extractions
        # Columns: id (auto), created_at (auto), job_id, user_id, file_url, metadata, 
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
            "upload_device": upload_device  # Required: Device/platform from form upload
        }
        
        # Add user_id if provided
        if user_id:
            mutation_object["user_id"] = user_id
        
        graphql_mutation = {
            "query": """
                mutation InsertPDFExtraction($object: pdf_extractions_insert_input!) {
                    insert_pdf_extractions_one(object: $object) {
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
        
        response = requests.post(
            graphql_url,
            json=graphql_mutation,
            headers=headers,
            timeout=timeout
        )
        
        if response.status_code == 200:
            result = response.json()
            if 'errors' in result:
                app.logger.error(f"Nhost GraphQL errors: {result['errors']}")
                return None
            app.logger.info(f"Successfully sent data to Nhost for job {job_id}")
            return result.get('data', {})
        else:
            app.logger.error(f"Nhost request failed: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        app.logger.error(f"Error sending to Nhost: {str(e)}")
        return None


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
                              send_webhook=True, user_id=None, file_url=None, upload_device="web"):
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
    jobs[job_id] = {
        'status': 'processing', 
        'progress': 0,
        'stage': 'file_received',
        'message': 'File received, starting extraction...'
    }
    
    try:
        # Update: Reading file
        jobs[job_id] = {
            'status': 'processing',
            'progress': 5,
            'stage': 'reading',
            'message': 'Reading PDF file...'
        }
        
        # Process extraction using file path with progress updates
        result, filename = _process_pdf_extraction_from_path(
            file_path, original_filename, extract_type, pages, include_tables, jobs, job_id
        )
        
        if result is None:
            jobs[job_id] = {'status': 'failed', 'error': filename, 'stage': 'failed'}
            if send_webhook:
                _send_webhook(job_id, 'failed', error=filename)
            return
        
        # Update: Completed reading
        jobs[job_id] = {
            'status': 'processing',
            'progress': 50,
            'stage': 'reading_complete',
            'message': 'Completed reading PDF, extracting data...'
        }
        
        # Send to Nhost if enabled
        nhost_result = None
        if send_to_nhost:
            jobs[job_id] = {
                'status': 'processing',
                'progress': 60,
                'stage': 'sending_to_db',
                'message': 'Chunking text for embeddings...'
            }
            nhost_result = _send_to_nhost(result, job_id, filename, user_id, jobs, file_url=file_url, upload_device=upload_device)
            jobs[job_id] = {
                'status': 'processing',
                'progress': 90,
                'stage': 'sending_to_db',
                'message': 'Data sent to database successfully'
            }
        
        # Update job status - Done
        jobs[job_id] = {
            'status': 'completed',
            'progress': 100,
            'stage': 'done',
            'message': 'Processing complete!',
            'filename': filename,
            'data': result,
            'nhost_result': nhost_result
        }
        
        # Send webhook
        if send_webhook:
            _send_webhook(job_id, 'completed', data={
                'filename': filename,
                'extraction': result,
                'nhost_success': nhost_result is not None
            })
            
    except Exception as e:
        error_msg = str(e)
        jobs[job_id] = {'status': 'failed', 'error': error_msg}
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
            
            if jobs_dict and job_id:
                jobs_dict[job_id] = {
                    'status': 'processing',
                    'progress': 10,
                    'stage': 'reading',
                    'message': f'Reading PDF ({num_pages} pages)...'
                }
            
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
                
                if jobs_dict and job_id:
                    jobs_dict[job_id] = {
                        'status': 'processing',
                        'progress': 30,
                        'stage': 'reading',
                        'message': f'Extracted text from {num_pages} pages, processing tables...'
                    }
                
                # Tables can be very slow for large PDFs - extract conditionally
                if include_tables:
                    # For very large PDFs, warn that tables take time
                    if num_pages > 100:
                        if jobs_dict and job_id:
                            jobs_dict[job_id] = {
                                'status': 'processing',
                                'progress': 35,
                                'stage': 'reading',
                                'message': f'Extracting tables from {num_pages} pages (this may take a while)...'
                            }
                    result['tables'] = extractor.extract_tables(pages)
        
        if jobs_dict and job_id:
            jobs_dict[job_id] = {
                'status': 'processing',
                'progress': 50,
                'stage': 'reading_complete',
                'message': 'Completed reading PDF, preparing data...'
            }
        
        return result, filename
        
    except Exception as e:
        return None, str(e)
    finally:
        # Clean up temporary file
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass  # Ignore cleanup errors


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
    return jsonify({'status': 'healthy', 'service': 'PDF Extractor API'})


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
            nhost_result = _send_to_nhost(result, job_id, filename, user_id, jobs, file_url=file_url, upload_device=upload_device)
        
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
        except Exception as e:
            return jsonify({
                'success': False,
                'error': f'Failed to save file: {str(e)}'
            }), 500
        
        # Start async processing with file path instead of file object
        thread = threading.Thread(
            target=_process_extraction_async,
            args=(temp_path, filename, job_id, extract_type, pages, include_tables, send_to_nhost, send_webhook, user_id, file_url, upload_device)
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
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job = jobs[job_id]
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
