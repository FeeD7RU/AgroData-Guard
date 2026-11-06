FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml README.md ./
COPY agroguard ./agroguard
RUN pip install --no-cache-dir --no-deps . && useradd --create-home guard && mkdir -p /app/data && chown guard:guard /app/data
USER guard
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')"
CMD ["agroguard", "serve", "--host", "0.0.0.0", "--demo"]
