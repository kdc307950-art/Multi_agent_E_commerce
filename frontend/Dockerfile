# 前端镜像（Next.js standalone）。仅提供骨架，未在本机构建验证。
# 基础镜像走镜像加速器（本机对 docker.io 直连受限）。
FROM docker.1ms.run/library/node:20-alpine AS deps
WORKDIR /app
COPY package.json ./
RUN npm install --registry=https://registry.npmmirror.com

FROM docker.1ms.run/library/node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Next.js 的 NEXT_PUBLIC_* 与 next.config.mjs 均在 build 时求值，故以 build args 注入。
# 默认同源 /api（本地/预发布一致）；预发布同源由 nginx 承担，可传 API_PROXY_TARGET="" 关闭 rewrites。
ARG NEXT_PUBLIC_API_BASE_URL=/api
ARG API_PROXY_TARGET=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL}
ENV API_PROXY_TARGET=${API_PROXY_TARGET}
RUN npm run build

FROM docker.1ms.run/library/node:20-alpine AS runner
WORKDIR /app
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json
ENV NODE_ENV=production
EXPOSE 3000
CMD ["npm", "start"]
