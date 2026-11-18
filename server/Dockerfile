FROM python:3.12-slim
WORKDIR /app

# 网关本体零依赖；cryptography 仅微信支付回调验签/解密用，装不上不影响其它功能
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple cryptography \
    || echo "cryptography 安装失败：支付回调不可用，其余功能不受影响"

# 不只是 app.py：小组（groups）与远征（expedition）是运行时按需 import 的模块，
# 少拷一个的表现是「接口 503 / 组功能不可用」，而且启动不报错——很难查。
COPY app.py groups.py expedition.py tick.py ranking.py ./
# 内容池不在仓库里（.gitignore 排除，见 docs/用户组设计.md 的内容红线），
# 构建前必须把它放到 server/content/，否则远征一进就 503「内容未配置」。
COPY content ./content

ENV DB_PATH=/app/data/data.db
VOLUME /app/data
# 无域名部署用的是自签证书 + 8443（80/443 未备案容易被拦）
EXPOSE 8443
CMD ["python", "app.py"]
