import { useEffect } from "react";

/** Runs after the destination's lazy content is ready, inside its Suspense boundary. */
export default function PageReady() {
  useEffect(() => {
    const previous = document.activeElement;
    const frame = requestAnimationFrame(() => {
      // Native dialogs own keyboard focus until the user dismisses them.
      const main = document.getElementById("main");
      const focused = document.activeElement;
      if (
        !document.querySelector("dialog[open]") &&
        (focused === previous || focused === document.body) &&
        (!focused || !main?.contains(focused))
      )
        main?.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
  }, []);
  return null;
}
