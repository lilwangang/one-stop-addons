ARG BUILD_FROM
FROM ${BUILD_FROM}
ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir aiohttp==3.11.18
COPY app.py /app/app.py
CMD ["python3", "/app/app.py"]
