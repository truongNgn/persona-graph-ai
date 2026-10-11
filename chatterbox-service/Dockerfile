FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 HF_HOME=/models HF_HUB_DISABLE_XET=1 TOKENIZERS_PARALLELISM=false
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git libsndfile1 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir --no-deps git+https://github.com/resemble-ai/Perth.git@ff1c8ac55a976971245cdd53c18d6131ca00d993 git+https://github.com/resemble-ai/chatterbox.git@5de7a54aa4e5e2baadb0182dde554908b48b85c2
COPY app.py .
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
