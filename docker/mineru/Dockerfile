ARG BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim

FROM ${BASE_IMAGE}

# PyPI 国内镜像源, 可用构建参数覆盖
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} "mineru[core]<4"

# mineru 依赖的 opencv-python(非 headless 版)运行需要 X11/OpenGL 系统库,
# python:*-slim 基础镜像不带 libxcb.so.1 / libGL.so.1 等, 缺失会导致
# `import cv2` 直接 ImportError, 故在此补齐。
# 使用国内 Debian 镜像源(deb.debian.org 直连偶发 503 且很慢), 再补齐系统库。
# 放在 pip 层之后: 重建时 pip 依赖层缓存可复用。
RUN sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g; s|security.debian.org|mirrors.ustc.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g; s|security.debian.org|mirrors.ustc.edu.cn|g' \
        /etc/apt/sources.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libxcb1 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# 模型从 ModelScope 下载(国内网络可达)
ENV MINERU_MODEL_SOURCE=modelscope

EXPOSE 8888

CMD ["mineru-api", "--host", "0.0.0.0", "--port", "8888"]
