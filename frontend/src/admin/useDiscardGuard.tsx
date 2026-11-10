import { useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";

export function useDiscardGuard(dirty: boolean) {
  const action = useRef<() => void>(() => {});
  const allowUnload = useRef(false);
  const [open, setOpen] = useState(false);
  function requestDiscard(next: () => void) {
    if (!dirty) return next();
    action.current = next;
    setOpen(true);
  }
  useEffect(() => {
    if (!dirty) return;
    allowUnload.current = false;
    const unload = (event: BeforeUnloadEvent) => {
      if (allowUnload.current) return;
      event.preventDefault();
      event.returnValue = "";
    };
    const navigate = (event: MouseEvent) => {
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      )
        return;
      const anchor =
        event.target instanceof Element
          ? event.target.closest("a[href]")
          : null;
      if (
        !(anchor instanceof HTMLAnchorElement) ||
        anchor.target === "_blank" ||
        anchor.hasAttribute("download")
      )
        return;
      const target = new URL(anchor.href);
      if (
        target.origin === location.origin &&
        target.pathname === location.pathname &&
        target.search === location.search
      )
        return;
      event.preventDefault();
      event.stopPropagation();
      action.current = () => location.assign(target.href);
      setOpen(true);
    };
    window.addEventListener("beforeunload", unload);
    document.addEventListener("click", navigate, true);
    return () => {
      window.removeEventListener("beforeunload", unload);
      document.removeEventListener("click", navigate, true);
    };
  }, [dirty]);
  return {
    requestDiscard,
    discardDialog: (
      <Dialog.Root open={open} onOpenChange={setOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="admin-overlay admin-model-discard-overlay" />
          <div className="admin-modal-viewport admin-model-discard-viewport">
            <Dialog.Content className="admin-dialog admin-model-discard-dialog">
              <Dialog.Title>放弃未保存的修改？</Dialog.Title>
              <Dialog.Description>
                当前修改尚未保存。你可以继续编辑，或放弃修改后离开。
              </Dialog.Description>
              <div className="admin-detail-actions">
                <button className="admin-button" onClick={() => setOpen(false)}>
                  继续编辑
                </button>
                <button
                  className="admin-button admin-button-danger"
                  onClick={() => {
                    allowUnload.current = true;
                    setOpen(false);
                    action.current();
                  }}
                >
                  放弃修改
                </button>
              </div>
            </Dialog.Content>
          </div>
        </Dialog.Portal>
      </Dialog.Root>
    ),
  };
}
