# Build context is this folder (see docker-compose.yml). Needs LibreOffice and poppler for the PDF render and page images.
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-impress poppler-utils fonts-liberation fonts-crosextra-carlito fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY deck_service ./deck_service
COPY agents ./agents
ENV DECK_OUTPUT_DIR=/files/decks
EXPOSE 8010
CMD ["uvicorn", "deck_service.main:app", "--host", "0.0.0.0", "--port", "8010"]
