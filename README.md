# PDF Data Extractor API

A production-ready Python application for extracting data from PDF files. Optimized for large documents (800+ pages) with intelligent text chunking for chatbot embeddings.

## Features

- **Text Extraction**: Extract text content from PDF pages with page-by-page organization
- **Metadata Extraction**: Get PDF metadata (title, author, creation date, page count, etc.)
- **Table Extraction**: Extract tables from PDF pages with structured data
- **CLI Interface**: Command-line tool for batch processing
- **Web API**: RESTful API with synchronous and asynchronous endpoints
- **Async Processing**: Background job processing with real-time progress tracking
- **Large PDF Support**: Optimized for 800+ page documents (car manuals, technical docs)
- **Text Chunking**: Advanced semantic chunking with text normalization (1000 chars/chunk with overlap)
- **Nhost/Hasura Integration**: Automatic data storage with GraphQL mutations
- **Webhook Support**: Notify Next.js apps when processing completes
- **Progress Tracking**: Real-time status updates with stages and percentages
- **Flexible Page Selection**: Extract data from specific pages or entire document
- **PDF File Validation**: Magic byte validation and integrity checks to prevent malicious uploads
- **DigitalOcean Spaces Integration**: Automatic PDF storage to S3-compatible storage
- **AWS SES Email Notifications**: Email alerts when embeddings are successfully created
- **Subscriber Management**: Automatic creation of subscriber entries for tracking
- **Redis Job Storage**: Persistent job storage with automatic expiration (production-ready)
- **Security Features**: Protection against malicious files, viruses, and dangerous content

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Set up environment variables (create `.env` file):
```env
PORT=5000
FLASK_DEBUG=False

# Nhost Configuration
NHOST_BACKEND_URL=https://your-project.nhost.run
NHOST_ADMIN_SECRET=your-admin-secret
NHOST_GRAPHQL_URL=https://your-project.nhost.run/v1/graphql  # Optional: Override default

# Webhook Configuration
WEBHOOK_URL=https://your-nextjs-app.com/api/webhook/pdf-extraction

# CORS Configuration
CORS_ORIGINS=https://your-app.com,https://app.vercel.app

# DigitalOcean Spaces (S3-compatible) Configuration
DO_SPACES_URL=nyc3.digitaloceanspaces.com  # or full URL: https://nyc3.digitaloceanspaces.com
DO_SPACES_ID=your-spaces-access-key-id
DO_SPACES_SECRET=your-spaces-secret-access-key
DO_SPACES_BUCKET=your-bucket-name

# AWS SES Email Configuration
AWS_SES_REGION=us-east-1
AWS_ACCESS_KEY_ID=your-aws-access-key-id
AWS_SECRET_ACCESS_KEY=your-aws-secret-access-key
AWS_SES_FROM_EMAIL=noreply@yourdomain.com  # Must be verified in SES
AWS_SES_TO_EMAIL=admin@yourdomain.com  # Where notifications are sent

# Redis Configuration (for job storage - production)
REDIS_URL=redis://default:password@host:port  # Automatically provided by Railway
REDIS_JOB_TTL=86400  # Optional: 24 hours (default for processing jobs)
REDIS_JOB_TTL_COMPLETED=3600  # Optional: 1 hour (default for completed jobs)
REDIS_JOB_TTL_FAILED=86400  # Optional: 24 hours (default for failed jobs)
```

## Usage

### Command Line Interface

#### Basic Usage
```bash
# Extract all data from a PDF
python cli.py document.pdf

# Extract only text
python cli.py document.pdf --text-only

# Extract only metadata
python cli.py document.pdf --metadata-only

# Extract only tables
python cli.py document.pdf --tables-only

# Extract specific pages (1-indexed)
python cli.py document.pdf --pages 1 2 3

# Skip table extraction for faster processing
python cli.py document.pdf --no-tables

# Save output to file
python cli.py document.pdf --output results.json

# Pretty print JSON output
python cli.py document.pdf --pretty
```

#### CLI Options
- `pdf_file`: Path to the PDF file (required)
- `--text-only`: Extract only text content
- `--metadata-only`: Extract only metadata
- `--tables-only`: Extract only tables
- `--no-tables`: Skip table extraction
- `--pages`: Specify page numbers to extract (1-indexed)
- `--output`, `-o`: Output file path (JSON format)
- `--pretty`: Pretty print JSON output

### Web API

#### Start the Server

**Development:**
```bash
python api.py
```

**Production (with Gunicorn):**
```bash
gunicorn api:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
```

The server will start on `http://0.0.0.0:5000` by default.

#### Environment Variables

| Variable | Description | Required | Default |
|----------|-------------|----------|---------|
| `PORT` | Port number | No | 5000 |
| `FLASK_DEBUG` | Enable debug mode | No | False |
| `NHOST_BACKEND_URL` | Nhost backend URL | Yes* | - |
| `NHOST_ADMIN_SECRET` | Nhost admin secret | Yes* | - |
| `NHOST_GRAPHQL_URL` | Nhost GraphQL endpoint (optional override) | No | Auto-detected |
| `WEBHOOK_URL` | Next.js webhook endpoint | No | - |
| `CORS_ORIGINS` | Comma-separated allowed origins | No | * (all) |
| `DO_SPACES_URL` | DigitalOcean Spaces endpoint | No | - |
| `DO_SPACES_ID` | DigitalOcean Spaces access key ID | No | - |
| `DO_SPACES_SECRET` | DigitalOcean Spaces secret key | No | - |
| `DO_SPACES_BUCKET` | DigitalOcean Spaces bucket name | No | - |
| `AWS_SES_REGION` | AWS SES region | No | us-east-1 |
| `AWS_ACCESS_KEY_ID` | AWS access key ID | No | - |
| `AWS_SECRET_ACCESS_KEY` | AWS secret access key | No | - |
| `AWS_SES_FROM_EMAIL` | Verified sender email in SES | No | - |
| `AWS_SES_TO_EMAIL` | Recipient email for notifications | No | - |

*Required if using Nhost integration

#### API Endpoints

##### Health Check
```bash
GET /health
```

Returns:
```json
{
  "status": "healthy",
  "service": "PDF Extractor API"
}
```

##### Extract All Data (Synchronous)
```bash
POST /extract
Content-Type: multipart/form-data
```

**⚠️ Warning:** This endpoint blocks until completion. Use `/extract/async` for large PDFs.

**Form data:**
- `file`: PDF file (required, max 200MB)
- `extract_type`: Optional. One of 'all', 'text', 'metadata', 'tables' (default: 'all')
- `pages`: Optional. Comma-separated page numbers (1-indexed)
- `include_tables`: Optional. 'true' or 'false' (default: 'true')
- `send_to_nhost`: Optional. 'true' or 'false' (default: 'false')
- `user_id`: Optional. User ID (UUID format)
- `user_display_name`: Optional. User display name for email notifications
- `upload_device`: Optional. Device/platform identifier (default: 'web')

**Response:**
```json
{
  "success": true,
  "filename": "document.pdf",
  "data": {
    "metadata": {...},
    "text": {...},
    "tables": {...}
  }
}
```

##### Extract All Data (Asynchronous) - **RECOMMENDED**
```bash
POST /extract/async
Content-Type: multipart/form-data
```

**✅ Recommended for production**, especially for large PDFs (800+ pages).

**Form data:**
- `file`: PDF file (required, max 200MB)
- `extract_type`: Optional. One of 'all', 'text', 'metadata', 'tables' (default: 'all')
- `pages`: Optional. Comma-separated page numbers (1-indexed)
- `include_tables`: Optional. 'true' or 'false' (default: 'true')
- `send_to_nhost`: Optional. 'true' or 'false' (default: 'true')
- `send_webhook`: Optional. 'true' or 'false' (default: 'true')
- `user_id`: Optional. User ID from Next.js (UUID format)
- `user_display_name`: Optional. User display name for email notifications
- `upload_device`: Optional. Device/platform identifier (default: 'web')
- `file_url`: Optional. URL if file is already stored in S3/storage

**Response (202 Accepted):**
```json
{
  "success": true,
  "job_id": "uuid-string",
  "status": "processing",
  "message": "Extraction started. Use /job/<job_id> to check status."
}
```

##### Check Job Status
```bash
GET /job/<job_id>
```

**Response (Processing):**
```json
{
  "job_id": "uuid",
  "status": "processing",
  "progress": 45,
  "stage": "reading",
  "message": "Reading PDF (800 pages)..."
}
```

**Response (Completed):**
```json
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
```

**Response (Failed):**
```json
{
  "job_id": "uuid",
  "status": "failed",
  "stage": "failed",
  "error": "Error message"
}
```

**Job Stages:**
- `file_received`: File uploaded and saved
- `reading`: Extracting text/metadata from PDF
- `reading_complete`: Text extraction finished
- `sending_to_db`: Chunking text and sending to database
- `done`: Processing complete
- `failed`: Error occurred

##### Extract Metadata Only
```bash
POST /extract/metadata
Content-Type: multipart/form-data
```

**Form data:**
- `file`: PDF file
- `pages`: Optional. Comma-separated page numbers

##### Extract Text Only
```bash
POST /extract/text
Content-Type: multipart/form-data
```

**Form data:**
- `file`: PDF file
- `pages`: Optional. Comma-separated page numbers

##### Extract Tables Only
```bash
POST /extract/tables
Content-Type: multipart/form-data
```

**Form data:**
- `file`: PDF file
- `pages`: Optional. Comma-separated page numbers

#### API Examples

**Using curl:**
```bash
# Health check
curl https://your-api.railway.app/health

# Extract all data (async - recommended)
curl -X POST -F "file=@manual.pdf" \
  -F "send_to_nhost=true" \
  -F "send_webhook=true" \
  -F "user_id=user-uuid-here" \
  -F "user_display_name=John Doe" \
  https://your-api.railway.app/extract/async

# Check job status
curl https://your-api.railway.app/job/<job_id>

# Extract all data (synchronous - for small PDFs)
curl -X POST -F "file=@document.pdf" \
  https://your-api.railway.app/extract

# Extract only text from pages 1-3
curl -X POST -F "file=@document.pdf" \
  -F "pages=1,2,3" \
  https://your-api.railway.app/extract/text

# Extract metadata
curl -X POST -F "file=@document.pdf" \
  https://your-api.railway.app/extract/metadata
```

**Using Python requests:**
```python
import requests
import time

# Async extraction (recommended)
url = "https://your-api.railway.app/extract/async"
files = {'file': open('manual.pdf', 'rb')}
data = {
    'extract_type': 'all',
    'include_tables': 'true',
    'send_to_nhost': 'true',
    'send_webhook': 'true',
    'user_id': 'user-uuid-here',
    'user_display_name': 'John Doe',
    'upload_device': 'web'
}

response = requests.post(url, files=files, data=data)
result = response.json()
job_id = result['job_id']

# Poll for status
status_url = f"https://your-api.railway.app/job/{job_id}"
while True:
    status = requests.get(status_url).json()
    if status['status'] == 'completed':
        print("Done!", status['data'])
        break
    elif status['status'] == 'failed':
        print("Failed:", status['error'])
        break
    print(f"Progress: {status['progress']}% - {status['message']}")
    time.sleep(2)
```

## Response Format

### CLI Output
The CLI outputs JSON with the following structure:

```json
{
  "metadata": {
    "title": "Document Title",
    "author": "Author Name",
    "num_pages": 800,
    "creation_date": "...",
    "is_encrypted": false
  },
  "text": {
    "page_1": {
      "page_number": 1,
      "text": "Extracted text content...",
      "char_count": 1234
    },
    ...
  },
  "tables": {
    "page_1": {
      "page_number": 1,
      "num_tables": 2,
      "tables": [[...], [...]]
    },
    ...
  }
}
```

### API Response (Synchronous)
```json
{
  "success": true,
  "filename": "document.pdf",
  "data": {
    "metadata": {...},
    "text": {...},
    "tables": {...}
  },
  "nhost_result": {...}
}
```

### API Response (Asynchronous - Initial)
```json
{
  "success": true,
  "job_id": "uuid-here",
  "status": "processing",
  "message": "Extraction started. Use /job/<job_id> to check status."
}
```

### API Response (Job Status)
```json
{
  "job_id": "uuid-here",
  "status": "completed",
  "progress": 100,
  "stage": "done",
  "message": "Processing complete!",
  "filename": "manual.pdf",
  "data": {
    "metadata": {...},
    "text": {...},
    "tables": {...}
  },
  "nhost_result": {
    "insert_pdf_embeddings_one": {
      "id": "uuid",
      "job_id": "uuid",
      "status": "ready_for_embedding"
    }
  }
}
```

## Text Chunking for Embeddings

For large PDFs (800+ pages), the API automatically chunks text intelligently for embedding generation with advanced semantic splitting and text normalization:

### Chunking Strategy

- **Chunk Size**: 1000 characters per chunk (optimal for most embedding models)
- **Overlap**: 200 characters between chunks (maintains context across boundaries)
- **Semantic Splitting**: Text is split into semantic units (paragraphs, bullet points, sentences)
- **Single Idea Per Chunk**: Each chunk ideally contains a single idea or concept
- **Page Tracking**: Each chunk includes accurate page range metadata

### Text Normalization

The chunking process includes comprehensive text normalization:

- **Line Break Normalization**: Multiple newlines converted to paragraph breaks
- **Hyphenation Removal**: Removes hyphenation artifacts (e.g., `word-\nword` → `word word`)
- **Bullet Point Standardization**: All bullet styles (`-`, `*`, `•`, `o`) normalized to consistent `•` format
- **Whitespace Cleanup**: Multiple spaces normalized to single spaces (preserves paragraph structure)
- **Trailing Whitespace**: Removed while maintaining document structure

### Semantic Unit Splitting

Text is intelligently split into semantic units before chunking:

1. **Paragraphs**: Split by double newlines (paragraph breaks)
2. **Bullet Lists**: Each bullet point becomes its own unit
3. **Numbered Lists**: Each numbered item becomes its own unit
4. **Long Paragraphs**: If a paragraph exceeds 800 characters, it's split by sentence boundaries
5. **Regular Paragraphs**: Kept as single units if reasonably sized

This ensures that:
- Goals, barriers, and pathways are split individually (not in one 3,000-char chunk)
- Each bullet point is a separate semantic unit
- Long paragraphs are broken into smaller, focused chunks
- Context is preserved through intelligent overlap

**Chunk Structure:**
```json
{
  "chunk_index": 0,
  "text": "Normalized chunk text here...",
  "pages": [1, 2, 3],
  "start_page": 1,
  "end_page": 3,
  "char_count": 987
}
```

The chunked text is stored in the `text_chunks` field in the database, ready for embedding generation. Each chunk is optimized for semantic search and AI-powered question answering.

## Large PDF Support

The API is optimized for large PDFs (800+ pages, car manuals, technical documents):

- **File Size Limit**: 200MB (configurable)
- **Progress Tracking**: Real-time updates during extraction
- **Memory Efficient**: Processes pages incrementally
- **Table Extraction**: Optional (can be slow for large PDFs)
- **Dynamic Timeouts**: Adjusts based on document size
- **Background Processing**: Async endpoint prevents timeouts

## PDF File Validation

The API includes robust PDF file validation to prevent malicious uploads:

- **Magic Byte Check**: Validates PDF file signature (`%PDF`)
- **Integrity Check**: Uses PyPDF2 to verify PDF structure
- **File Type Validation**: Only allows `.pdf` extensions
- **File Size Limits**: Maximum 200MB per file

Invalid or corrupted PDFs are rejected before processing begins.

## Nhost/Hasura Integration

The API automatically sends extracted data to Nhost/Hasura when `send_to_nhost=true`:

**Database Schema:**
See `DB_STRUCTURE.md` for complete database schema details.

The API stores data in the `pdf_embeddings` table with the following key fields:
- `id`: UUID primary key
- `job_id`: Unique job identifier
- `user_id`: User who uploaded the PDF
- `metadata`: JSONB containing PDF metadata (filename, page count, etc.)
- `text_content`: Full extracted text
- `text_by_page`: JSONB with page-by-page text
- `text_chunks`: JSONB with intelligently chunked text for embeddings
- `chunk_count`: Number of text chunks
- `tables`: JSONB with extracted tables
- `status`: Processing status
- `file_url`: URL to PDF stored in DigitalOcean Spaces
- `upload_device`: Device/platform used for upload

**GraphQL Mutation:**
The API uses the following mutation:
```graphql
mutation InsertPDFEmbedding($object: pdf_embeddings_insert_input!) {
  insert_pdf_embeddings_one(object: $object) {
    id
    job_id
    status
  }
}
```

**Subscriber Management:**
After successful embedding creation, the API automatically creates an entry in the `pdf_embeddings_subscribers` table to track user subscriptions to embeddings.

## DigitalOcean Spaces Integration

When configured, the API automatically uploads processed PDFs to DigitalOcean Spaces (S3-compatible storage):

- **Storage Location**: `docs_pdf_embedding_sources/{pdf_embedding_id}/{filename}`
- **File Organization**: Each PDF is stored in a folder named after its embedding ID
- **Database Update**: The `file_url` field is automatically updated after successful upload
- **Private Storage**: Files are stored with private ACL (configurable)

**Configuration:**
Set the following environment variables:
- `DO_SPACES_URL`: Spaces endpoint (e.g., `nyc3.digitaloceanspaces.com`)
- `DO_SPACES_ID`: Access key ID
- `DO_SPACES_SECRET`: Secret access key
- `DO_SPACES_BUCKET`: Bucket name

## AWS SES Email Notifications

The API can send email notifications via AWS SES when embeddings are successfully created:

- **Trigger**: Sent after successful Nhost insertion and Spaces upload
- **Content**: Includes filename, user ID, user display name, and PDF embedding ID
- **Format**: Both plain text and HTML versions
- **Error Handling**: Gracefully handles missing configuration or SES errors

**Email Content:**
- **Subject**: "PDF Embedding Created: {filename}"
- **Body**: File name, User ID, User Display Name, PDF Embedding ID

**Configuration:**
Set the following environment variables:
- `AWS_SES_REGION`: AWS region (default: `us-east-1`)
- `AWS_ACCESS_KEY_ID`: AWS access key
- `AWS_SECRET_ACCESS_KEY`: AWS secret key
- `AWS_SES_FROM_EMAIL`: Verified sender email in SES
- `AWS_SES_TO_EMAIL`: Recipient email address

**Note**: The sender email must be verified in AWS SES before use.

## Webhook Integration

When `send_webhook=true`, the API sends a POST request to `WEBHOOK_URL` when processing completes:

**Webhook Payload (Success):**
```json
{
  "job_id": "uuid",
  "status": "completed",
  "data": {
    "filename": "manual.pdf",
    "extraction": {...},
    "nhost_success": true
  }
}
```

**Webhook Payload (Failure):**
```json
{
  "job_id": "uuid",
  "status": "failed",
  "error": "Error message"
}
```

## Dependencies

- `pdfplumber>=0.10.0`: Advanced PDF text and table extraction
- `PyPDF2>=3.0.0`: PDF metadata and basic text extraction, file validation
- `flask>=3.0.0`: Web framework for API
- `flask-cors>=4.0.0`: CORS support for API
- `python-dotenv>=1.0.0`: Environment variable management
- `gunicorn>=21.2.0`: Production WSGI server (for deployment)
- `requests>=2.31.0`: HTTP library for Nhost/webhook integration
- `boto3>=1.34.0`: AWS SDK for SES and S3-compatible storage
- `pdf2image>=1.16.0`: PDF to image conversion (requires Poppler)
- `redis>=5.0.0`: Redis client for job storage (production)

## Deployment

See `DEPLOYMENT.md` for detailed deployment instructions for:
- Railway (recommended)
- AWS EC2
- Render
- Other platforms

### Railway Deployment

1. Connect your GitHub repository to Railway
2. Set environment variables in Railway dashboard
3. Railway will automatically detect the `Procfile` and deploy

**Required Environment Variables for Railway:**
- `NHOST_BACKEND_URL`
- `NHOST_ADMIN_SECRET`
- `DO_SPACES_URL` (if using Spaces)
- `DO_SPACES_ID` (if using Spaces)
- `DO_SPACES_SECRET` (if using Spaces)
- `DO_SPACES_BUCKET` (if using Spaces)
- `AWS_SES_REGION` (if using email notifications)
- `AWS_ACCESS_KEY_ID` (if using email notifications)
- `AWS_SECRET_ACCESS_KEY` (if using email notifications)
- `AWS_SES_FROM_EMAIL` (if using email notifications)
- `AWS_SES_TO_EMAIL` (if using email notifications)
- `REDIS_URL` (automatically provided by Railway when Redis service is added)

## Limitations

- **Maximum file size**: 200MB (configurable in `api.py`)
- **Maximum pages**: 10,000 pages per PDF (DoS protection)
- **Maximum chunks**: 10,000 chunks per PDF (resource protection)
- **Table extraction**: Can be slow for large PDFs (>100 pages)
- **Complex layouts**: May affect extraction accuracy
- **Encrypted PDFs**: May not be fully extractable
- **Job storage**: Falls back to in-memory if Redis unavailable (not recommended for production)

## Production Considerations

1. **Job Storage**: Redis is implemented and automatically used when `REDIS_URL` is set. Jobs automatically expire:
   - Processing jobs: 24 hours
   - Completed jobs: 1 hour (automatically deleted)
   - Failed jobs: 24 hours
2. **File Storage**: DigitalOcean Spaces integration is already implemented
3. **Monitoring**: Add logging and monitoring for production
4. **Rate Limiting**: Implement rate limiting for API endpoints
5. **Error Handling**: Retry logic is implemented for Nhost/webhook calls
6. **Scaling**: Use multiple workers for concurrent processing (see `HOW_TO_PRODUCTION.md`)
7. **Email Verification**: Ensure sender email is verified in AWS SES
8. **CORS**: Configure `CORS_ORIGINS` appropriately for production
9. **Redis Setup**: Add Redis service in Railway (automatically provides `REDIS_URL`)
10. **Security**: Security features are enabled by default (see Security Features section)

## Security Features

### File Validation
- **File Type Validation**: Only PDF files are accepted (`.pdf` extension)
- **Magic Byte Validation**: Verifies actual PDF file structure (checks `%PDF` header)
- **File Size Limits**: 
  - Minimum: 100 bytes (prevents empty/minimal files)
  - Maximum: 200MB (prevents DoS attacks via large files)
- **PDF Structure Validation**: Validates PDF integrity and detects malformed files
- **Page Count Limits**: Maximum 10,000 pages per PDF (DoS protection)
- **Secure Filenames**: Uses `secure_filename()` to prevent path traversal

### Content Security
- **PDF Structure Analysis**: Detects embedded JavaScript and embedded files (potential security risks)
- **Text Sanitization**: Removes null bytes, control characters, and excessive whitespace
- **Dangerous Content Detection**: Detects SQL injection, script injection, and command injection patterns
- **Chunk Limits**: Maximum 10,000 chunks per PDF and 2,000 characters per chunk (resource protection)
- **Text Truncation**: Automatically truncates text exceeding safe limits

### Infrastructure Security
- **CORS Configuration**: Configurable CORS origins
- **Environment Variables**: Sensitive data stored in environment variables
- **Redis Security**: Jobs automatically expire to prevent data accumulation
- **Error Handling**: Secure error messages that don't leak sensitive information

### Security Warnings
The system logs warnings for:
- PDFs containing JavaScript (potential XSS risk)
- PDFs containing embedded files (potential malware)
- Dangerous content patterns in extracted text
- Files exceeding recommended limits

**Note**: By default, the system logs warnings but continues processing (fail-open policy). You can modify the code to reject files with security warnings if you prefer a stricter fail-closed policy.

## License

This project is open source and available for use.
