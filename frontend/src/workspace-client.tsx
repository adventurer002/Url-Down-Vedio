import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { WorkspaceProvider } from "./hooks/useWorkspace";
import "./style.css";
import "./subscription.css";
import "./knowledge.css";
import "./workspace.css";
import "./desktop-typography.css";
import "./account.css";
import "./accent.css";
import "./content-workspace.css";

createRoot(document.getElementById("app")!).render(
  <StrictMode>
    <BrowserRouter>
      <WorkspaceProvider>
        <App />
      </WorkspaceProvider>
    </BrowserRouter>
  </StrictMode>,
);
