# clipbot — image de production (Render Free, Oracle Cloud ARM ; multi-architecture)
#
# Fonctionne avec les deux façons de publier le code sur GitHub :
#   - dépôt « normal » (dossier clipbot/ + requirements.txt à la racine)
#   - dépôt « téléphone » : 3 fichiers seulement (Dockerfile, render.yaml,
#     clipbot-src.tar.gz) -> l'archive est décompressée pendant la construction.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CLIPBOT_WORKDIR=/data \
    HF_HOME=/data/hf

# FFmpeg (avec libass pour les sous-titres) + police grasse
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN if [ -f clipbot-src.tar.gz ]; then tar -xzf clipbot-src.tar.gz && rm clipbot-src.tar.gz; fi \
 && test -f clipbot/__main__.py || (echo "Code source introuvable" && exit 1)

# yt-dlp mis à jour à chaque construction : les sites changent souvent
RUN pip install -r requirements.txt && pip install -U yt-dlp

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"8080\")}/health')"

CMD ["python", "-m", "clipbot"]
