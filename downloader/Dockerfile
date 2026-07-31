FROM python:3.12-slim

COPY requirements.txt /requirements.txt
COPY download.py /download.py
COPY model_list.yaml /model_list.yaml

RUN pip install --upgrade pip && pip install --no-cache-dir -r /requirements.txt

CMD ["python", "/download.py"]
