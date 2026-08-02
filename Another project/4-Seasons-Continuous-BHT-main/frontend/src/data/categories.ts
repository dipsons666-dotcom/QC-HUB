export interface CategoryCredential {
  username: string;
  password: string;
  category: string;
  slug: string;
  description: string;
  icon: string;
}

export interface AdminCredential {
  username: string;
  password: string;
  role: "admin";
}

export const categories: CategoryCredential[] = [
  { username: "cereals", password: "inicio2026", category: "Breakfast Cereals", slug: "breakfast-cereals", description: "Exploring the breakfast cereals market landscape, consumer preferences, brand positioning, and growth trends across key demographics.", icon: "🥣" },
  { username: "noodles", password: "inicio2026", category: "Noodles", slug: "noodles", description: "Comprehensive analysis of the noodles market covering instant, dried, and fresh segments with brand share and pricing dynamics.", icon: "🍜" },
  { username: "toothpaste", password: "inicio2026", category: "Toothpaste", slug: "toothpaste", description: "In-depth toothpaste market research including whitening, sensitivity, and herbal segments across retail channels.", icon: "🪥" },
  { username: "bleach", password: "inicio2026", category: "Bleach", slug: "bleach", description: "Market intelligence on bleach products covering household and industrial segments, brand loyalty, and pricing strategies.", icon: "🧴" },
  { username: "wethair", password: "inicio2026", category: "Wet Hair", slug: "wet-hair", description: "Wet hair care market analysis spanning shampoos, conditioners, and treatments with consumer behavior insights.", icon: "💧" },
  { username: "dryhair", password: "inicio2026", category: "Dry Hair", slug: "dry-hair", description: "Dry hair care segment research including styling products, serums, and protective treatments across demographics.", icon: "💇" },
  { username: "condiment", password: "inicio2026", category: "Condiment Mixes", slug: "condiment-mixes", description: "Condiment mixes market study covering spice blends, seasoning cubes, and flavor enhancers with regional preferences.", icon: "🌶️" },
  { username: "malt", password: "inicio2026", category: "Malt", slug: "malt", description: "Malt-based beverages and food products market research including energy drinks, breakfast malt, and nutritional supplements.", icon: "🍺" },
  { username: "snacks", password: "inicio2026", category: "Snacks", slug: "snacks", description: "Snacks industry analysis covering chips, crackers, nuts, and confectionery with distribution and consumer trend data.", icon: "🍿" },
  { username: "oil", password: "inicio2026", category: "Edible Oil", slug: "edible-oil", description: "Edible oil market research spanning palm, groundnut, soy, and blended oils with pricing, sourcing, and demand patterns.", icon: "🫒" },
  { username: "toilet", password: "inicio2026", category: "Toilet Cleaner", slug: "toilet-cleaner", description: "Toilet cleaner market intelligence covering liquid, gel, and tablet formats with brand penetration and channel analysis.", icon: "🚽" },
];

export const ADMIN_HIDDEN_CATEGORY_SLUGS = new Set<string>();

export function getAdminAccessibleCategories(): CategoryCredential[] {
  return categories.filter((item) => !ADMIN_HIDDEN_CATEGORY_SLUGS.has(item.slug));
}

export function isAdminRestrictedCategory(slug: string | null | undefined): boolean {
  return ADMIN_HIDDEN_CATEGORY_SLUGS.has(String(slug || "").trim().toLowerCase());
}

export function isMaltCategorySlug(slug: string | null | undefined): boolean {
  return String(slug || "").trim().toLowerCase() === "malt";
}

export function getCategoryBrandLogo(slug: string | null | undefined): { src: string; alt: string } {
  // if (isMaltCategorySlug(slug)) {
  //   return { src: "/Guinness-Logo_1.png", alt: "Guinness logo" };
  // }
  // return { src: "/tolaram1.png", alt: "Tolaram logo" };
  return { src: "", alt: "" };
}

export const adminCredentials: AdminCredential = {
  username: "admin",
  password: "inicio2026",
  role: "admin",
};

export function authenticateUser(username: string, password: string): CategoryCredential | null {
  const found = categories.find(
    (c) => c.username.toLowerCase() === username.toLowerCase() && c.password === password
  );
  return found || null;
}

export function authenticateAdmin(username: string, password: string): AdminCredential | null {
  const isMatch =
    adminCredentials.username.toLowerCase() === username.toLowerCase() &&
    adminCredentials.password === password;
  return isMatch ? adminCredentials : null;
}
