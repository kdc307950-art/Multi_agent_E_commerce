/** @type {import('next').NextConfig} */
// 反向代理：浏览器只访问同源 /api/*，由 Next.js 服务端代理到后端，规避浏览器跨域。
// 生产/预发布环境中 Nginx/Ingress 承担该角色（见《生产环境架构设计》§3）；
// 仅当 API_PROXY_TARGET 非空时开启 Next rewrites（本地开发用），为空则关闭（同源交 nginx）。
// 本地开发默认转发到 FastAPI；生产环境由 Nginx/Ingress 接管，仍可显式覆写。
const API_PROXY_TARGET = process.env.API_PROXY_TARGET ??
  (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8000" : "");

const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    if (!API_PROXY_TARGET) return [];
    return [
      {
        source: "/api/:path*",
        destination: `${API_PROXY_TARGET}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
