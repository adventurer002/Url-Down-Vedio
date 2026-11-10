import assert from "node:assert/strict";
import { readFile, access } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const output = fileURLToPath(new URL("../dist/", import.meta.url));
const routes = ["/", "/help", "/privacy", "/examples", "/subscription"];
const documents = new Map();
for (const route of routes) {
  const file = route === "/" ? "index.html" : `${route.slice(1)}/index.html`;
  const document = new JSDOM(await readFile(path.join(output, file), "utf8"))
    .window.document;
  documents.set(route, document);
  assert.equal(
    document.querySelectorAll("h1").length,
    1,
    `${route}: expected one readable H1`,
  );
  assert.ok(
    document.querySelector("main")?.textContent.length > 200,
    `${route}: missing initial content`,
  );
  assert.equal(document.querySelectorAll('meta[name="description"]').length, 1);
  assert.ok(document.querySelector('meta[property="og:image"]'));
  const schema = JSON.parse(
    document.querySelector('script[type="application/ld+json"]').textContent,
  );
  assert.ok(schema["@graph"].some((item) => item["@type"] === "WebPage"));
  const styles = [...document.querySelectorAll('link[rel="stylesheet"]')];
  assert.ok(styles.length, `${route}: missing initial stylesheet`);
  for (const stylesheet of styles)
    await access(path.join(output, stylesheet.getAttribute("href")));
  for (const element of document.querySelectorAll("[src], [poster]")) {
    const url = element.getAttribute("src") || element.getAttribute("poster");
    if (url?.startsWith("/")) await access(path.join(output, url));
  }
}
const home = documents.get("/");
const origin = new URL(home.querySelector('link[rel="canonical"]').href).origin;
const indexing = home.querySelector('meta[name="robots"]').content;
for (const [route, document] of documents) {
  assert.equal(
    document.querySelector('link[rel="canonical"]').href,
    `${origin}${route}`,
  );
  assert.equal(document.querySelector('meta[name="robots"]').content, indexing);
  for (const link of document.querySelectorAll('a[href^="/"]')) {
    const url = new URL(link.href, origin);
    if (url.pathname.startsWith("/media/"))
      await access(path.join(output, decodeURIComponent(url.pathname)));
    if (url.hash && documents.has(url.pathname)) {
      assert.ok(
        documents
          .get(url.pathname)
          .getElementById(decodeURIComponent(url.hash.slice(1))),
        `Missing anchor ${url.href}`,
      );
    }
  }
}
const sitemap = new JSDOM(
  await readFile(path.join(output, "sitemap.xml"), "utf8"),
  { contentType: "text/xml" },
);
assert.deepEqual(
  [...sitemap.window.document.querySelectorAll("loc")].map(
    (item) => item.textContent,
  ),
  routes.map((route) => `${origin}${route}`),
);
for (const file of ["app-shell.html", "404.html"]) {
  const document = new JSDOM(await readFile(path.join(output, file), "utf8"))
    .window.document;
  assert.equal(
    document.querySelector('meta[name="robots"]').content,
    "noindex,follow",
  );
  assert.equal(document.querySelector('link[rel="canonical"]'), null);
  assert.ok(document.querySelector('link[rel="stylesheet"]'));
}
await access(path.join(output, "media/landing/social-cover.png"));
console.log(
  "Public output verified: 5 readable documents, metadata, styles, assets, source links, sitemap and private noindex shells.",
);
