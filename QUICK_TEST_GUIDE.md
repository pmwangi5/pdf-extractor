# Quick Test Guide - Improved PDF Extraction

## What Changed?

### 1. **Better Context in Embeddings**
- Chunks are now **50% larger** (1500 vs 1000 chars)
- Overlap is **100% bigger** (400 vs 200 chars)
- Smarter boundary detection preserves sentences

### 2. **Automatic Title Inference**
- Extracts title from first page when metadata is empty
- Your "Emissions Gap Report 2025" will now have a proper title

### 3. **Enhanced Text Extraction**
- Better layout detection for tables and columns
- Improved handling of headers and section markers
- Reduced text artifacts

---

## Test the Changes

### Option 1: Quick Test with Test Script

```bash
cd /Users/patrick/DEV/OUTBOX/GCS/2025/serverPython/pdf-extractor

# Test title inference only (fast)
python test_title_inference.py profile.pdf

# Or test with your Emissions Gap Report
python test_title_inference.py path/to/EGR2025.pdf
```

**Expected Output:**
```
==============================================================
Testing Title Inference for: profile.pdf
==============================================================

📄 PDF Metadata:
   Title: ''
   Author: ''
   Pages: 76

📖 First Page Text (first 500 chars):
   Off target
   Continued collective inaction puts
   global temperature goal at risk
   Emissions Gap Report 2025...

✨ Results:
   Original Title: '(empty)'
   Inferred Title: 'Off target Continued collective inaction puts global temperature goal at risk Emissions Gap Report 2025'

✅ Title will be auto-populated in GraphQL!
```

---

### Option 2: Full Extraction Test with API

```bash
# Start the API server
python api.py

# In another terminal, upload a test PDF
curl -X POST -F "file=@profile.pdf" \
     -F "send_to_nhost=true" \
     -F "user_id=your-user-id-here" \
     -F "upload_device=web" \
     http://localhost:5000/extract/async

# Response will include job_id:
# {"success": true, "job_id": "abc-123-...", "status": "processing"}

# Check status
curl http://localhost:5000/job/abc-123-...

# When complete, check your GraphQL database
# The pdf_embeddings record should show:
# - metadata.title: "Off target Continued..." (auto-inferred!)
# - chunk_count: ~230 (fewer, but richer chunks)
# - text_chunks: Each chunk has 1500 chars with 400 char overlap
```

---

## Verify Improvements in Database

After processing, check your `pdf_embeddings` table:

```graphql
query CheckPDFEmbedding($id: uuid!) {
  pdf_embeddings_by_pk(id: $id) {
    metadata
    chunk_count
    text_chunks
    text_by_page
  }
}
```

**What to Look For:**

1. **`metadata.title`** should be populated (even if PDF metadata was empty)
2. **`chunk_count`** should be ~30% lower than before (fewer, richer chunks)
3. **`text_chunks[0].char_count`** should average ~1500 (vs ~1000 before)
4. **`text_chunks[n].text`** should have better context and fewer mid-sentence breaks

---

## Compare Before/After

### Before (Old Settings):
```json
{
  "chunk_index": 42,
  "text": "...global emissions reach their peak between 2020 and 2025, noting",
  "char_count": 998,
  "pages": [45, 46]
}
```

### After (New Settings):
```json
{
  "chunk_index": 28,
  "text": "The outcome of the first global stocktake encourages parties to align their NDCs with 1.5°C, \"as informed by the latest science, in the light of different national circumstances\" (UNFCCC 2023). It also notes the importance of aligning NDCs with long-term, low emissions development strategies, which in turn are to be \"towards just transitions to net-zero emissions.\" The global stocktake recognizes that in scenarios limiting warming to 1.5°C (>50 per cent), global emissions reach their peak between 2020 and 2025, noting that \"this does not imply peaking in all countries within this time frame, and that time frames for peaking may be shaped by sustainable development, poverty eradication needs and equity and be in line with different national circumstances\" (UNFCCC 2023).",
  "char_count": 1498,
  "pages": [45, 46, 47]
}
```

**Notice:**
- More complete context
- Full sentences preserved
- Better citation preservation
- Natural reading flow

---

## Troubleshooting

### If title inference doesn't work:
- Check if first page has readable text (not just images)
- Verify first page isn't a blank cover page
- Try adjusting title detection thresholds in code

### If chunks are still too fragmented:
- Increase `chunk_size` to 2000
- Increase `overlap` to 500
- Adjust in `api.py` line ~843 and ~1500

### If extraction is slower:
- Larger chunks mean fewer chunks, should be faster
- But better quality might need more processing
- Monitor with: `time curl http://localhost:5000/extract/async ...`

---

## Performance Impact

**Speed:**
- ✅ Faster: Fewer chunks to process (~30% reduction)
- ✅ Similar: Extraction time roughly the same
- ⚠️ Slightly slower: More sophisticated boundary detection

**Quality:**
- ✅ Better: More context per chunk
- ✅ Better: Fewer mid-sentence breaks
- ✅ Better: Automatic title detection
- ✅ Better: Preserved document structure

**Storage:**
- ✅ Similar: Total text size unchanged
- ✅ Less: Fewer chunk objects (~30% reduction)
- ⚠️ Slightly more: Larger overlap means some redundancy

---

## Questions?

Check the logs when processing:
```bash
# Look for these log messages:
"Inferred title from first page: ..."
"Chunking text for embeddings..." 
"Created X chunks with improved overlap"
```

---

## Summary

🎯 **Main Improvements:**
1. Chunk size: 1000 → 1500 chars (+50%)
2. Overlap: 200 → 400 chars (+100%)
3. Title: Auto-inferred from first page
4. Layout: Better table and structure detection

📊 **Expected for 76-page doc:**
- Chunks: 344 → ~230 (fewer, richer)
- Title: Empty → "Off target..." (auto-filled)
- Context: Better preserved across chunks
- Quality: Improved for embeddings and search

✅ **Ready to test!**

