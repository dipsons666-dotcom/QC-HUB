import { Toaster } from "@/components/ui/toaster";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AnimatePresence } from "framer-motion";
import { BrowserRouter, Routes, Route, useLocation } from "react-router-dom";
import { AuthProvider } from "@/contexts/AuthContext";
import Login from "./pages/Login";
import AdminCategorySelect from "./pages/AdminCategorySelect";
import Welcome from "./pages/Welcome";
import Dashboard from "./pages/Dashboard";
import MetadataReview from "./pages/MetadataReview";
import NotFound from "./pages/NotFound";

const queryClient = new QueryClient();

function getRouteAnimationKey(pathname: string): string {
  const parts = pathname.split("/").filter(Boolean);
  if (parts[0] === "dashboard" && parts[1]) {
    return `/dashboard/${parts[1]}`;
  }
  if (parts[0] === "admin" && parts[1] === "dashboard" && parts[2]) {
    return `/admin/dashboard/${parts[2]}`;
  }
  return pathname;
}

function GlobalFooter({ className = "" }: { className?: string }) {
  return (
    <footer className={`relative z-40 bg-transparent py-3 text-xs font-medium text-slate-700 ${className}`}>
      <div className="mx-auto max-w-7xl text-center">
        &copy; 2026 INICIO Tech Team. All rights reserved.
      </div>
    </footer>
  );
}

function AppRoutes() {
  const location = useLocation();
  const animationKey = getRouteAnimationKey(location.pathname);
  const isDashboardRoute = location.pathname.startsWith("/dashboard/") || location.pathname.startsWith("/admin/dashboard/");

  return (
    <div className="min-h-screen flex flex-col bg-[#f0f3f7]">
      <AnimatePresence mode="wait" initial={false}>
        <div className="flex-1">
          <Routes location={location} key={animationKey}>
            <Route path="/" element={<Login />} />
            <Route path="/admin/categories" element={<AdminCategorySelect />} />
            <Route path="/admin/metadata-review" element={<MetadataReview />} />
            <Route path="/admin/welcome/:slug" element={<Welcome />} />
            <Route path="/admin/dashboard/:slug" element={<Dashboard />} />
            <Route path="/admin/dashboard/:slug/*" element={<Dashboard />} />
            <Route path="/welcome/:slug" element={<Welcome />} />
            <Route path="/dashboard/:slug" element={<Dashboard />} />
            <Route path="/dashboard/:slug/*" element={<Dashboard />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </div>
      </AnimatePresence>
      <GlobalFooter className={isDashboardRoute ? "md:ml-[18rem]" : ""} />
    </div>
  );
}

const App = () => (
  <QueryClientProvider client={queryClient}>
    <TooltipProvider>
      <AuthProvider>
        <Toaster />
        <Sonner />
        <BrowserRouter
          future={{
            v7_startTransition: true,
            v7_relativeSplatPath: true,
          }}
        >
          <AppRoutes />
        </BrowserRouter>
      </AuthProvider>
    </TooltipProvider>
  </QueryClientProvider>
);

export default App;
