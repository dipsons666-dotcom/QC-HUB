import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider } from "@/contexts/AuthContext";
import MetadataReview from "./MetadataReview";

function renderPage() {
  window.localStorage.setItem("inicio.auth.v1", JSON.stringify({ role: "admin", username: "admin" }));
  window.localStorage.setItem("inicio.admin.category.v1", "noodles");
  return render(
    <MemoryRouter>
      <AuthProvider>
        <MetadataReview />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("MetadataReview", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.restoreAllMocks();
  });

  it("renders diagnostics and pending review queue", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/api/admin/metadata/diagnostics")) {
        return Promise.resolve(new Response(JSON.stringify({
          ok: true,
          tables: { brand_registry: 2, metadata_change_queue: 1 },
          pendingReview: [],
        })));
      }
      if (url.includes("/api/admin/metadata/review")) {
        return Promise.resolve(new Response(JSON.stringify({
          ok: true,
          items: [{
            id: "change_1",
            entity_type: "brand",
            change_type: "unknown_brand_label",
            status: "pending_review",
            recommendation: "Review as possible new brand.",
            detected_definition: { label: "Hypo" },
            warnings: ["possible_alias"],
          }],
        })));
      }
      if (url.includes("/api/admin/metadata/registry/brands")) {
        return Promise.resolve(new Response(JSON.stringify({ ok: true, brands: [{ id: "brand_1", label: "Hypo", category: "Bleach" }] })));
      }
      if (url.includes("/api/admin/metadata/registry/questions")) {
        return Promise.resolve(new Response(JSON.stringify({ ok: true, questions: [{ id: "question_1", variable: "BL_BAU1A", label: "Top brand", category: "Bleach" }] })));
      }
      if (url.includes("/api/admin/metadata/export-spec")) {
        return Promise.resolve(new Response(JSON.stringify({
          ok: true,
          diagnostics: { brandUniverseCount: 1, questionUniverseCount: 1, tableCount: 1 },
          generatedTables: [],
        })));
      }
      return Promise.resolve(new Response(JSON.stringify({ ok: true })));
    });

    renderPage();

    expect(await screen.findByText("Metadata Review")).toBeInTheDocument();
    expect(await screen.findByText("brand registry")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Hypo")).toBeInTheDocument());
    expect(screen.getByText("Review as possible new brand.")).toBeInTheDocument();
    expect(screen.getByText("Generated Export Spec Preview")).toBeInTheDocument();
  });
});
