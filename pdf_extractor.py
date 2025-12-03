"""
PDF Data Extractor Module
Provides functionality to extract text, metadata, and tables from PDF files.
"""

import pdfplumber
import PyPDF2
from typing import Dict, List, Any, Optional
import json


class PDFExtractor:
    """Extract data from PDF files including text, metadata, and tables."""
    
    def __init__(self, pdf_path: str):
        """
        Initialize PDF extractor with a PDF file path.
        
        Args:
            pdf_path: Path to the PDF file
        """
        self.pdf_path = pdf_path
        self.pdfplumber_pdf = None
        self.pypdf2_pdf = None
        
    def _load_pdf(self):
        """Load PDF using both libraries for different extraction needs."""
        if self.pdfplumber_pdf is None:
            self.pdfplumber_pdf = pdfplumber.open(self.pdf_path)
        if self.pypdf2_pdf is None:
            # PyPDF2.PdfReader can take a file path directly, which handles file opening/closing internally
            self.pypdf2_pdf = PyPDF2.PdfReader(self.pdf_path)
    
    def extract_metadata(self) -> Dict[str, Any]:
        """
        Extract metadata from PDF.
        
        Returns:
            Dictionary containing PDF metadata
        """
        self._load_pdf()
        metadata = {}
        
        # Extract using PyPDF2
        if self.pypdf2_pdf.metadata:
            metadata = {
                'title': self.pypdf2_pdf.metadata.get('/Title', ''),
                'author': self.pypdf2_pdf.metadata.get('/Author', ''),
                'subject': self.pypdf2_pdf.metadata.get('/Subject', ''),
                'creator': self.pypdf2_pdf.metadata.get('/Creator', ''),
                'producer': self.pypdf2_pdf.metadata.get('/Producer', ''),
                'creation_date': str(self.pypdf2_pdf.metadata.get('/CreationDate', '')),
                'modification_date': str(self.pypdf2_pdf.metadata.get('/ModDate', '')),
            }
        
        # Add additional info
        metadata['num_pages'] = len(self.pypdf2_pdf.pages)
        metadata['is_encrypted'] = self.pypdf2_pdf.is_encrypted
        
        return metadata
    
    def extract_text(self, pages: Optional[List[int]] = None) -> Dict[str, Any]:
        """
        Extract text from PDF pages with improved layout preservation.
        
        Args:
            pages: List of page numbers to extract (0-indexed). If None, extracts all pages.
        
        Returns:
            Dictionary with page numbers as keys and extracted text as values
        """
        self._load_pdf()
        text_data = {}
        
        if pages is None:
            pages = range(len(self.pdfplumber_pdf.pages))
        
        # Enhanced extraction settings for better text quality
        extraction_settings = {
            'layout': True,  # Preserve layout
            'x_tolerance': 3,  # Horizontal tolerance for character grouping
            'y_tolerance': 3,  # Vertical tolerance for line grouping
            'keep_blank_chars': False,  # Remove blank characters
            'use_text_flow': True,  # Follow text flow direction
        }
        
        for page_num in pages:
            if 0 <= page_num < len(self.pdfplumber_pdf.pages):
                page = self.pdfplumber_pdf.pages[page_num]
                # Use enhanced extraction settings
                text = page.extract_text(**extraction_settings)
                
                # Fallback to basic extraction if enhanced fails
                if not text:
                    text = page.extract_text()
                
                text_data[f'page_{page_num + 1}'] = {
                    'page_number': page_num + 1,
                    'text': text if text else '',
                    'char_count': len(text) if text else 0
                }
        
        return text_data
    
    def extract_tables(self, pages: Optional[List[int]] = None) -> Dict[str, Any]:
        """
        Extract tables from PDF pages.
        
        Args:
            pages: List of page numbers to extract (0-indexed). If None, extracts all pages.
        
        Returns:
            Dictionary with page numbers as keys and extracted tables as values
        """
        self._load_pdf()
        tables_data = {}
        
        if pages is None:
            pages = range(len(self.pdfplumber_pdf.pages))
        
        for page_num in pages:
            if 0 <= page_num < len(self.pdfplumber_pdf.pages):
                page = self.pdfplumber_pdf.pages[page_num]
                tables = page.extract_tables()
                
                if tables:
                    tables_data[f'page_{page_num + 1}'] = {
                        'page_number': page_num + 1,
                        'num_tables': len(tables),
                        'tables': [table for table in tables]
                    }
        
        return tables_data
    
    def extract_all(self, include_tables: bool = True) -> Dict[str, Any]:
        """
        Extract all available data from PDF.
        
        Args:
            include_tables: Whether to extract tables (can be slow for large PDFs)
        
        Returns:
            Dictionary containing all extracted data
        """
        result = {
            'metadata': self.extract_metadata(),
            'text': self.extract_text(),
        }
        
        if include_tables:
            result['tables'] = self.extract_tables()
        
        return result
    
    def close(self):
        """Close PDF file handles."""
        if self.pdfplumber_pdf:
            self.pdfplumber_pdf.close()
        self.pdfplumber_pdf = None
        self.pypdf2_pdf = None
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
