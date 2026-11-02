import {
  createContext,
  useContext,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { createWorkspace, type Workspace } from "../stores/workspace";

const WorkspaceContext = createContext<Workspace | null>(null);
export function WorkspaceProvider({
  children,
  store: initialStore,
}: {
  children: ReactNode;
  store?: Workspace;
}) {
  const [store] = useState(() => initialStore ?? createWorkspace());
  return (
    <WorkspaceContext.Provider value={store}>
      {children}
    </WorkspaceContext.Provider>
  );
}
export function useWorkspaceStore() {
  const store = useContext(WorkspaceContext);
  if (!store) throw new Error("WorkspaceProvider is required");
  return store;
}
export function useWorkspaceValue<T>(select: (store: Workspace) => T) {
  const store = useWorkspaceStore();
  return useSyncExternalStore(
    store.subscribe,
    () => select(store),
    () => select(store),
  );
}
export function useWorkspace() {
  const store = useWorkspaceStore();
  useSyncExternalStore(store.subscribe, store.getVersion, store.getVersion);
  return store;
}
