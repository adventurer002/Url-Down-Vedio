import { useEffect, useState, type ComponentType } from "react";
import type { AeroShardsProps } from "./AeroShards";

/** Optional decoration: never blocks the form or loads GPU code on unsupported devices. */
export default function HeroBackground() {
  const [enabled, setEnabled] = useState(false);
  const [failed, setFailed] = useState(false);
  const [Shards, setShards] = useState<ComponentType<AeroShardsProps> | null>(
    null,
  );

  useEffect(() => {
    if (!("gpu" in navigator) || typeof window.matchMedia !== "function")
      return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    const mobile = window.matchMedia("(max-width: 767px)");
    const update = () => setEnabled(!reduced.matches && !mobile.matches);
    update();
    reduced.addEventListener("change", update);
    mobile.addEventListener("change", update);
    return () => {
      reduced.removeEventListener("change", update);
      mobile.removeEventListener("change", update);
    };
  }, []);

  useEffect(() => {
    if (!enabled || failed) return;
    let active = true;
    import("./AeroShards").then(
      (module) => {
        if (active) setShards(() => module.default);
      },
      () => {
        if (active) setFailed(true);
      },
    );
    return () => {
      active = false;
    };
  }, [enabled, failed]);

  if (!enabled || failed || !Shards) return null;
  return (
    <div className="hero-background" aria-hidden="true">
      <Shards
        shardColor="#6e7279ff"
        accentColor="#585b5eff"
        lightMode
        placement="full"
        material="glass"
        count={400}
        speed={0.6}
        shardSize={0.8}
        spread={1}
        depthOfField={0.5}
        glow={0.4}
        grain={0}
        vignette={0.5}
        markers="none"
        intro={false}
        mouseInteraction={false}
        pulseOnClick={false}
        holdToGather={false}
        parallax={0}
        dpr={1.25}
        onError={() => setFailed(true)}
      />
    </div>
  );
}
