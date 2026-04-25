FROM mcr.microsoft.com/playwright/python:v1.59.1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY book.py .

ENTRYPOINT ["python", "book.py"]
