import { useEffect, type RefObject } from "react";

/** Connect the native side cards to the artwork, without changing the bitmap. */
function updateHeroConnectors(root: HTMLElement) {
  const hero = root.querySelector<HTMLElement>(".m-hero");
  const stage = root.querySelector<HTMLElement>(".m-hero-stage");
  const svg = root.querySelector<SVGSVGElement>(".m-hero-connectors");
  if (!hero || !stage || !svg) return;
  const frame = hero.getBoundingClientRect();
  if (frame.width <= 900 || !frame.height) return;
  const art = stage.getBoundingClientRect();
  svg.setAttribute("viewBox", `0 0 ${frame.width} ${frame.height}`);
  for (const side of ["left", "right"] as const) {
    const card = root.querySelector<HTMLElement>(`.m-hero-side-${side}`);
    const path = svg.querySelector<SVGPathElement>(
      `[data-hero-connector="${side}"]`,
    );
    if (!card || !path) continue;
    const box = card.getBoundingClientRect();
    const sx = box.left + box.width / 2 - frame.left;
    // Begin inside the card so its background naturally covers the join.
    const sy = box.bottom - 14 - frame.top;
    const ex =
      art.left + art.width * (side === "left" ? 0.18 : 0.88) - frame.left;
    const ey =
      art.top + art.height * (side === "left" ? 0.34 : 0.4) - frame.top;
    const bend = Math.max(48, Math.abs(ey - sy) * 0.65);
    const approach = side === "left" ? -60 : 60;
    const d = `M${sx} ${sy}C${sx} ${sy + bend} ${ex + approach} ${ey} ${ex} ${ey}`;
    if (path.getAttribute("d") !== d) path.setAttribute("d", d);
  }
}

/** Progressive enhancement: server HTML and unsupported browsers stay readable. */
export function useLandingMotion(
  rootRef: RefObject<HTMLDivElement | null>,
  enabled: boolean,
  onActiveSection?: (section: string) => void,
) {
  useEffect(() => {
    const root = rootRef.current;
    if (!enabled || !root || !window.requestAnimationFrame) return;
    const sections = [
      ...root.querySelectorAll<HTMLElement>("[data-nav-section]"),
    ];
    const links = [
      ...root.querySelectorAll<HTMLAnchorElement>(
        ".m-gooey-list:not(.m-gooey-overlay) a[href^='#']",
      ),
    ];
    let frame = 0;
    const update = () => {
      frame = 0;
      const scrollRange =
        document.documentElement.scrollHeight - window.innerHeight;
      const progress =
        scrollRange > 0
          ? Math.min(1, Math.max(0, window.scrollY / scrollRange))
          : 0;
      const active = sections
        .filter(
          (section) =>
            section.getBoundingClientRect().top <= window.innerHeight * 0.4,
        )
        .at(-1)?.dataset.navSection;
      root.dataset.scrolled = String(window.scrollY > 24);
      root.style.setProperty("--m-reading-progress", String(progress));
      onActiveSection?.(active ?? "");
      links.forEach((link) => {
        if (link.hash === `#${active}`)
          link.setAttribute("aria-current", "location");
        else link.removeAttribute("aria-current");
      });
      updateHeroConnectors(root);
    };
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(update);
    };
    update();
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    root.addEventListener("load", schedule, true);
    const geometry = window.ResizeObserver
      ? new ResizeObserver(schedule)
      : null;
    root
      .querySelectorAll(".m-hero, .m-hero-stage, .m-hero-side")
      .forEach((node) => geometry?.observe(node));
    return () => {
      geometry?.disconnect();
      window.cancelAnimationFrame(frame);
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      root.removeEventListener("load", schedule, true);
      links.forEach((link) => link.removeAttribute("aria-current"));
      delete root.dataset.scrolled;
      root.style.removeProperty("--m-reading-progress");
    };
  }, [rootRef, enabled, onActiveSection]);

  useEffect(() => {
    const root = rootRef.current;
    if (!enabled || !root || !window.matchMedia) return;
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    let stop = () => {};
    const configure = () => {
      stop();
      if (preference.matches) return;
      const nodes = [...root.querySelectorAll<HTMLElement>("[data-reveal]")];
      let observer: IntersectionObserver | undefined;
      let pointerFrame = 0;
      let x = 0;
      let y = 0;
      const hero = root.querySelector<HTMLElement>(".m-hero");
      const stage = root.querySelector<HTMLElement>(".m-hero-stage");
      const compact = window.matchMedia("(max-width: 700px)");
      let scrollFrame = 0;
      let previousProgress = -1;
      let previousAmount = 0;
      const properties = [
        "--m-hero-copy-y",
        "--m-hero-copy-opacity",
        "--m-hero-portrait-y",
        "--m-hero-spread",
        "--m-hero-rise",
      ];
      const scrollHero = () => {
        scrollFrame = 0;
        if (!hero) return;
        const rect = hero.getBoundingClientRect();
        const progress =
          rect.height > 0
            ? Math.max(0, Math.min(1, -rect.top / (rect.height * 0.85)))
            : 0;
        const amount = compact.matches ? 0.25 : 1;
        if (progress === previousProgress && amount === previousAmount) return;
        previousProgress = progress;
        previousAmount = amount;
        hero.style.setProperty(
          "--m-hero-copy-y",
          `${progress * -40 * amount}px`,
        );
        hero.style.setProperty(
          "--m-hero-copy-opacity",
          String(1 - progress * 0.65 * amount),
        );
        hero.style.setProperty(
          "--m-hero-portrait-y",
          `${progress * -24 * amount}px`,
        );
        hero.style.setProperty(
          "--m-hero-spread",
          `${progress * 50 * amount}px`,
        );
        hero.style.setProperty("--m-hero-rise", `${progress * -44 * amount}px`);
        updateHeroConnectors(root);
      };
      const scheduleHero = () => {
        if (!scrollFrame)
          scrollFrame = window.requestAnimationFrame(scrollHero);
      };
      const resetArt = () => {
        if (pointerFrame) window.cancelAnimationFrame(pointerFrame);
        pointerFrame = 0;
        stage?.style.removeProperty("--m-hero-pointer-x");
        stage?.style.removeProperty("--m-hero-pointer-y");
      };
      const move = (event: PointerEvent) => {
        if (event.pointerType !== "mouse" || !hero || !stage) return;
        const rect = hero.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        x = Math.max(
          -1,
          Math.min(1, ((event.clientX - rect.left) / rect.width) * 2 - 1),
        );
        y = Math.max(
          -1,
          Math.min(1, ((event.clientY - rect.top) / rect.height) * 2 - 1),
        );
        if (!pointerFrame)
          pointerFrame = window.requestAnimationFrame(() => {
            pointerFrame = 0;
            stage.style.setProperty("--m-hero-pointer-x", `${x * 10}px`);
            stage.style.setProperty("--m-hero-pointer-y", `${y * 6}px`);
          });
      };
      // Only arm content below the fold, and only after the observer exists.
      if (window.IntersectionObserver) {
        observer = new IntersectionObserver(
          (entries) => {
            entries.forEach((entry) => {
              if (!entry.isIntersecting) return;
              (entry.target as HTMLElement).dataset.revealed = "true";
              observer?.unobserve(entry.target);
            });
          },
          { threshold: 0, rootMargin: "0px 0px -6% 0px" },
        );
        nodes.forEach((node) => {
          if (node.getBoundingClientRect().top >= window.innerHeight * 0.94) {
            node.dataset.revealed = "false";
            observer!.observe(node);
          }
        });
      }
      root.dataset.motion = "ready";
      if (hero && typeof window.requestAnimationFrame === "function") {
        scrollHero();
        window.addEventListener("scroll", scheduleHero, { passive: true });
        window.addEventListener("resize", scheduleHero);
      }
      if (
        typeof window.requestAnimationFrame === "function" &&
        window.matchMedia("(hover: hover) and (pointer: fine)").matches
      ) {
        hero?.addEventListener("pointermove", move, { passive: true });
        hero?.addEventListener("pointerleave", resetArt);
        hero?.addEventListener("pointercancel", resetArt);
      }
      const revealTarget = (target: Element) => {
        for (const node of nodes) {
          if (
            node === target ||
            node.contains(target) ||
            target.contains(node)
          ) {
            node.dataset.revealed = "true";
            observer?.unobserve(node);
          }
        }
      };
      const followHash = () => {
        // Anchor navigation and keyboard focus must not wait on an entrance.
        let target: HTMLElement | null = null;
        try {
          target = document.getElementById(
            decodeURIComponent(window.location.hash.slice(1)),
          );
        } catch {
          return;
        }
        if (!target || !root.contains(target)) return;
        revealTarget(target);
      };
      const revealFocus = (event: FocusEvent) => {
        if (event.target instanceof Element) revealTarget(event.target);
      };
      window.addEventListener("hashchange", followHash);
      root.addEventListener("focusin", revealFocus);
      followHash();
      stop = () => {
        observer?.disconnect();
        resetArt();
        if (scrollFrame) window.cancelAnimationFrame(scrollFrame);
        window.removeEventListener("scroll", scheduleHero);
        window.removeEventListener("resize", scheduleHero);
        properties.forEach((property) => hero?.style.removeProperty(property));
        updateHeroConnectors(root);
        hero?.removeEventListener("pointermove", move);
        hero?.removeEventListener("pointerleave", resetArt);
        hero?.removeEventListener("pointercancel", resetArt);
        window.removeEventListener("hashchange", followHash);
        root.removeEventListener("focusin", revealFocus);
        nodes.forEach((node) => delete node.dataset.revealed);
        delete root.dataset.motion;
      };
    };
    configure();
    preference.addEventListener("change", configure);
    return () => {
      preference.removeEventListener("change", configure);
      stop();
    };
  }, [rootRef, enabled]);
}
