import type { NextConfig } from "next";

// 开发态才需要反向代理：生产是同源部署（FastAPI 把 ui/out 挂在 /workbench，
// 数据面就在同一个 origin 的 /api/v1 下），所以 rewrites 只在 `next dev` 生效。
// `output: "export"` 与 rewrites 不兼容，条件展开保证 `next build` 看不到它。
const isDev = process.env.NODE_ENV === "development";
const coreOrigin = process.env.SW_CORE_ORIGIN ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  output: "export",
  // FastAPI 把产物挂在 /workbench 下，所有资源与路由都带这个前缀
  basePath: "/workbench",
  // 导出成 out/<route>/index.html，StaticFiles(html=True) 直接能服务
  trailingSlash: true,
  reactStrictMode: true,
  // 静态导出没有图片优化服务；媒体本来就是后端直出的原文件
  images: { unoptimized: true },
  ...(isDev
    ? {
        async rewrites() {
          return [
            // 数据面
            { source: "/api/:path*", destination: `${coreOrigin}/api/:path*`, basePath: false },
            // 媒体端点（封面 / 图集 / 成片 / 公众号预览），刻意不在 /api/v1 下
            { source: "/review/:path*", destination: `${coreOrigin}/review/:path*`, basePath: false },
          ];
        },
      }
    : {}),
};

export default nextConfig;
