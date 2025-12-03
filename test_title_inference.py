#!/usr/bin/env python3
"""
Test script to verify title inference from PDF first page.
Demonstrates how the improved extraction infers titles when PDF metadata is empty.
"""

import sys
from pdf_extractor import PDFExtractor


def test_title_inference(pdf_path):
    """
    Test title inference by extracting first page and simulating the inference logic.
    
    Args:
        pdf_path: Path to PDF file
    """
    print(f"\n{'='*60}")
    print(f"Testing Title Inference for: {pdf_path}")
    print(f"{'='*60}\n")
    
    try:
        with PDFExtractor(pdf_path) as extractor:
            # Extract metadata
            metadata = extractor.extract_metadata()
            print("📄 PDF Metadata:")
            print(f"   Title: '{metadata.get('title', '')}'")
            print(f"   Author: '{metadata.get('author', '')}'")
            print(f"   Pages: {metadata.get('num_pages', 0)}")
            print()
            
            # Extract first page
            first_page_data = extractor.extract_text(pages=[0])
            if 'page_1' in first_page_data:
                first_page_text = first_page_data['page_1']['text']
                
                print("📖 First Page Text (first 500 chars):")
                print(f"   {first_page_text[:500]}...")
                print()
                
                # Simulate title inference
                inferred_title = _infer_title_logic(first_page_text)
                
                print("✨ Results:")
                print(f"   Original Title: '{metadata.get('title', '(empty)')}'")
                print(f"   Inferred Title: '{inferred_title}'")
                print()
                
                if not metadata.get('title') and inferred_title:
                    print("✅ Title will be auto-populated in GraphQL!")
                elif metadata.get('title'):
                    print("ℹ️  Title already exists in metadata, no inference needed")
                else:
                    print("⚠️  Could not infer title from first page")
                print()
                
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()


def _infer_title_logic(first_page_text):
    """
    Simplified version of title inference logic for testing.
    Matches the logic in api.py
    """
    import re
    
    if not first_page_text:
        return ""
    
    # Split into lines
    lines = [line.strip() for line in first_page_text.split('\n') if line.strip()]
    
    if not lines:
        return ""
    
    # Look for short lines at the beginning (typically titles)
    potential_titles = []
    for i, line in enumerate(lines[:10]):
        # Skip very short lines or page numbers
        if len(line) < 5 or re.match(r'^\d+$', line):
            continue
        # Titles are typically 5-100 characters
        if 5 <= len(line) <= 100:
            potential_titles.append(line)
        # Longer descriptive lines might be subtitles
        elif 100 < len(line) <= 200 and i < 5:
            potential_titles.append(line)
    
    if not potential_titles:
        return lines[0] if lines and len(lines[0]) > 10 else ""
    
    # Combine multiple lines that form the title
    if len(potential_titles) >= 2:
        combined = ' '.join(potential_titles[:3])
        if len(combined) <= 200:
            return combined
    
    return potential_titles[0]


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python test_title_inference.py <path_to_pdf>")
        print("\nExample:")
        print("  python test_title_inference.py profile.pdf")
        print("  python test_title_inference.py /path/to/EGR2025.pdf")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    test_title_inference(pdf_path)
    
    print("\n" + "="*60)
    print("Test complete!")
    print("="*60 + "\n")

