FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 KOKORO_MODEL_DIR=/app/models ONNX_PROVIDER=CPUExecutionProvider

WORKDIR /app

# Install sound and system packages
RUN apt-get update && apt-get install -y \
    libsndfile1 \
    espeak-ng \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download model and mapping files during Docker build
COPY download_model.py .
RUN python download_model.py

# Copy code
COPY app.py .

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
