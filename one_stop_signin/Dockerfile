ARG BUILD_FROM
FROM ${BUILD_FROM}
ENV PYTHONUNBUFFERED=1
# The Playwright image ships the browsers; the Python package must match its version.
RUN pip install --no-cache-dir --break-system-packages "playwright==1.65.2" "aiohttp==3.11.18" \
 || pip install --no-cache-dir "playwright==1.65.2" "aiohttp==3.11.18"
COPY app.py /app/app.py
CMD ["python3", "/app/app.py"]
