# Quick Start Guide

## 🚀 Deploy in 5 Minutes (Railway - Recommended)

1. **Push your code to GitHub**

2. **Sign up at [railway.app](https://railway.app)**

3. **Create new project → Deploy from GitHub**

4. **Add environment variables:**
   ```
   NHOST_BACKEND_URL=https://your-project.nhost.run
   NHOST_ADMIN_SECRET=your-admin-secret
   WEBHOOK_URL=https://your-nextjs-app.vercel.app/api/webhook/pdf-extraction
   CORS_ORIGINS=https://your-nextjs-app.vercel.app
   ```

5. **Deploy!** Your API is live at `https://your-app.railway.app`

## 📝 Next.js Integration

1. **Add to `.env.local`:**
   ```env
   NEXT_PUBLIC_PDF_API_URL=https://your-app.railway.app
   ```

2. **Create webhook handler** (see `NEXTJS_INTEGRATION.md`)

3. **Use the upload component** (see `NEXTJS_INTEGRATION.md`)

## 🔄 Workflow

```
User uploads PDF
    ↓
Next.js → PDF API (/extract/async)
    ↓
PDF API returns job_id immediately
    ↓
PDF API processes in background
    ↓
PDF API → Nhost (saves extracted data)
    ↓
PDF API → Next.js webhook (confirmation)
    ↓
Nhost generates embeddings
    ↓
Done! ✅
```

## 🧪 Test It

```bash
# Test health
curl https://your-app.railway.app/health

# Test extraction (async)
curl -X POST -F "file=@test.pdf" \
  -F "send_to_nhost=true" \
  https://your-app.railway.app/extract/async

# Check status
curl https://your-app.railway.app/job/<job_id>
```

## 📚 Full Documentation

- **Deployment**: See `DEPLOYMENT.md`
- **Next.js Integration**: See `NEXTJS_INTEGRATION.md`
- **API Usage**: See `README.md`

## 🔧 Nhost Setup

1. Create a table `pdf_embeddings` in Nhost
2. Set up GraphQL permissions
3. Configure webhook/function for embedding generation (optional)

See `NEXTJS_INTEGRATION.md` for SQL schema example.

