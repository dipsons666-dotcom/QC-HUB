import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { motion } from "framer-motion";
import { Eye, EyeOff, User, Lock } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { authenticateAdmin, authenticateUser, categories } from "@/data/categories";
import { useAuth } from "@/contexts/AuthContext";

type FloatingCategory = {
  id: string;
  label: string;
  x: number;
  y: number;
  dx: number;
  dy: number;
  size: number;
  color: string;
};

const FLOATING_COLORS = ["#009FE3", "#2ca44f", "#d39c2f", "#0ea5e9", "#16a34a", "#1d4ed8"];

function buildFloatingCategories(labels: string[]): FloatingCategory[] {
  return labels.map((label, idx) => ({
    id: `category-${idx}`,
    label,
    x: Math.random() * 100,
    y: Math.random() * 100,
    dx: (Math.random() * 0.25 + 0.1) * (Math.random() > 0.5 ? 1 : -1),
    dy: (Math.random() * 0.25 + 0.1) * (Math.random() > 0.5 ? 1 : -1),
    size: Math.random() * 10 + 18,
    color: FLOATING_COLORS[idx % FLOATING_COLORS.length],
  }));
}

const Login = () => {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const { loginCategory, loginAdmin } = useAuth();
  const categoryLabels = useMemo(() => categories.map((item) => item.category), []);
  const [floatingCategories, setFloatingCategories] = useState<FloatingCategory[]>(() =>
    buildFloatingCategories(categoryLabels),
  );

  useEffect(() => {
    setFloatingCategories(buildFloatingCategories(categoryLabels));
  }, [categoryLabels]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setFloatingCategories((prev) =>
        prev.map((item) => {
          const jitterX = (Math.random() - 0.5) * 0.015;
          const jitterY = (Math.random() - 0.5) * 0.015;
          let ndx = item.dx + jitterX;
          let ndy = item.dy + jitterY;
          const speed = Math.hypot(ndx, ndy);
          const minSpeed = 0.04;
          const maxSpeed = 0.18;

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
          if (nx < -5) nx = 105;
          if (nx > 105) nx = -5;
          if (ny < -5) ny = 105;
          if (ny > 105) ny = -5;

          return { ...item, x: nx, y: ny, dx: ndx, dy: ndy };
        }),
      );
    }, 56);

    return () => window.clearInterval(timer);
  }, []);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    setTimeout(() => {
      const admin = authenticateAdmin(username, password);
      if (admin) {
        loginAdmin(admin.username);
        navigate("/admin/categories");
        setLoading(false);
        return;
      }
      const categoryUser = authenticateUser(username, password);
      if (categoryUser) {
        loginCategory(categoryUser);
        navigate(`/welcome/${categoryUser.slug}`);
        setLoading(false);
      } else {
        setError("Invalid username or password");
        setLoading(false);
      }
    }, 800);
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -4, scale: 0.99 }}
      transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
      className="relative flex min-h-screen flex-col overflow-hidden bg-[#f0f3f7] text-slate-900"
    >
      {/* Abstract background shapes */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -top-40 -right-40 h-96 w-96 rounded-full bg-sky-400/15 blur-3xl" />
        <div className="absolute -bottom-40 -left-40 h-96 w-96 rounded-full bg-emerald-400/10 blur-3xl" />
        <div className="absolute left-1/4 top-1/3 h-64 w-64 rounded-full bg-amber-300/15 blur-2xl" style={{ animation: "pulse-glow 4s ease-in-out infinite" }} />
        <div className="absolute bottom-1/4 right-1/3 h-48 w-48 rounded-full bg-sky-300/20 blur-2xl" style={{ animation: "pulse-glow 4s ease-in-out infinite 2s" }} />
      </div>
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        {floatingCategories.map((item) => (
          <span
            key={item.id}
            className="absolute select-none font-semibold uppercase tracking-wide"
            style={{
              left: `${item.x}%`,
              top: `${item.y}%`,
              transform: "translate(-50%, -50%)",
              fontSize: `${item.size}px`,
              color: item.color,
              opacity: 0.42,
              textShadow: "0 1px 8px rgba(248,250,252,0.65), 0 1px 4px rgba(15,23,42,0.16)",
            }}
          >
            {item.label}
          </span>
        ))}
      </div>

      {/* Header */}
      <motion.div
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6 }}
        className="relative z-10 flex flex-col items-center justify-center px-4 pt-12 pb-3 sm:pt-14 sm:pb-4"
      >
        <div className="flex flex-col items-center gap-2 sm:gap-3">
          <img src="/infinity-logo.png" alt="Infinity logo" className="h-16 w-auto object-contain sm:h-24" />
          <p className="text-base font-bold uppercase tracking-[0.24em] text-slate-800 sm:text-xl">INICIO INSIGHTS</p>
        </div>
      </motion.div>

      {/* Main content */}
      <div className="relative z-10 flex flex-1 items-center justify-center px-3 sm:px-4">
        <div className="flex w-full max-w-5xl flex-col items-center gap-8 sm:gap-10 lg:flex-row lg:gap-16">
          {/* Left side - Welcome text */}
          <motion.div
            initial={{ opacity: 0, x: -40 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.7, delay: 0.2 }}
            className="flex-1 text-center lg:text-left"
          >
            <h2 className="mb-3 text-3xl font-bold leading-tight text-slate-900 sm:mb-4 sm:text-4xl lg:text-5xl">
              Welcome to<br />
              <span className="text-gradient-cyan">4 Seasons Consumer Tracker</span>
            </h2>
            <p className="mx-auto max-w-md text-base text-slate-700 sm:text-lg lg:mx-0">
              Access comprehensive market research dashboards with real-time insights across multiple FMCG categories.
            </p>
            {/* <div className="mt-8 flex gap-6 justify-center lg:justify-start">
              {["📊", "📈", "🎯"].map((emoji, i) => (
                <motion.div
                  key={i}
                  initial={{ opacity: 0, scale: 0 }}
                  animate={{ opacity: 1, scale: 1 }}
                  transition={{ delay: 0.5 + i * 0.15, type: "spring" }}
                  className="w-14 h-14 glass-card flex items-center justify-center text-2xl"
                >
                  {emoji}
                </motion.div>
              ))}
            </div> */}
          </motion.div>

          {/* Right side - Login card */}
          <motion.div
            initial={{ opacity: 0, x: 40 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.7, delay: 0.3 }}
            className="w-full max-w-md"
          >
            <div className="rounded-2xl border border-white/60 bg-white/75 p-5 shadow-[0_8px_30px_rgba(15,23,42,0.12)] backdrop-blur-xl sm:p-8">
              <div className="mb-6 text-center sm:mb-8">
                <h3 className="text-xl font-semibold text-slate-900">Sign In</h3>
                <p className="mt-1 text-sm text-slate-600">Enter your category credentials</p>
              </div>

              <form onSubmit={handleSubmit} className="space-y-5">
                <div className="relative">
                  <User className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
                  <Input
                    placeholder="Username"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    className="border-slate-300 bg-white/70 pl-10 text-slate-900 placeholder:text-slate-500 focus:border-sky-500"
                  />
                </div>

                <div className="relative">
                  <Lock className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
                  <Input
                    type={showPassword ? "text" : "password"}
                    placeholder="Password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className="border-slate-300 bg-white/70 pl-10 pr-10 text-slate-900 placeholder:text-slate-500 focus:border-sky-500"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword(!showPassword)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 transition-colors hover:text-slate-700"
                  >
                    {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </button>
                </div>

                {error && (
                  <motion.p
                    initial={{ opacity: 0, y: -5 }}
                    animate={{ opacity: 1, y: 0 }}
                    className="text-destructive text-sm text-center"
                  >
                    {error}
                  </motion.p>
                )}

                <Button
                  type="submit"
                  disabled={loading || !username || !password}
                  className="h-11 w-full bg-[#009FE3] font-semibold text-white hover:bg-[#008fcc]"
                >
                  {loading ? (
                    <motion.div
                      animate={{ rotate: 360 }}
                      transition={{ repeat: Infinity, duration: 1, ease: "linear" }}
                      className="w-5 h-5 border-2 border-primary-foreground/30 border-t-primary-foreground rounded-full"
                    />
                  ) : (
                    "Sign In"
                  )}
                </Button>
              </form>
            </div>
          </motion.div>
        </div>
      </div>
    </motion.div>
  );
};

export default Login;


