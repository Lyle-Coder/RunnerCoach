FROM python:3.12-slim
WORKDIR /app
# 网关本体零依赖；cryptography 仅微信支付回调验签/解密用，装不上不影响其它功能
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple cryptography \
    || echo "cryptography 安装失败：支付回调不可用，其余功能不受影响"
COPY app.py .
ENV DB_PATH=/app/data/data.db
VOLUME /app/data
EXPOSE 8300
CMD ["python", "app.py"]
