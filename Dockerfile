# BankQL API. llama-server runs on the HOST (GPU); point BANKQL_ROUTER_URL / BANKQL_SQL_URL at host.docker.internal.
# Registry is a build ARG (Docker Hub rate-limits anonymous pulls): docker build --build-arg REGISTRY=docker.io/library .
ARG REGISTRY=public.ecr.aws/docker/library
FROM ${REGISTRY}/python:3.12-slim
RUN pip install --no-cache-dir uv==0.9.7
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
# dev group included: the image can also build the synthetic fixture (Faker) and run the test suite.
RUN uv sync --frozen --no-install-project --group dev
COPY bankql ./bankql
COPY config ./config
COPY scripts ./scripts
COPY tests ./tests
RUN uv sync --frozen --group dev && useradd -r -u 10001 bankql && mkdir -p data && chown bankql data
USER bankql
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uvicorn", "bankql.api:app", "--host", "0.0.0.0", "--port", "8000"]
