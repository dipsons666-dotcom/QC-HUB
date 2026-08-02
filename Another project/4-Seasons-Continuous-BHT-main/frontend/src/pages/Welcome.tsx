import { useEffect, useMemo, useState } from "react";
import { Navigate, useLocation, useNavigate, useParams } from "react-router-dom";
import { motion } from "framer-motion";
import { categories, getCategoryBrandLogo, isAdminRestrictedCategory, isMaltCategorySlug } from "@/data/categories";
import { Button } from "@/components/ui/button";
import { setDashboardSidebarHoverColor } from "@/lib/dashboardTheme";
import { useAuth } from "@/contexts/AuthContext";

type FloatingItem = {
  id: string;
  x: number;
  y: number;
  dx: number;
  dy: number;
  size: number;
  kind: "image" | "text";
  src?: string;
  label?: string;
  color?: string;
};

function FloatingImage({ src, size }: { src: string; size: number }) {
  const [processedSrc, setProcessedSrc] = useState(src);

  useEffect(() => {
    let active = true;
    const img = new Image();
    img.src = src;
    img.onload = () => {
      try {
        const canvas = document.createElement("canvas");
        canvas.width = img.naturalWidth || img.width;
        canvas.height = img.naturalHeight || img.height;
        const ctx = canvas.getContext("2d");
        if (!ctx) {
          if (active) setProcessedSrc(src);
          return;
        }

        ctx.drawImage(img, 0, 0);
        const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const data = imageData.data;

        for (let i = 0; i < data.length; i += 4) {
          const r = data[i];
          const g = data[i + 1];
          const b = data[i + 2];
          const max = Math.max(r, g, b);
          const min = Math.min(r, g, b);
          const avg = (r + g + b) / 3;
          const saturation = max - min;

          // Remove near-white, low-saturation pixels (typical plain white backgrounds).
          if (avg > 238 && saturation < 24) {
            data[i + 3] = 0;
          }
        }

        ctx.putImageData(imageData, 0, 0);
        const out = canvas.toDataURL("image/png");
        if (active) setProcessedSrc(out);
      } catch {
        if (active) setProcessedSrc(src);
      }
    };
    img.onerror = () => {
      if (active) setProcessedSrc(src);
    };

    return () => {
      active = false;
    };
  }, [src]);

  return (
    <img
      src={processedSrc}
      alt=""
      style={{
        width: `${size}px`,
        height: `${size}px`,
        objectFit: "contain",
      }}
    />
  );
}

const FLOATING_CONTENT_BY_SLUG: Record<string, { imageFiles: string[]; texts: string[] }> = {
  noodles: {
    imageFiles: [
      "1BrvrEUGq1FVUaE67Y0Em6AhSbHqnGCVYt3hMVLW.jpeg",
      "cherie-noodles.webp",
      "Chikki Chicken Instant Noodles (100g).jpg",
      "indomie-mi-goreng-special_detail.png",
      "NoodlessHero.webp",
      "SPRSI72510.avif",
    ],
    texts: ["Indomie", "Golden Penny", "Dangote Noodles", "Honeywell", "Mimee", "Chikki"],
  },
  "breakfast-cereals": {
    imageFiles: ["bc001.webp", "bc002.webp", "bc003.webp", "bc004.webp", "bc005.webp", "bc006.jpg"],
    texts: [
      "Blue Boat",
      "Candia Cereal",
      "Frosted Flakes",
      "Golden Morn",
      "Good Morning Cornflakes",
      "Kellogg's Coco Pops",
      "Kellogg's Rice Krispies",
      "Kellogg's Cornflakes",
      "Quaker Oats",
      "Nestle Milo Cereal",
      "NASCO Cornflakes",
      "Amazing Day",
    ],
  },
  toothpaste: {
    imageFiles: [
      "tp001.jpg",
      "tp002.jpg",
      "tp003.svg",
      "tp004.webp",
      "tp005.png",
      "tp006.webp",
      "tp007.svg",
      "tp008.jpg",
      "tp009.avif",
      "tp010.jpg",
      "tp011.avif",
      "tp012.webp",
    ],
    texts: [],
  },
  bleach: {
    imageFiles: ["bl001.png", "bl002.png", "bl003.png", "bl004.jpg", "bl005.jpg", "bl006.jpg", "bl007.jpg", "bloo4.png"],
    texts: [],
  },
  "wet-hair": {
    imageFiles: [
      "wh001.png",
      "wh002.webp",
      "wh003.jpg",
      "wh004.jpg",
      "wh005.png",
      "wh005.webp",
      "wh006.avif",
      "wh007.webp",
      "wh008.jpg",
      "wh009.webp",
      "wh010.jpg",
      "wh011.webp",
      "wh012.jpg",
      "wh013.png",
    ],
    texts: [],
  },
  "dry-hair": {
    imageFiles: [
      "wh001.png",
      "wh002.webp",
      "wh003.jpg",
      "wh004.jpg",
      "wh005.png",
      "wh005.webp",
      "wh006.avif",
      "wh007.webp",
      "wh008.jpg",
      "wh009.webp",
      "wh010.jpg",
      "wh011.webp",
      "wh012.jpg",
      "wh013.png",
    ],
    texts: [],
  },
  "condiment-mixes": {
    imageFiles: [
      "cm001.avif",
      "cm002.png",
      "cm003.png",
      "cm004.avif",
      "cm005.jpg",
      "cm006.avif",
      "cm007.jpg",
      "cm008.png",
      "cm009.jpg",
      "cm010.jpg",
      "cm011.webp",
      "cm012.png",
      "cm013.avif",
      "cm014.jpg",
      "cm015.png",
    ],
    texts: [],
  },
  malt: {
    imageFiles: [
      "ml001.png",
      "ml002.png",
      "ml003.png",
      "ml004.png",
      "ml005.webp",
      "ml006.png",
      "ml007.webp",
      "ml008.png",
      "ml009.png",
      "ml010.png",
      "ml011.webp",
      "ml012.png",
      "ml013.webp",
      "ml014.jpg",
      "ml015.avif",
      "ml016.jpg",
    ],
    texts: [],
  },
  snacks: {
    imageFiles: ["sk001.png", "sk002.jpg", "sk002.png", "sk004.jpg", "sk005.jpg"],
    texts: [
      "Gala",
      "Minimie Chinchin",
      "Munch It",
      "Rite",
      "Bigi Snacks",
      "Mr Bigg's",
      "Supreme",
      "Fan",
      "Others Specify...",
      "None",
    ],
  },
  "edible-oil": {
    imageFiles: [
      "eo001.png",
      "eo002.webp",
      "eo003.jpg",
      "eo004.jpg",
      "eo005.png",
      "eo006.webp",
      "eo007.jpg",
      "eo008.jpg",
      "eo009.jpg",
      "eo010.webp",
      "eo011.jpg",
      "eo012.jpeg",
      "eo013.avif",
      "eo014.webp",
    ],
    texts: [],
  },
  "toilet-cleaner": {
    imageFiles: ["tc001.jpg", "tc002.jpg", "tc003.png", "tc004.webp", "tc005.jpg", "tc006.jpg", "tc007.jpg", "tc008.avif", "tc009.jpg"],
    texts: [],
  },
};

const colors = ["#0b5fa5", "#2f7d32", "#9a6700", "#0369a1", "#7c3aed", "#be123c"];

function buildFloatingItems(slug: string): FloatingItem[] {
  const fallback = FLOATING_CONTENT_BY_SLUG.noodles;
  const selected = FLOATING_CONTENT_BY_SLUG[slug] || fallback;
  const imageFolder =
    slug === "breakfast-cereals"
      ? "breakfast_cereals"
      : slug === "wet-hair"
        ? "wet_hair"
        : slug === "dry-hair"
          ? "dry_hair"
          : slug === "condiment-mixes"
            ? "condiment_mixes"
            : slug === "edible-oil"
              ? "edible_oil"
              : slug === "toilet-cleaner"
                ? "toilet_cleaners"
            : slug;
  const imageSources = selected.imageFiles.map((file) => `/${imageFolder}/${encodeURIComponent(file)}`);

  const images = imageSources.map((src, idx) => ({
    id: `image-${idx}`,
    x: Math.random() * 100,
    y: Math.random() * 100,
    dx: (Math.random() * 0.35 + 0.14) * (Math.random() > 0.5 ? 1 : -1),
    dy: (Math.random() * 0.35 + 0.14) * (Math.random() > 0.5 ? 1 : -1),
    size: Math.random() * 64 + 64,
    kind: "image" as const,
    src,
  }));

  const texts = selected.texts.map((label, idx) => ({
    id: `text-${idx}`,
    x: Math.random() * 100,
    y: Math.random() * 100,
    dx: (Math.random() * 0.3 + 0.12) * (Math.random() > 0.5 ? 1 : -1),
    dy: (Math.random() * 0.3 + 0.12) * (Math.random() > 0.5 ? 1 : -1),
    size: Math.random() * 14 + 13,
    kind: "text" as const,
    label,
    color: colors[idx % colors.length],
  }));

  return [...images, ...texts];
}

function FloatingLayer({ slug }: { slug: string }) {
  const [items, setItems] = useState<FloatingItem[]>(() => buildFloatingItems(slug));

  useEffect(() => {
    setItems(buildFloatingItems(slug));
  }, [slug]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setItems((prev) =>
        prev.map((item) => {
          const jitterX = (Math.random() - 0.5) * 0.015;
          const jitterY = (Math.random() - 0.5) * 0.015;
          let ndx = item.dx + jitterX;
          let ndy = item.dy + jitterY;
          const speed = Math.hypot(ndx, ndy);
          const minSpeed = 0.05;
          const maxSpeed = 0.24;
          if (speed > maxSpeed) {
            ndx = (ndx / speed) * maxSpeed;
            ndy = (ndy / speed) * maxSpeed;
          }
          if (speed < minSpeed) {
            const ratio = minSpeed / (speed || 0.001);
            ndx *= ratio;
            ndy *= ratio;
          }

          let nx = item.x + ndx;
          let ny = item.y + ndy;

          if (nx < -2) nx = 102;
          if (nx > 102) nx = -2;
          if (ny < -2) ny = 102;
          if (ny > 102) ny = -2;

          return { ...item, x: nx, y: ny, dx: ndx, dy: ndy };
        }),
      );
    }, 56);

    return () => window.clearInterval(timer);
  }, []);

  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden">
      {items.map((item) => (
        <div
          key={item.id}
          className="absolute select-none"
          style={{
            left: `${item.x}%`,
            top: `${item.y}%`,
            transform: "translate(-50%, -50%)",
            opacity: item.kind === "image" ? 0.42 : 0.72,
          }}
        >
          {item.kind === "image" ? (
            <FloatingImage src={item.src || ""} size={item.size} />
          ) : (
            <span
              style={{
                color: item.color,
                fontSize: `${item.size}px`,
                fontWeight: 700,
                textShadow: "0 1px 2px rgba(255,255,255,0.9), 0 1px 3px rgba(15,23,42,0.2)",
              }}
            >
              {item.label}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}

export default function Welcome() {
  const navigate = useNavigate();
  const location = useLocation();
  const { user } = useAuth();
  const { slug = "" } = useParams();

  const category = useMemo(() => categories.find((item) => item.slug === slug), [slug]);

  const isAdminRoute = location.pathname.startsWith("/admin/");
  const isAdminUser = user?.role === "admin";
  const partnerLogo = getCategoryBrandLogo(slug);
  const isMalt = isMaltCategorySlug(slug);
  const handleExploreDashboard = () => navigate(`${isAdminRoute || isAdminUser ? "/admin/dashboard" : "/dashboard"}/${slug}`);

  if ((isAdminRoute || isAdminUser) && isAdminRestrictedCategory(slug)) {
    return <Navigate to="/admin/categories" replace />;
  }

  if (!category) {
    return (
      <main className="grid min-h-screen place-items-center bg-slate-950 text-white">
        <div className="text-center">
          <h1 className="text-2xl font-bold">Category not found</h1>
          <Button className="mt-4" onClick={() => navigate("/")}>
            Back to Login
          </Button>
        </div>
      </main>
    );
  }

  return (
    <motion.main
      initial={{ opacity: 0, y: 8, scale: 0.995 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -6, scale: 0.99 }}
      transition={{ duration: 0.42, ease: [0.22, 1, 0.36, 1] }}
      className="relative min-h-screen overflow-hidden bg-[#f0f3f7] text-slate-900"
    >
      <FloatingLayer slug={slug} />
      <section className="relative z-10 mx-auto flex min-h-screen w-full max-w-3xl flex-col px-4 pb-8 text-center sm:px-6">
        <div className="w-full pt-12 sm:pt-14">
          <div className="mb-3 flex items-center justify-center gap-3 sm:mb-4 sm:gap-6">
            <img src="/infinity-logo.png" alt="Infinity logo" className="h-14 w-auto object-contain sm:h-20" />
            <img src={partnerLogo.src} alt={partnerLogo.alt} className="h-14 w-auto object-contain sm:h-20" />
          </div>
          <div className="mx-auto mb-2 h-[2px] w-72 bg-slate-300/80 sm:w-96" />
          <p className={`text-xl font-semibold tracking-wide sm:text-3xl ${isMalt ? "text-[#C8A44D]" : "text-slate-700"}`}>
            4 Seasons Brand Health Tracking
          </p>
        </div>

        <div className="flex flex-1 flex-col items-center justify-center">
          <h1 className={`mb-2 break-words text-[clamp(2rem,9vw,3rem)] font-black tracking-tight ${isMalt ? "text-[#C8A44D]" : ""}`}>
            {category.category}
          </h1>
          <p className={`mb-4 text-base font-semibold sm:mb-6 sm:text-lg ${isMalt ? "text-[#8c6a20]" : "text-[#009FE3]"}`}>
            Market Research Dashboard
          </p>
          <p className="mb-8 max-w-2xl text-base text-slate-700 sm:mb-10 sm:text-lg">{category.description}</p>

          <Button
            onMouseEnter={setDashboardSidebarHoverColor}
            onFocus={setDashboardSidebarHoverColor}
            onClick={handleExploreDashboard}
            className="h-12 rounded-xl bg-cyan-500 px-6 text-base font-semibold text-slate-900 shadow-lg shadow-cyan-400/30 hover:bg-cyan-400 sm:h-14 sm:px-8 sm:text-lg"
          >
            Explore Dashboard
          </Button>
        </div>
      </section>
    </motion.main>
  );
}
