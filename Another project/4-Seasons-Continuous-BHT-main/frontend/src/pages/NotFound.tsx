import { Link } from "react-router-dom";

export default function NotFound() {
  return (
    <main className="grid min-h-screen place-items-center bg-slate-950 px-6 text-white">
      <section className="w-full max-w-lg rounded-2xl border border-white/10 bg-slate-900/75 p-8 text-center shadow-xl backdrop-blur">
        <h1 className="text-4xl font-black">404</h1>
        <p className="mt-3 text-slate-300">Page not found.</p>
        <Link
          to="/"
          className="mt-6 inline-flex rounded-lg bg-cyan-500 px-5 py-2.5 font-semibold text-slate-900 hover:bg-cyan-400"
        >
          Back to Login
        </Link>
      </section>
    </main>
  );
}
