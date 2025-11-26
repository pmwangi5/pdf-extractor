#!/usr/bin/env python3
"""
Example usage of the PDF extractor module.
Demonstrates how to use the PDFExtractor class programmatically.
"""

from pdf_extractor import PDFExtractor
import json
import sys


def example_usage(pdf_path):
    """Example of extracting data from a PDF."""
    
    if not pdf_path:
        print("Usage: python example_usage.py <path_to_pdf>")
        sys.exit(1)
    
    try:
        # Using context manager (recommended)
        with PDFExtractor(pdf_path) as extractor:
            # Extract metadata
            print("=" * 60)
            print("METADATA")
            print("=" * 60)
            metadata = extractor.extract_metadata()
            print(json.dumps(metadata, indent=2, ensure_ascii=False))
            
            # Extract text from first page
            print("\n" + "=" * 60)
            print("TEXT FROM PAGE 1")
            print("=" * 60)
            text_data = extractor.extract_text(pages=[0])
            if text_data:
                page_text = text_data.get('page_1', {})
                print(f"Character count: {page_text.get('char_count', 0)}")
                print(f"Text preview (first 500 chars):")
                text = page_text.get('text', '')
                print(text[:500] + "..." if len(text) > 500 else text)
            
            # Extract all data
            print("\n" + "=" * 60)
            print("EXTRACTING ALL DATA (this may take a moment)...")
            print("=" * 60)
            all_data = extractor.extract_all(include_tables=True)
            
            # Summary
            print(f"\nSummary:")
            print(f"  - Total pages: {all_data['metadata']['num_pages']}")
            print(f"  - Pages with text: {len(all_data['text'])}")
            if 'tables' in all_data:
                total_tables = sum(
                    page_data.get('num_tables', 0) 
                    for page_data in all_data['tables'].values()
                )
                print(f"  - Total tables found: {total_tables}")
            
    except FileNotFoundError:
        print(f"Error: PDF file not found: {pdf_path}")
        sys.exit(1)
    except Exception as e:
        print(f"Error processing PDF: {str(e)}")
        sys.exit(1)


if __name__ == '__main__':
    pdf_path = sys.argv[1] if len(sys.argv) > 1 else None
    example_usage(pdf_path)
