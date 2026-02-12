FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY gunicorn.conf.py .
COPY templates/ templates/
COPY static/ static/

EXPOSE 5000

CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "-c", "gunicorn.conf.py", "app:app"]
