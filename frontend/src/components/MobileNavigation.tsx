import { useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import Icon from "./Icon";

export default function MobileNavigation() {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const { pathname } = useLocation();
  useEffect(() => setOpen(false), [pathname]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  return (
    <div
      className="mobile-navigation"
      ref={root}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          setOpen(false);
          trigger.current?.focus();
        }
      }}
    >
      <button
        ref={trigger}
        className="icon-button"
        aria-label="更多导航"
        aria-expanded={open}
        aria-controls="mobile-navigation-links"
        onClick={() => setOpen(!open)}
      >
        <Icon name="menu" size={19} />
      </button>
      {open && (
        <nav id="mobile-navigation-links" aria-label="网站导航">
          {[
            ["/account", "个人中心"],
            ["/library", "我的知识库"],
            ["/subscription", "订阅方案"],
            ["/help", "使用帮助"],
            ["/privacy", "隐私说明"],
            ["/settings", "设置"],
          ].map(([path, label]) => (
            <Link
              key={path}
              to={path}
              onClick={() => setOpen(false)}
              aria-current={pathname === path ? "page" : undefined}
            >
              {label}
            </Link>
          ))}
        </nav>
      )}
    </div>
  );
}
