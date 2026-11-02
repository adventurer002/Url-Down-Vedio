import { readFile, writeFile, mkdir } from "node:fs/promises";
import path from "node:path";
import {
  createServer,
  type Plugin,
  type ResolvedConfig,
  type ViteDevServer,
  type PreviewServer,
} from "vite";
import react from "@vitejs/plugin-react";
import {
  cleanPath,
  documentPaths,
  isWorkspacePath,
} from "../src/marketing/routes";

const documentFile = (route: string) =>
  route === "/" ? "index.html" : `${route.slice(1)}/index.html`;
type Renderer = {
  publicDocument(
    template: string,
    route: string,
    options: { styles: string[]; indexing?: boolean; empty?: boolean },
  ): string;
};

export default function publicPages(): Plugin {
  let config: ResolvedConfig;
  return {
    name: "videohub-public-documents",
    configResolved(value) {
      config = value;
    },
    configureServer(server: ViteDevServer) {
      server.middlewares.use(async (req, res, next) => {
        if (!["GET", "HEAD"].includes(req.method || "")) return next();
        const pathname = new URL(req.url || "/", "http://localhost").pathname;
        if (
          /\.[a-zA-Z0-9]+$/.test(pathname) ||
          /^\/(api|@|src|node_modules|media)(\/|$)/.test(pathname) ||
          pathname.startsWith("/@")
        )
          return next();
        try {
          const route = cleanPath(pathname);
          const publicPage = documentPaths.some((item) => item === route);
          const workspace = isWorkspacePath(route) && route !== "/subscription";
          const renderer = (await server.ssrLoadModule(
            "/src/marketing/render.tsx",
          )) as Renderer;
          const template = await server.transformIndexHtml(
            pathname,
            await readFile(path.join(config.root, "index.html"), "utf8"),
          );
          const styles =
            workspace || route === "/subscription"
              ? [
                  "/src/style.css",
                  "/src/subscription.css",
                  "/src/desktop-typography.css",
                  "/src/accent.css",
                ]
              : ["/src/marketing/marketing.css"];
          res.statusCode = publicPage || workspace ? 200 : 404;
          res.setHeader("Content-Type", "text/html; charset=utf-8");
          res.setHeader("X-Robots-Tag", "noindex, follow");
          const html = renderer.publicDocument(template, route, {
            styles,
            indexing: false,
            empty: workspace,
          });
          res.end(req.method === "HEAD" ? undefined : html);
        } catch (error) {
          next(error);
        }
      });
    },
    configurePreviewServer(server: PreviewServer) {
      server.middlewares.use(async (req, res, next) => {
        if (!["GET", "HEAD"].includes(req.method || "")) return next();
        const pathname = new URL(req.url || "/", "http://localhost").pathname;
        if (/\.[a-zA-Z0-9]+$/.test(pathname) || pathname.startsWith("/api/"))
          return next();
        const route = cleanPath(pathname);
        const known = documentPaths.some((item) => item === route);
        const workspace = isWorkspacePath(route);
        try {
          const file = known
            ? documentFile(route)
            : workspace
              ? "app-shell.html"
              : "404.html";
          const html = await readFile(
            path.resolve(config.root, config.build.outDir, file),
            "utf8",
          );
          res.statusCode = known || workspace ? 200 : 404;
          res.setHeader("Content-Type", "text/html; charset=utf-8");
          res.setHeader("X-Robots-Tag", "noindex, follow");
          res.end(req.method === "HEAD" ? undefined : html);
        } catch (error) {
          next(error);
        }
      });
    },
    async closeBundle() {
      if (config.command !== "build" || config.build.ssr) return;
      const out = path.resolve(config.root, config.build.outDir);
      const template = await readFile(path.join(out, "index.html"), "utf8");
      const manifest = JSON.parse(
        await readFile(path.join(out, "asset-manifest.json"), "utf8"),
      ) as Record<
        string,
        { name?: string; css?: string[]; imports?: string[] }
      >;
      const css = (key: string) => {
        const styles = new Set<string>();
        const seen = new Set<string>();
        const collect = (name: string) => {
          if (seen.has(name)) return;
          seen.add(name);
          const item = manifest[name];
          item?.imports?.forEach(collect);
          item?.css?.forEach((file) => styles.add(`/${file}`));
        };
        const resolved = manifest[key]
          ? key
          : Object.keys(manifest).find(
              (name) =>
                manifest[name].name ===
                path.basename(key).replace(/\.tsx$/, ""),
            );
        if (!resolved) throw new Error(`Missing build entry: ${key}`);
        collect(resolved);
        return [...styles];
      };
      const ssr = await createServer({
        root: config.root,
        configFile: false,
        mode: config.mode,
        cacheDir: path.resolve(config.root, "node_modules/.vite-public-ssr"),
        plugins: [react()],
        appType: "custom",
        server: { middlewareMode: true, hmr: false, ws: false, watch: null },
        optimizeDeps: { noDiscovery: true, include: [] },
        envPrefix: "VITE_",
      });
      try {
        const renderer = (await ssr.ssrLoadModule(
          "/src/marketing/render.tsx",
        )) as Renderer;
        const indexing = config.env.VITE_PUBLIC_INDEXING !== "false";
        for (const route of documentPaths) {
          const target = path.join(out, documentFile(route));
          await mkdir(path.dirname(target), { recursive: true });
          await writeFile(
            target,
            renderer.publicDocument(template, route, {
              styles: css(
                route === "/subscription"
                  ? "src/workspace-client.tsx"
                  : "src/marketing/client.tsx",
              ),
              indexing,
            }),
          );
        }
        await writeFile(
          path.join(out, "app-shell.html"),
          renderer.publicDocument(template, "/app", {
            styles: css("src/workspace-client.tsx"),
            empty: true,
            indexing: false,
          }),
        );
        await writeFile(
          path.join(out, "404.html"),
          renderer.publicDocument(template, "/not-found", {
            styles: css("src/marketing/client.tsx"),
            indexing: false,
          }),
        );
        const origin = (
          config.env.VITE_SITE_URL || "https://vedio.vip"
        ).replace(/\/$/, "");
        await writeFile(
          path.join(out, "sitemap.xml"),
          `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${documentPaths.map((route) => `<url><loc>${origin}${route}</loc></url>`).join("")}</urlset>\n`,
        );
        await writeFile(
          path.join(out, "robots.txt"),
          indexing
            ? `User-agent: *\nDisallow: /api/\n\nSitemap: ${origin}/sitemap.xml\n`
            : "User-agent: *\nDisallow: /\n",
        );
      } finally {
        await ssr.close();
      }
    },
  };
}
