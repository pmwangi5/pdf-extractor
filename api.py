#!/usr/bin/env python3
"""
Web API for PDF data extraction.
Provides REST endpoints to upload and extract data from PDF files.
Integrates with Nhost for embeddings storage.
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
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
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


def _send_to_nhost(data, job_id, filename, user_id=None):
    """
    Send extracted data to Nhost for embeddings.
    
    Args:
        data: Extracted PDF data
        job_id: Job identifier
        filename: Original filename
        user_id: Optional user ID
    """
    if not NHOST_BACKEND_URL or not NHOST_ADMIN_SECRET:
        app.logger.warning("Nhost configuration missing, skipping Nhost integration")
        return None
    
    try:
        # Prepare data for Nhost
        # Combine all text from pages for embedding
        combined_text = ""
        if 'text' in data:
            for page_key, page_data in data['text'].items():
                if isinstance(page_data, dict) and 'text' in page_data:
                    combined_text += page_data['text'] + "\n\n"
        
        # Prepare payload for Nhost
        # Adjust this based on your Nhost schema
        payload = {
            'job_id': job_id,
            'filename': filename,
            'metadata': data.get('metadata', {}),
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
        
        # Build mutation object
        mutation_object = {
            "job_id": job_id,
            "filename": filename,
            "metadata": payload['metadata'],
            "text_content": combined_text,
            "text_by_page": payload['text_by_page'],
            "tables": payload['tables'],
            "status": "ready_for_embedding"
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
                        filename
                        status
                    }
                }
            """,
            "variables": {
                "object": mutation_object
            }
        }
        
        response = requests.post(
            graphql_url,
            json=graphql_mutation,
            headers=headers,
            timeout=30
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
                              send_webhook=True, user_id=None):
    """
    Process PDF extraction asynchronously.
    
    Args:
        file_path: Path to saved file
        original_filename: Original filename
        job_id: Unique job identifier
        extract_type: Type of extraction
        pages: List of page numbers
        include_tables: Whether to include tables
        send_to_nhost: Whether to send to Nhost
        send_webhook: Whether to send webhook
        user_id: Optional user ID from Next.js
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
            'progress': 10,
            'stage': 'reading',
            'message': 'Reading PDF file...'
        }
        
        # Process extraction using file path
        result, filename = _process_pdf_extraction_from_path(file_path, original_filename, extract_type, pages, include_tables)
        
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
                'message': 'Sending data to database...'
            }
            nhost_result = _send_to_nhost(result, job_id, filename, user_id)
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


def _process_pdf_extraction_from_path(file_path, filename, extract_type='all', pages=None, include_tables=True):
    """
    Internal function to process PDF extraction from file path.
    
    Args:
        file_path: Path to PDF file
        filename: Original filename
        extract_type: Type of extraction ('all', 'text', 'metadata', 'tables')
        pages: List of page numbers (0-indexed) or None for all pages
        include_tables: Whether to include tables in extraction
    
    Returns:
        Tuple of (result_dict, filename) or (None, error_message)
    """
    try:
        # Extract data
        with PDFExtractor(file_path) as extractor:
            if extract_type == 'metadata':
                result = {'metadata': extractor.extract_metadata()}
            elif extract_type == 'text':
                result = {'text': extractor.extract_text(pages)}
            elif extract_type == 'tables':
                result = {'tables': extractor.extract_tables(pages)}
            else:  # 'all'
                # Extract all data, passing pages directly for efficiency
                result = {
                    'metadata': extractor.extract_metadata(),
                    'text': extractor.extract_text(pages),
                }
                if include_tables:
                    result['tables'] = extractor.extract_tables(pages)
        
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
    
    Expected form data:
    - file: PDF file to process
    - extract_type: Optional. One of 'all', 'text', 'metadata', 'tables'
    - pages: Optional. Comma-separated page numbers (1-indexed)
    - include_tables: Optional. Boolean string 'true'/'false'
    - send_to_nhost: Optional. Boolean string 'true'/'false' (default: 'false')
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
            nhost_result = _send_to_nhost(result, job_id, filename, user_id)
        
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
    Returns immediately with a job_id for status checking.
    Automatically sends to Nhost and webhook when complete.
    
    Expected form data:
    - file: PDF file to process
    - extract_type: Optional. One of 'all', 'text', 'metadata', 'tables'
    - pages: Optional. Comma-separated page numbers (1-indexed)
    - include_tables: Optional. Boolean string 'true'/'false'
    - send_to_nhost: Optional. Boolean string 'true'/'false' (default: 'true')
    - send_webhook: Optional. Boolean string 'true'/'false' (default: 'true')
    - user_id: Optional. User ID from Next.js
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
            args=(temp_path, filename, job_id, extract_type, pages, include_tables, send_to_nhost, send_webhook, user_id)
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
    
    Returns:
        - status: 'processing', 'completed', or 'failed'
        - progress: 0-100 (for processing)
        - data: Extracted data (if completed)
        - error: Error message (if failed)
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
    return jsonify({'error': 'File too large. Maximum size is 50MB'}), 413


@app.errorhandler(500)
def internal_error(error):
    """Handle internal server errors."""
    return jsonify({'error': 'Internal server error'}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
