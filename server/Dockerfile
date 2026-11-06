FROM python:3.12-slim
WORKDIR /app
# 纯标准库实现，无 pip 依赖
COPY app.py .
ENV DB_PATH=/app/data/data.db
VOLUME /app/data
EXPOSE 8300
CMD ["python", "app.py"]
