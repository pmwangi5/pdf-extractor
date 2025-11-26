# Database Structure for PDF Extractor

This document describes the database schema required for the PDF Extractor API integration with Nhost/Hasura.

**⚠️ IMPORTANT:** You MUST create this table structure in your Nhost/Hasura database before using the API. The API will fail if the table doesn't exist or has incorrect column types.

## Quick Setup

1. Go to your Nhost Dashboard → Database → SQL Editor
2. Run the migration script below (see "Migration Script" section)
3. Set up GraphQL permissions in Hasura Console
4. Verify the table appears in Hasura GraphQL schema

The API automatically sends data to this table when `send_to_nhost=true`.

## Table: `pdf_extractions`

Main table for storing PDF extraction data. This table stores all extracted content including text, metadata, tables, and chunked text for embeddings.

### SQL Schema

```sql
-- Create table for PDF extractions
CREATE TABLE pdf_extractions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  job_id TEXT UNIQUE NOT NULL,
  user_id UUID REFERENCES auth.users(id),
  file_url TEXT,
  metadata JSONB,
  text_content TEXT,
  text_by_page JSONB,
  text_chunks JSONB,
  chunk_count INTEGER DEFAULT 0,
  tables JSONB,
  status TEXT DEFAULT 'processing',
  nhost_embedding_id UUID,
  upload_device TEXT
);

-- Create indexes for faster queries
CREATE INDEX idx_pdf_extractions_user_id ON pdf_extractions(user_id);
CREATE INDEX idx_pdf_extractions_status ON pdf_extractions(status);
CREATE INDEX idx_pdf_extractions_job_id ON pdf_extractions(job_id);
CREATE INDEX idx_pdf_extractions_created_at ON pdf_extractions(created_at DESC);
```

### Column Descriptions

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | UUID | PRIMARY KEY, UNIQUE, DEFAULT: gen_random_uuid() | Primary key, auto-generated |
| `created_at` | TIMESTAMPTZ | DEFAULT: NOW() | Timestamp when record was created |
| `job_id` | TEXT | UNIQUE, NOT NULL | Unique job identifier from API (used for status tracking) |
| `user_id` | UUID | REFERENCES auth.users(id) | Foreign key to `auth.users(id)` - user who uploaded the PDF |
| `file_url` | TEXT | NULLABLE | URL to file if stored in S3/storage (optional) |
| `metadata` | JSONB | NULLABLE | PDF metadata (title, author, num_pages, creation_date, filename, etc.) |
| `text_content` | TEXT | NOT NULL | Full combined text from all pages (for reference/search) |
| `text_by_page` | JSONB | NOT NULL | Page-by-page text extraction with structure: `{"page_1": {"page_number": 1, "text": "...", "char_count": 1234}, ...}` |
| `text_chunks` | JSONB | NOT NULL | Array of text chunks for embeddings. Each chunk: `{"chunk_index": 0, "text": "...", "pages": [1,2,3], "start_page": 1, "end_page": 3, "char_count": 987}` |
| `chunk_count` | INTEGER | DEFAULT: 0 | Number of chunks created (for quick reference) |
| `tables` | JSONB | NOT NULL | Extracted tables by page: `{"page_1": {"page_number": 1, "num_tables": 2, "tables": [[...], [...]]}, ...}` |
| `status` | TEXT | DEFAULT: 'processing' | Current status: `'processing'`, `'ready_for_embedding'`, `'completed'`, `'failed'` |
| `nhost_embedding_id` | UUID | NULLABLE | Reference to embeddings table (if separate) |
| `upload_device` | TEXT | NOT NULL | Device/platform identifier (e.g., 'web', 'mobile', 'api') |

### JSONB Structure Examples

#### `metadata` Structure
```json
{
  "title": "Car Manual 2024",
  "author": "Manufacturer",
  "subject": "Vehicle Maintenance",
  "creator": "PDF Creator",
  "producer": "PDF Producer",
  "creation_date": "D:20240101120000Z",
  "modification_date": "D:20240101120000Z",
  "num_pages": 800,
  "is_encrypted": false,
  "filename": "car-manual-2024.pdf"
}
```

**Note:** The `filename` is stored in the `metadata` JSONB field, not as a separate column.

#### `text_by_page` Structure
```json
{
  "page_1": {
    "page_number": 1,
    "text": "Full text content from page 1...",
    "char_count": 1234
  },
  "page_2": {
    "page_number": 2,
    "text": "Full text content from page 2...",
    "char_count": 1567
  }
}
```

#### `text_chunks` Structure
```json
[
  {
    "chunk_index": 0,
    "text": "Chunk text content (up to 1000 chars)...",
    "pages": [1, 2],  // Array of page numbers this chunk covers
    "start_page": 1,  // First page in chunk
    "end_page": 2,    // Last page in chunk
    "char_count": 987 // Character count of chunk text
  },
  {
    "chunk_index": 1,
    "text": "Next chunk with overlap...",
    "pages": [2, 3],
    "start_page": 2,
    "end_page": 3,
    "char_count": 1023
  }
]
```

**Note:** The `pages` field is an array of integers stored as JSON in the JSONB column. This allows querying chunks by page range using Hasura's JSONB operators.

#### `tables` Structure
```json
{
  "page_5": {
    "page_number": 5,
    "num_tables": 2,
    "tables": [
      [
        ["Header1", "Header2", "Header3"],
        ["Row1Col1", "Row1Col2", "Row1Col3"],
        ["Row2Col1", "Row2Col2", "Row2Col3"]
      ],
      [
        ["Another", "Table", "Header"],
        ["Data", "Goes", "Here"]
      ]
    ]
  }
}
```

## Optional: Separate Embeddings Table

If you want to store embeddings separately (recommended for large-scale applications):

```sql
-- Create table for embeddings (optional, if storing separately)
CREATE TABLE pdf_embeddings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  pdf_extraction_id UUID REFERENCES pdf_extractions(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  embedding VECTOR(1536),  -- Adjust dimension based on your embedding model
  chunk_text TEXT,
  pages INTEGER[],  -- Array of page numbers this chunk covers
  start_page INTEGER,
  end_page INTEGER,
  char_count INTEGER,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create indexes
CREATE INDEX idx_pdf_embeddings_extraction ON pdf_embeddings(pdf_extraction_id);
CREATE INDEX idx_pdf_embeddings_chunk_index ON pdf_embeddings(pdf_extraction_id, chunk_index);

-- Enable vector similarity search (if using pgvector)
-- CREATE INDEX idx_pdf_embeddings_vector ON pdf_embeddings USING ivfflat (embedding vector_cosine_ops);
```

**Note:** Adjust the `VECTOR(1536)` dimension based on your embedding model:
- OpenAI `text-embedding-ada-002`: 1536
- OpenAI `text-embedding-3-small`: 1536
- OpenAI `text-embedding-3-large`: 3072
- Other models: Check model documentation

## GraphQL Permissions

Set up Hasura permissions to allow:

1. **Insert** (for API):
   - Role: `admin` or custom role with admin secret
   - Permission: Allow insert with all columns

2. **Select** (for users):
   - Role: `user`
   - Permission: Allow select where `user_id = X-Hasura-User-Id`

3. **Update** (optional, for status updates):
   - Role: `admin` or custom role
   - Permission: Allow update on specific columns (e.g., `status`, `updated_at`)

### Example Hasura Permission Rules

**For `user` role (Select):**
```json
{
  "filter": {
    "user_id": {
      "_eq": "X-Hasura-User-Id"
    }
  }
}
```

**For `admin` role (Insert/Update):**
- Allow all operations (used with admin secret header)

## GraphQL Mutation

The API uses this mutation to insert data:

```graphql
mutation InsertPDFExtraction($object: pdf_extractions_insert_input!) {
  insert_pdf_extractions_one(object: $object) {
    id
    job_id
    status
    chunk_count
    created_at
  }
}
```

**Variables Example:**
```json
{
  "object": {
    "job_id": "uuid-string",
    "user_id": "user-uuid",
    "file_url": null,
    "metadata": {
      "filename": "car-manual.pdf",
      "num_pages": 800,
      ...
    },
    "text_content": "Full text...",
    "text_by_page": {...},
    "text_chunks": [...],
    "chunk_count": 150,
    "tables": {...},
    "status": "ready_for_embedding",
    "upload_device": "web"
  }
}
```

## GraphQL Query Examples

### Get User's PDF Extractions
```graphql
query GetPdfExtractions($userId: uuid!) {
  pdf_extractions(
    where: { user_id: { _eq: $userId } },
    order_by: { created_at: desc }
  ) {
    id
    job_id
    status
    chunk_count
    metadata
    file_url
    upload_device
    created_at
  }
}
```

### Get Extractions Ready for Embedding
```graphql
query GetReadyExtractions($userId: uuid!) {
  pdf_extractions(
    where: { 
      user_id: { _eq: $userId },
      status: { _eq: "ready_for_embedding" }
    },
    order_by: { created_at: desc }
  ) {
    id
    chunk_count
    text_chunks
    metadata
    file_url
  }
}
```

### Get Specific Extraction with Chunks
```graphql
query GetExtractionWithChunks($extractionId: uuid!) {
  pdf_extractions_by_pk(id: $extractionId) {
    id
    job_id
    metadata
    text_chunks
    chunk_count
    text_by_page
    tables
    status
    file_url
    upload_device
    created_at
  }
}
```

### Get Filename from Metadata
```graphql
query GetExtractionsWithFilename($userId: uuid!) {
  pdf_extractions(
    where: { user_id: { _eq: $userId } }
  ) {
    id
    metadata
  }
}
```

Then extract filename from `metadata.filename` in your application.

## Status Values

The `status` field can have the following values:

- `processing`: Extraction is in progress (set by API during processing)
- `ready_for_embedding`: Extraction complete, ready for embedding generation
- `completed`: Fully processed including embeddings (if applicable)
- `failed`: Extraction failed

## Upload Device Values

The `upload_device` field can be:
- `web`: Uploaded via web interface (default)
- `mobile`: Uploaded via mobile app
- `api`: Uploaded via API directly
- Custom: Any string identifier for your use case

## Migration Script

Complete migration script for Nhost/Hasura:

```sql
-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Create pdf_extractions table
CREATE TABLE IF NOT EXISTS pdf_extractions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  job_id TEXT UNIQUE NOT NULL,
  user_id UUID REFERENCES auth.users(id),
  file_url TEXT,
  metadata JSONB,
  text_content TEXT,
  text_by_page JSONB,
  text_chunks JSONB,
  chunk_count INTEGER DEFAULT 0,
  tables JSONB,
  status TEXT DEFAULT 'processing',
  nhost_embedding_id UUID,
  upload_device TEXT
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_pdf_extractions_user_id ON pdf_extractions(user_id);
CREATE INDEX IF NOT EXISTS idx_pdf_extractions_status ON pdf_extractions(status);
CREATE INDEX IF NOT EXISTS idx_pdf_extractions_job_id ON pdf_extractions(job_id);
CREATE INDEX IF NOT EXISTS idx_pdf_extractions_created_at ON pdf_extractions(created_at DESC);
```

## Notes

1. **Filename Storage**: The filename is stored in `metadata.filename`, not as a separate column. Access it via `metadata->>'filename'` in SQL or `metadata.filename` in GraphQL.

2. **File URL**: The `file_url` field is optional and can be used if you store the original PDF file in S3 or similar storage. If provided, the API will include it in the mutation.

3. **JSONB Fields**: All JSONB fields can be queried using Hasura's JSONB operators

4. **Large Text**: `text_content` can be very large for 800+ page PDFs. PostgreSQL TEXT type supports unlimited size.

5. **Chunking**: `text_chunks` array is optimized for embedding generation - each chunk is ~1000 characters with 200 character overlap

6. **Indexing**: Indexes are created for common query patterns (user_id, status, job_id, created_at)

7. **Cascade Delete**: If using separate embeddings table, CASCADE ensures embeddings are deleted when extraction is deleted

8. **Upload Device**: Track where uploads come from for analytics/debugging
