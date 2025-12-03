# PDF Extraction Improvements

## Summary of Changes

This document outlines improvements made to the PDF extraction system to address:
1. **Better context preservation** in chunked embeddings
2. **Automatic title inference** from document content
3. **More thorough text extraction** with enhanced layout detection

---

## 1. Improved Chunking for Better Context (api.py)

### Changes:
- **Increased chunk size**: `1000 → 1500 characters`
  - Technical documents need more context per chunk
  - Prevents breaking up related concepts
  
- **Increased overlap**: `200 → 400 characters`
  - Ensures better continuity between chunks
  - Helps embeddings understand cross-chunk relationships
  
- **Smarter overlap boundaries**: 
  - Now looks for natural break points (sentences, paragraphs)
  - Avoids cutting words or sentences mid-way
  - Preserves semantic meaning across chunk boundaries

### Before vs After:
```
BEFORE: 1000 chars with 200 char overlap
Result: ~344 chunks with frequent context breaks

AFTER: 1500 chars with 400 char overlap
Result: ~230 chunks with better preserved context
```

---

## 2. Automatic Title Inference (api.py)

### New Feature: `_infer_title_from_first_page()`

When PDF metadata lacks a title, the script now:

1. **Extracts first page text**
2. **Analyzes top 10 lines** for title patterns
3. **Combines multi-line titles** (common in PDFs)
4. **Returns inferred title**

### Example:
For your Emissions Gap Report, it will now extract:
```
"Off target Continued collective inaction puts global temperature goal at risk Emissions Gap Report 2025"
```

Or optionally just the main title:
```
"Off target"
```

### Integration:
The inferred title is automatically added to metadata before sending to GraphQL, so your `metadata.title` field will be populated even when PDF metadata is empty.

---

## 3. Enhanced Text Extraction (pdf_extractor.py)

### Improved `extract_text()` method:

Added **layout-aware extraction settings**:
```python
extraction_settings = {
    'layout': True,        # Preserve document layout
    'x_tolerance': 3,      # Better column detection
    'y_tolerance': 3,      # Better line grouping
    'keep_blank_chars': False,  # Remove artifacts
    'use_text_flow': True,      # Follow reading order
}
```

### Benefits:
- **Better table text extraction** - columns stay aligned
- **Preserved headers and footers** - maintains document structure
- **Multi-column support** - correctly orders text in 2-column layouts
- **Reduced extraction artifacts** - cleaner text output

---

## 4. Improved Semantic Unit Splitting (api.py)

### Enhancements:

1. **Header Detection**:
   - Recognizes section headers (Chapter, Section, Box, Figure, Table)
   - Keeps headers as separate units for context
   - Preserves ALL CAPS and Title Case formatting

2. **List Handling**:
   - Keeps short lists together (< 1500 chars)
   - Only splits very long lists
   - Preserves list context and relationships

3. **Paragraph Preservation**:
   - Increased split threshold: `800 → 1200 characters`
   - Keeps related content together
   - Better handling of technical paragraphs

---

## 5. Configuration Updates

### Updated Constants:
```python
# Before
MAX_CHARS_PER_CHUNK = 2000
chunk_size = 1000
overlap = 200

# After
MAX_CHARS_PER_CHUNK = 3000  # +50% for technical docs
chunk_size = 1500            # +50% better context
overlap = 400                # +100% better continuity
```

---

## Expected Results

### For Your 76-Page Emissions Gap Report:

**Before:**
- ~344 chunks
- Average chunk: ~1000 characters
- Frequent context breaks
- Empty title in metadata
- Some tables/figures text fragmented

**After:**
- ~230 chunks (fewer, but richer)
- Average chunk: ~1500 characters
- Better context preservation
- Title: "Off target Continued collective inaction puts global temperature goal at risk Emissions Gap Report 2025"
- Improved table text extraction
- Section headers preserved as context markers

---

## Testing the Improvements

To test with your document:

```bash
# From serverPython/pdf-extractor directory
cd /Users/patrick/DEV/OUTBOX/GCS/2025/serverPython/pdf-extractor

# Test title inference and improved chunking
curl -X POST -F "file=@profile.pdf" \
     -F "send_to_nhost=true" \
     -F "user_id=your-user-id" \
     http://localhost:5000/extract/async

# Check job status
curl http://localhost:5000/job/<job_id>
```

---

## Additional Recommendations

### For Even Better Embeddings:

1. **Consider Document Structure**:
   ```python
   # Add to chunks:
   'section': 'Chapter 2',  # Current section
   'context_before': 'Previous paragraph summary',
   'context_after': 'Next paragraph summary'
   ```

2. **Add Metadata to Each Chunk**:
   ```python
   # Include in chunk object:
   'document_title': metadata.get('title'),
   'page_numbers': chunk['pages'],
   'chunk_type': 'paragraph|list|table|header'
   ```

3. **Consider Using Semantic Chunking Libraries**:
   - `langchain` TextSplitter
   - `llama-index` SentenceSplitter
   - Custom semantic splitters based on document type

4. **For Large Technical Documents**:
   - Consider even larger chunks: 2000-2500 chars
   - Use 500-600 char overlap
   - Preserve entire tables/figures in single chunks

---

## What's Preserved Now

✅ Section headers and titles  
✅ Bullet and numbered lists (together when short)  
✅ Table text with better column alignment  
✅ Figures and captions (when text)  
✅ References and citations  
✅ Multi-line titles and headings  
✅ Document structure and flow  
✅ Cross-references and page numbers  
✅ Technical terms and acronyms with context  

---

## Monitoring Extraction Quality

After deploying, check:

1. **Chunk Count**: Should be ~30% fewer chunks but each with more context
2. **Title Field**: Should be populated in metadata
3. **Text Quality**: Review `text_chunks` field in database
4. **Search Quality**: Test chatbot responses for better context understanding

---

## Next Steps

1. **Deploy changes** to your Python API server
2. **Re-process test document** to verify improvements
3. **Compare chunk quality** before/after
4. **Test chatbot responses** with new embeddings
5. **Monitor extraction logs** for any issues

---

## Notes

- Chunk counts will vary based on document structure
- Technical documents benefit from larger chunks
- Balance chunk size with embedding model limits (most support 512-2048 tokens)
- Consider your embedding model's context window when adjusting sizes

---

*Improvements made: December 2025*

