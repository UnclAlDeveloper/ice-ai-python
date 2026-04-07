FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium

COPY . .
RUN chmod +x entrypoint.sh

EXPOSE 8005

CMD ["./entrypoint.sh"]
