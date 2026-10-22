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
export function useWorkspace() {
  const store = useContext(WorkspaceContext);
  if (!store) throw new Error("WorkspaceProvider is required");
  useSyncExternalStore(store.subscribe, store.getVersion, store.getVersion);
  return store;
}
