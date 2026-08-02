import React, { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { CategoryCredential, categories, isAdminRestrictedCategory } from "@/data/categories";

type AuthUser =
  | { role: "category"; category: CategoryCredential }
  | { role: "admin"; username: string };

interface AuthContextType {
  user: AuthUser | null;
  selectedAdminCategory: CategoryCredential | null;
  loginCategory: (user: CategoryCredential) => void;
  loginAdmin: (username?: string) => void;
  setSelectedAdminCategory: (category: CategoryCredential | null) => void;
  setSelectedAdminCategoryBySlug: (slug: string) => void;
  logout: () => void;
}

const AuthContext = createContext<AuthContextType | null>(null);
const AUTH_STORAGE_KEY = "inicio.auth.v1";
const ADMIN_CATEGORY_STORAGE_KEY = "inicio.admin.category.v1";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(() => {
    try {
      const raw = window.localStorage.getItem(AUTH_STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as { role?: string; username?: string; slug?: string };
      if (parsed?.role === "admin" && parsed.username) return { role: "admin", username: parsed.username };
      if (parsed?.role === "category" && parsed.slug) {
        const found = categories.find((item) => item.slug === parsed.slug);
        if (found) return { role: "category", category: found };
      }
      return null;
    } catch {
      return null;
    }
  });
  const [selectedAdminCategory, setSelectedAdminCategory] = useState<CategoryCredential | null>(() => {
    try {
      const slug = window.localStorage.getItem(ADMIN_CATEGORY_STORAGE_KEY);
      if (!slug) return null;
      if (isAdminRestrictedCategory(slug)) return null;
      return categories.find((item) => item.slug === slug) || null;
    } catch {
      return null;
    }
  });

  const loginCategory = (u: CategoryCredential) => {
    setUser({ role: "category", category: u });
    setSelectedAdminCategory(null);
  };
  const loginAdmin = (username = "admin") => {
    setUser({ role: "admin", username });
    setSelectedAdminCategory(null);
  };
  const setSelectedAdminCategoryBySlug = (slug: string) => {
    if (isAdminRestrictedCategory(slug)) {
      setSelectedAdminCategory(null);
      return;
    }
    const matched = categories.find((item) => item.slug === slug) || null;
    setSelectedAdminCategory(matched);
  };
  const logout = () => {
    setUser(null);
    setSelectedAdminCategory(null);
  };

  useEffect(() => {
    try {
      if (!user) {
        window.localStorage.removeItem(AUTH_STORAGE_KEY);
        return;
      }
      if (user.role === "admin") {
        window.localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify({ role: "admin", username: user.username }));
      } else {
        window.localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify({ role: "category", slug: user.category.slug }));
      }
    } catch {
      // noop
    }
  }, [user]);

  useEffect(() => {
    try {
      if (!selectedAdminCategory) {
        window.localStorage.removeItem(ADMIN_CATEGORY_STORAGE_KEY);
        return;
      }
      window.localStorage.setItem(ADMIN_CATEGORY_STORAGE_KEY, selectedAdminCategory.slug);
    } catch {
      // noop
    }
  }, [selectedAdminCategory]);

  return (
    <AuthContext.Provider
      value={{
        user,
        selectedAdminCategory,
        loginCategory,
        loginAdmin,
        setSelectedAdminCategory,
        setSelectedAdminCategoryBySlug,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
