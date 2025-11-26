# PDF Data Extractor

A Python application for extracting data from PDF files. Supports text extraction, metadata extraction, and table extraction with both CLI and web API interfaces.

## Features

- **Text Extraction**: Extract text content from PDF pages
- **Metadata Extraction**: Get PDF metadata (title, author, creation date, etc.)
- **Table Extraction**: Extract tables from PDF pages
- **CLI Interface**: Command-line tool for batch processing
- **Web API**: RESTful API for PDF processing via HTTP
- **Flexible Page Selection**: Extract data from specific pages or entire document

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
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
```bash
python api.py
```

The server will start on `http://0.0.0.0:5000` by default. You can set environment variables:
- `PORT`: Port number (default: 5000)
- `FLASK_DEBUG`: Enable debug mode (default: False)

#### API Endpoints

##### Health Check
```bash
GET /health
```

##### Extract All Data
```bash
POST /extract
Content-Type: multipart/form-data

Form data:
- file: PDF file
- extract_type: Optional. One of 'all', 'text', 'metadata', 'tables' (default: 'all')
- pages: Optional. Comma-separated page numbers (1-indexed)
- include_tables: Optional. 'true' or 'false' (default: 'true')
```

##### Extract Metadata Only
```bash
POST /extract/metadata
Content-Type: multipart/form-data

Form data:
- file: PDF file
- pages: Optional. Comma-separated page numbers
```

##### Extract Text Only
```bash
POST /extract/text
Content-Type: multipart/form-data

Form data:
- file: PDF file
- pages: Optional. Comma-separated page numbers
```

##### Extract Tables Only
```bash
POST /extract/tables
Content-Type: multipart/form-data

Form data:
- file: PDF file
- pages: Optional. Comma-separated page numbers
```

#### API Examples

Using curl:
```bash
# Extract all data
curl -X POST -F "file=@document.pdf" http://localhost:5000/extract

# Extract only text from pages 1-3
curl -X POST -F "file=@document.pdf" -F "pages=1,2,3" http://localhost:5000/extract/text

# Extract metadata
curl -X POST -F "file=@document.pdf" http://localhost:5000/extract/metadata
```

Using Python requests:
```python
import requests

url = "http://localhost:5000/extract"
files = {'file': open('document.pdf', 'rb')}
data = {'extract_type': 'all', 'include_tables': 'true'}

response = requests.post(url, files=files, data=data)
result = response.json()
print(result)
```

## Response Format

### CLI Output
The CLI outputs JSON with the following structure:

```json
{
  "metadata": {
    "title": "Document Title",
    "author": "Author Name",
    "num_pages": 10,
    "creation_date": "...",
    ...
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

### API Response
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

## Dependencies

- `pdfplumber`: Advanced PDF text and table extraction
- `PyPDF2`: PDF metadata and basic text extraction
- `flask`: Web framework for API
- `flask-cors`: CORS support for API
- `python-dotenv`: Environment variable management

## Limitations

- Maximum file size: 50MB (configurable in `api.py`)
- Table extraction can be slow for large PDFs
- Complex layouts may affect extraction accuracy
- Encrypted PDFs may not be fully extractable

## License

This project is open source and available for use.
