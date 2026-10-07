FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY static static
COPY templates templates
COPY config.example.yaml config.yaml
COPY company_notes.example.yaml company_notes.yaml
COPY resumes/resume_base.example.yaml resumes/resume_base.yaml
RUN mkdir -p data output/prompts output/resumes
CMD exec uvicorn app.web:app --host 0.0.0.0 --port ${PORT}
