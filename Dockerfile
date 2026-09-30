# Strait Safe Route - container image for Railway (or any Docker host).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src MPLBACKEND=Agg \
    SAFEROUTE_MAX_NETWORKS=8
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Build the board once at image build time as a smoke test (about a second).
RUN python -c "from saferoute import cache; cache.grid()"
EXPOSE 8765
CMD ["python", "-m", "saferoute.serve"]
