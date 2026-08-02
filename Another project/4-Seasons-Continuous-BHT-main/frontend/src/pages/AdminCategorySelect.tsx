import { useMemo } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { motion } from "framer-motion";
import { categories, getAdminAccessibleCategories } from "@/data/categories";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/contexts/AuthContext";

export default function AdminCategorySelect() {
  const navigate = useNavigate();
  const { user, setSelectedAdminCategory } = useAuth();
  const sortedCategories = useMemo(
    () => [...getAdminAccessibleCategories()].sort((a, b) => a.category.localeCompare(b.category)),
    [],
  );

  if (!user || user.role !== "admin") {
    return <Navigate to="/" replace />;
  }

  const handleSelect = (slug: string) => {
    const selected = categories.find((item) => item.slug === slug) || null;
    setSelectedAdminCategory(selected);
    navigate(`/admin/welcome/${slug}`);
  };

  return (
    <motion.main
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
      className="min-h-screen bg-[#f0f3f7] px-3 py-6 text-slate-900 sm:px-6 sm:py-10"
    >
      <section className="mx-auto max-w-6xl">
        <div className="mb-6 text-center sm:mb-8">
          <h1 className="text-2xl font-bold text-slate-900 sm:text-3xl">Admin Category Selection</h1>
          <p className="mt-2 text-sm text-slate-600">Select a category to open its welcome page and dashboard.</p>
        </div>

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {sortedCategories.map((item) => (
            <article
              key={item.slug}
              className="rounded-2xl border border-white/60 bg-white/75 p-5 shadow-[0_8px_30px_rgba(15,23,42,0.08)] backdrop-blur-md"
            >
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{item.slug}</p>
              <h2 className="mt-1 text-xl font-bold text-slate-900">{item.category}</h2>
              <p className="mt-2 line-clamp-3 text-sm text-slate-600">{item.description}</p>
              <Button
                className="mt-4 h-10 w-full rounded-xl bg-[#009FE3] font-semibold text-white hover:bg-[#008fcc]"
                onClick={() => handleSelect(item.slug)}
              >
                Open {item.category}
              </Button>
            </article>
          ))}
        </div>
      </section>
    </motion.main>
  );
}
