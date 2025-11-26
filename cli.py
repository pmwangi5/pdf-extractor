#!/usr/bin/env python3
"""
Command-line interface for PDF data extraction.
"""

import argparse
import json
import sys
from pathlib import Path
from pdf_extractor import PDFExtractor


def main():
    parser = argparse.ArgumentParser(
        description='Extract data from PDF files',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Extract all data from a PDF
  python cli.py document.pdf
  
  # Extract only text
  python cli.py document.pdf --text-only
  
  # Extract only metadata
  python cli.py document.pdf --metadata-only
  
  # Extract specific pages
  python cli.py document.pdf --pages 1 2 3
  
  # Save output to file
  python cli.py document.pdf --output results.json
        """
    )
    
    parser.add_argument(
        'pdf_file',
        type=str,
        help='Path to the PDF file to process'
    )
    
    parser.add_argument(
        '--text-only',
        action='store_true',
        help='Extract only text content'
    )
    
    parser.add_argument(
        '--metadata-only',
        action='store_true',
        help='Extract only metadata'
    )
    
    parser.add_argument(
        '--tables-only',
        action='store_true',
        help='Extract only tables'
    )
    
    parser.add_argument(
        '--no-tables',
        action='store_true',
        help='Skip table extraction (faster for large PDFs)'
    )
    
    parser.add_argument(
        '--pages',
        type=int,
        nargs='+',
        help='Specific page numbers to extract (1-indexed)'
    )
    
    parser.add_argument(
        '--output',
        '-o',
        type=str,
        help='Output file path (JSON format). If not specified, prints to stdout'
    )
    
    parser.add_argument(
        '--pretty',
        action='store_true',
        help='Pretty print JSON output'
    )
    
    args = parser.parse_args()
    
    # Validate PDF file exists
    pdf_path = Path(args.pdf_file)
    if not pdf_path.exists():
        print(f"Error: PDF file not found: {args.pdf_file}", file=sys.stderr)
        sys.exit(1)
    
    if not pdf_path.is_file():
        print(f"Error: Not a valid file: {args.pdf_file}", file=sys.stderr)
        sys.exit(1)
    
    try:
        # Convert 1-indexed page numbers to 0-indexed
        pages = None
        if args.pages:
            pages = [p - 1 for p in args.pages]
        
        # Extract data based on options
        with PDFExtractor(str(pdf_path)) as extractor:
            if args.metadata_only:
                result = {'metadata': extractor.extract_metadata()}
            elif args.text_only:
                result = {'text': extractor.extract_text(pages)}
            elif args.tables_only:
                result = {'tables': extractor.extract_tables(pages)}
            else:
                # Extract all data, passing pages directly for efficiency
                result = {
                    'metadata': extractor.extract_metadata(),
                    'text': extractor.extract_text(pages),
                }
                if not args.no_tables:
                    result['tables'] = extractor.extract_tables(pages)
        
        # Output results
        output_json = json.dumps(result, indent=2 if args.pretty else None, ensure_ascii=False)
        
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(output_json)
            print(f"Results saved to: {args.output}")
        else:
            print(output_json)
            
    except Exception as e:
        print(f"Error processing PDF: {str(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
