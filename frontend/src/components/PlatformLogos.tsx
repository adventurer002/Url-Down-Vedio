import { useEffect, useState } from "react";
import {
  SiBilibili,
  SiDailymotion,
  SiTiktok,
  SiVimeo,
  SiYoutube,
} from "react-icons/si";
import LogoLoop, { type LogoItem } from "./LogoLoop";

// Matches named extractors in the installed backend. Individual videos may still be unavailable.
const platforms: LogoItem[] = [
  { node: <SiYoutube aria-hidden="true" />, title: "YouTube" },
  { node: <SiBilibili aria-hidden="true" />, title: "bilibili" },
  { node: <SiVimeo aria-hidden="true" />, title: "Vimeo" },
  { node: <SiDailymotion aria-hidden="true" />, title: "Dailymotion" },
  { node: <SiTiktok aria-hidden="true" />, title: "TikTok" },
];

export default function PlatformLogos() {
  const [reducedMotion, setReducedMotion] = useState(false);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReducedMotion(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return (
    <section className="platform-section" aria-label="视频平台">
      {reducedMotion ? (
        <ul className="platform-static" aria-label="视频来源平台">
          {platforms.map((item) => (
            <li key={item.title}>
              {"node" in item && item.node}
              <span>{item.title}</span>
            </li>
          ))}
        </ul>
      ) : (
        <LogoLoop
          logos={platforms}
          speed={30}
          layout="line"
          logoHeight={40}
          gap={56}
          hoverSpeed={0}
          fadeOut
          fadeOutWidth={0.08}
          labels="inline"
          draggable={false}
          intro={false}
          ariaLabel="视频来源平台"
          className="platform-loop"
        />
      )}
    </section>
  );
}
