FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm poppler-utils \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

CMD ["python", "main.py"]
