FROM python:3.11-slim
WORKDIR /worker
COPY analysis/factor_ast.py /worker/factor_ast.py
COPY scripts/research-worker/worker.py /worker/worker.py
USER 65534:65534
ENTRYPOINT ["python", "-I", "/worker/worker.py"]
