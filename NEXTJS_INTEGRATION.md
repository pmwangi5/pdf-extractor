# Next.js Integration Guide

This guide shows how to integrate the PDF Extractor API with your Next.js application and Nhost.

## Setup

### 1. Environment Variables

Add to your Next.js `.env.local`:

```env
NEXT_PUBLIC_PDF_API_URL=https://your-pdf-api.railway.app
# or http://your-ec2-ip:5000 for EC2

NHOST_BACKEND_URL=https://your-project.nhost.run
NHOST_ADMIN_SECRET=your-admin-secret
```

### 2. API Route: Webhook Handler

Create `pages/api/webhook/pdf-extraction.ts` (or `app/api/webhook/pdf-extraction/route.ts` for App Router):

**Pages Router:**
```typescript
// pages/api/webhook/pdf-extraction.ts
import type { NextApiRequest, NextApiResponse } from 'next';
import { nhost } from '@/lib/nhost'; // Your Nhost client setup

export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse
) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const { job_id, status, data, error } = req.body;

  try {
    if (status === 'completed' && data) {
      // Handle successful extraction
      // You can trigger embedding generation here
      // or the PDF API already sent it to Nhost
      
      console.log(`PDF extraction completed for job ${job_id}`);
      console.log('Filename:', data.filename);
      console.log('Nhost success:', data.nhost_success);
      
      // Optionally trigger additional processing
      // await triggerEmbeddingGeneration(job_id, data);
    } else if (status === 'failed') {
      console.error(`PDF extraction failed for job ${job_id}:`, error);
    }

    res.status(200).json({ received: true });
  } catch (err) {
    console.error('Webhook error:', err);
    res.status(500).json({ error: 'Webhook processing failed' });
  }
}
```

**App Router:**
```typescript
// app/api/webhook/pdf-extraction/route.ts
import { NextRequest, NextResponse } from 'next/server';
import { nhost } from '@/lib/nhost';

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { job_id, status, data, error } = body;

    if (status === 'completed' && data) {
      console.log(`PDF extraction completed for job ${job_id}`);
      // Handle successful extraction
    } else if (status === 'failed') {
      console.error(`PDF extraction failed:`, error);
    }

    return NextResponse.json({ received: true });
  } catch (err) {
    return NextResponse.json(
      { error: 'Webhook processing failed' },
      { status: 500 }
    );
  }
}
```

### 3. File Upload Component

Create a component for PDF upload:

```typescript
// components/PDFUpload.tsx
'use client';

import { useState } from 'react';
import { useUser } from '@nhost/nextjs';

export default function PDFUpload() {
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const user = useUser();

  const API_URL = process.env.NEXT_PUBLIC_PDF_API_URL || 'http://localhost:5000';

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setFile(e.target.files[0]);
    }
  };

  const handleUpload = async () => {
    if (!file) return;

    setUploading(true);
    setStatus('Uploading...');

    try {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('extract_type', 'all');
      formData.append('include_tables', 'true');
      formData.append('send_to_nhost', 'true');
      formData.append('send_webhook', 'true');
      
      if (user?.id) {
        formData.append('user_id', user.id);
      }

      // Use async endpoint for better UX
      const response = await fetch(`${API_URL}/extract/async`, {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        throw new Error('Upload failed');
      }

      const result = await response.json();
      setJobId(result.job_id);
      setStatus('Processing...');

      // Poll for status
      pollJobStatus(result.job_id);
    } catch (error) {
      console.error('Upload error:', error);
      setStatus('Upload failed');
      setUploading(false);
    }
  };

  const pollJobStatus = async (jobId: string) => {
    const maxAttempts = 60; // 5 minutes max (5s intervals)
    let attempts = 0;

    const interval = setInterval(async () => {
      attempts++;
      
      try {
        const response = await fetch(`${API_URL}/job/${jobId}`);
        const job = await response.json();

        if (job.status === 'completed') {
          setStatus('Completed! Data sent to Nhost for embeddings.');
          setUploading(false);
          clearInterval(interval);
          
          // Optionally refresh your data or show success message
          // You could also use the webhook instead of polling
        } else if (job.status === 'failed') {
          setStatus(`Failed: ${job.error}`);
          setUploading(false);
          clearInterval(interval);
        } else {
          setStatus(`Processing... (${job.progress || 0}%)`);
        }

        if (attempts >= maxAttempts) {
          setStatus('Processing is taking longer than expected. Check back later.');
          clearInterval(interval);
        }
      } catch (error) {
        console.error('Status check error:', error);
        clearInterval(interval);
        setStatus('Error checking status');
        setUploading(false);
      }
    }, 5000); // Check every 5 seconds
  };

  return (
    <div className="p-6 max-w-md mx-auto bg-white rounded-lg shadow-md">
      <h2 className="text-2xl font-bold mb-4">Upload PDF</h2>
      
      <div className="mb-4">
        <input
          type="file"
          accept=".pdf"
          onChange={handleFileChange}
          disabled={uploading}
          className="block w-full text-sm text-gray-500
            file:mr-4 file:py-2 file:px-4
            file:rounded-full file:border-0
            file:text-sm file:font-semibold
            file:bg-blue-50 file:text-blue-700
            hover:file:bg-blue-100"
        />
      </div>

      <button
        onClick={handleUpload}
        disabled={!file || uploading}
        className="w-full bg-blue-500 text-white py-2 px-4 rounded
          hover:bg-blue-600 disabled:bg-gray-300 disabled:cursor-not-allowed"
      >
        {uploading ? 'Processing...' : 'Upload & Extract'}
      </button>

      {status && (
        <div className="mt-4 p-3 bg-gray-100 rounded">
          <p className="text-sm">{status}</p>
          {jobId && (
            <p className="text-xs text-gray-500 mt-1">Job ID: {jobId}</p>
          )}
        </div>
      )}
    </div>
  );
}
```

### 4. Alternative: Using Webhooks (Recommended)

Instead of polling, use webhooks for better performance:

```typescript
// components/PDFUploadWebhook.tsx
'use client';

import { useState, useEffect } from 'react';
import { useUser } from '@nhost/nextjs';
import { useRouter } from 'next/navigation';

export default function PDFUploadWebhook() {
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const user = useUser();
  const router = useRouter();

  const API_URL = process.env.NEXT_PUBLIC_PDF_API_URL || 'http://localhost:5000';

  // Listen for webhook events (using Server-Sent Events or polling)
  useEffect(() => {
    if (!jobId) return;

    // Option 1: Use Server-Sent Events if your API supports it
    // Option 2: Poll briefly then rely on webhook
    // Option 3: Use a WebSocket connection
    
    // For now, we'll show a message and let the webhook handle the rest
    // The webhook can update your database, which triggers a UI refresh
  }, [jobId]);

  const handleUpload = async () => {
    if (!file) return;

    setUploading(true);

    try {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('extract_type', 'all');
      formData.append('send_to_nhost', 'true');
      formData.append('send_webhook', 'true');
      
      if (user?.id) {
        formData.append('user_id', user.id);
      }

      const response = await fetch(`${API_URL}/extract/async`, {
        method: 'POST',
        body: formData,
      });

      const result = await response.json();
      setJobId(result.job_id);
      
      // Show success message
      alert('PDF uploaded! Processing in background. You will be notified when complete.');
      
      // Redirect or refresh data
      router.refresh();
    } catch (error) {
      console.error('Upload error:', error);
      alert('Upload failed');
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="p-6">
      <input
        type="file"
        accept=".pdf"
        onChange={(e) => setFile(e.target.files?.[0] || null)}
        disabled={uploading}
      />
      <button
        onClick={handleUpload}
        disabled={!file || uploading}
      >
        {uploading ? 'Uploading...' : 'Upload PDF'}
      </button>
    </div>
  );
}
```

### 5. Nhost Schema Example

Your Nhost database should have a table for PDF extractions:

```sql
-- Create table for PDF extractions
CREATE TABLE pdf_extractions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id TEXT UNIQUE NOT NULL,
  user_id UUID REFERENCES auth.users(id),
  filename TEXT NOT NULL,
  metadata JSONB,
  text_content TEXT,
  text_by_page JSONB,
  tables JSONB,
  status TEXT DEFAULT 'processing',
  nhost_embedding_id UUID, -- Reference to embeddings table
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create index for faster queries
CREATE INDEX idx_pdf_extractions_user_id ON pdf_extractions(user_id);
CREATE INDEX idx_pdf_extractions_status ON pdf_extractions(status);

-- Create table for embeddings (if separate)
CREATE TABLE pdf_embeddings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  pdf_extraction_id UUID REFERENCES pdf_extractions(id),
  embedding VECTOR(1536), -- Adjust dimension based on your embedding model
  chunk_text TEXT,
  chunk_index INTEGER,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_pdf_embeddings_extraction ON pdf_embeddings(pdf_extraction_id);
```

### 6. Trigger Embedding Generation (Optional)

If you want to generate embeddings in Nhost after extraction:

```typescript
// lib/embeddings.ts
import { nhost } from './nhost';

export async function generateEmbeddings(pdfExtractionId: string) {
  // This would be a Nhost function or webhook
  // that processes the text_content and creates embeddings
  
  const { data, error } = await nhost.graphql.request(`
    mutation GenerateEmbeddings($pdfId: uuid!) {
      generate_pdf_embeddings(pdf_extraction_id: $pdfId) {
        success
        embedding_count
      }
    }
  `, {
    pdfId: pdfExtractionId
  });

  return { data, error };
}
```

## Usage Flow

1. User uploads PDF in Next.js app
2. Next.js sends file to PDF API `/extract/async` endpoint
3. PDF API returns `job_id` immediately
4. PDF API processes extraction in background
5. PDF API sends extracted data to Nhost
6. PDF API sends webhook to Next.js (optional)
7. Next.js can poll `/job/<job_id>` or rely on webhook
8. Nhost processes embeddings (via function/webhook)
9. UI updates when complete

## Testing

Test the integration:

```bash
# Test API directly
curl -X POST -F "file=@test.pdf" \
  -F "send_to_nhost=true" \
  -F "send_webhook=true" \
  https://your-api.railway.app/extract/async

# Check job status
curl https://your-api.railway.app/job/<job_id>
```

