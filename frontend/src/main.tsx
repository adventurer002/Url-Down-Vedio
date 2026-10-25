import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { WorkspaceProvider } from "./hooks/useWorkspace";
import "./style.css";
import "./subscription.css";
import "./knowledge.css";

createRoot(document.getElementById("app")!).render(
  <StrictMode>
    <BrowserRouter>
      <WorkspaceProvider>
        <App />
      </WorkspaceProvider>
    </BrowserRouter>
  </StrictMode>,
);
