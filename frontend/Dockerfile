FROM node:20-alpine

WORKDIR /app

# npm 国内镜像（上传 GitHub 后他人也可更快构建）
RUN npm config set registry https://registry.npmmirror.com

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

ENV HOST=0.0.0.0
ENV PORT=5173

EXPOSE 5173

# 演示环境用 Vite dev server（支持运行时 VITE_API_BASE）
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
