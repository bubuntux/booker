FROM mcr.microsoft.com/playwright/python:v1.58.0

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY book.py .

ENTRYPOINT ["python", "book.py"]
