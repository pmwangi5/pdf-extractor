# Use Python 3.11 slim image
FROM python:3.11-slim

# Install system dependencies required for PDF processing
# Poppler is required by pdf2image for PDF to image conversion
RUN apt-get update && apt-get install -y \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port (default to 5000, Railway will set PORT env var at runtime)
EXPOSE 5000

# Use gunicorn to run the Flask app
# Railway will set PORT env var, so we use sh -c to properly expand it
CMD sh -c "gunicorn api:app --bind 0.0.0.0:${PORT:-5000} --workers 2 --timeout 120 --access-logfile - --error-logfile -"

