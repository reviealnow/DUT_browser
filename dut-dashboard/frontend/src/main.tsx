import React from "react";
import { createRoot } from "react-dom/client";

import Dashboard from "./pages/Dashboard";
import ErrorBoundary from "./ErrorBoundary";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <Dashboard />
    </ErrorBoundary>
  </React.StrictMode>,
);
