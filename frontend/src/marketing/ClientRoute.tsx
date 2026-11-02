import { useEffect } from "react";
import MarketingSite from "./MarketingSite";
import { metadata } from "./content";
import "./marketing.css";

export default function ClientRoute({ path }: { path: string }) {
  useEffect(() => {
    document.title = metadata(path).title;
    window.scrollTo(0, 0);
  }, [path]);
  return <MarketingSite path={path} />;
}
