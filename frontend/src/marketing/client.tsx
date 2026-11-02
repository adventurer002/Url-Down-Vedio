import { hydrateRoot, createRoot } from "react-dom/client";
import MarketingSite from "./MarketingSite";
import { cleanPath, documentPaths } from "./routes";
import { metadata } from "./content";
import "./marketing.css";

const path = cleanPath(window.location.pathname);
document.title = metadata(path).title;
const root = document.getElementById("app")!;
const element = <MarketingSite path={path} />;
if (root.hasChildNodes()) hydrateRoot(root, element);
else createRoot(root).render(element);
if (!documentPaths.some((item) => item === path)) {
  const robots = document.createElement("meta");
  robots.name = "robots";
  robots.content = "noindex,follow";
  document.head.appendChild(robots);
}
