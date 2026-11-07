# Strait Safe Route - container image for Railway (or any Docker host).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src MPLBACKEND=Agg \
    SAFEROUTE_MAX_NETWORKS=8
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Optional: MPA GeoSpace-Sea chart layers. If the Railway service has an
# MPA_API_KEY variable, the layers are downloaded once per build and the
# berths and dashboard data rebuilt from them (none of it is in the repo).
# Without the key the image is built on GEBCO + OpenStreetMap as before.
ARG MPA_API_KEY=""
RUN if [ -n "$MPA_API_KEY" ]; then \
      MPA_API_KEY="$MPA_API_KEY" python scripts/download_gss.py && \
      python scripts/build_mpa.py ; \
    else echo "no MPA_API_KEY: building without the MPA chart layers"; fi
# Build the board once at image build time as a smoke test.
RUN python -c "from saferoute import cache; cache.grid()"
EXPOSE 8765
CMD ["python", "-m", "saferoute.serve"]
