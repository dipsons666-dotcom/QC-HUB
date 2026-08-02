export interface KPIData {
  marketShare: string;
  growthRate: string;
  revenue: string;
  unitsSold: string;
}

export interface BrandData {
  brand: string;
  value: number;
  share: number;
}

export interface TrendData {
  month: string;
  sales: number;
  growth: number;
}

export interface CrossTabRow {
  region: string;
  brandA: number;
  brandB: number;
  brandC: number;
  brandD: number;
  total: number;
}

export interface CategoryDashboardData {
  kpis: KPIData;
  brands: BrandData[];
  trends: TrendData[];
  pieData: { name: string; value: number; fill: string }[];
  crossTab: CrossTabRow[];
}

const pieColors = [
  "hsl(199, 89%, 48%)",
  "hsl(160, 60%, 45%)",
  "hsl(280, 60%, 55%)",
  "hsl(35, 90%, 55%)",
  "hsl(340, 70%, 55%)",
];

function generateData(seed: number): CategoryDashboardData {
  const s = (n: number) => ((seed * n + 7) % 90) + 10;
  const brands = ["Brand Alpha", "Brand Beta", "Brand Gamma", "Brand Delta"];
  const brandValues = brands.map((b, i) => s(i * 3 + 1));
  const total = brandValues.reduce((a, b) => a + b, 0);

  return {
    kpis: {
      marketShare: `${(s(1) % 40 + 15)}%`,
      growthRate: `${(s(2) % 20 + 2).toFixed(1)}%`,
      revenue: `₦${(s(3) * 12 + 500)}M`,
      unitsSold: `${(s(4) * 8 + 100)}K`,
    },
    brands: brands.map((brand, i) => ({
      brand,
      value: brandValues[i],
      share: Math.round((brandValues[i] / total) * 100),
    })),
    trends: ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"].map((month, i) => ({
      month,
      sales: s(i * 5 + seed) * 10 + 200,
      growth: (s(i * 3 + seed) % 30) - 5,
    })),
    pieData: brands.map((name, i) => ({
      name,
      value: brandValues[i],
      fill: pieColors[i % pieColors.length],
    })),
    crossTab: ["North", "South", "East", "West", "Central"].map((region, i) => ({
      region,
      brandA: s(i * 7 + 1) * 5,
      brandB: s(i * 7 + 2) * 5,
      brandC: s(i * 7 + 3) * 5,
      brandD: s(i * 7 + 4) * 5,
      total: s(i * 7 + 1) * 5 + s(i * 7 + 2) * 5 + s(i * 7 + 3) * 5 + s(i * 7 + 4) * 5,
    })),
  };
}

const categorySeeds: Record<string, number> = {
  "breakfast-cereals": 11,
  noodles: 23,
  toothpaste: 37,
  bleach: 43,
  "wet-hair": 53,
  "dry-hair": 61,
  "condiment-mixes": 71,
  malt: 79,
  snacks: 83,
  "edible-oil": 89,
  "toilet-cleaner": 97,
};

export function getCategoryData(slug: string): CategoryDashboardData {
  return generateData(categorySeeds[slug] || 11);
}
