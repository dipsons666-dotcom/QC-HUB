import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { Navigate, useLocation, useNavigate, useParams } from "react-router-dom";
import { motion } from "framer-motion";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  BarChart3,
  Calendar,
  ChevronDown,
  Clock3,
  Download,
  MapPin,
  Percent,
  Shield,
  SlidersHorizontal,
  Table2,
  Users,
  Wallet,
} from "lucide-react";
import { DashboardSidebar } from "@/components/DashboardSidebar";
import {
  CustomTableBuilderModal,
  type CustomTableFormatOptions,
  type CustomTableSelectionPayload,
} from "@/components/CustomTableBuilderModal";
import { categories, getCategoryBrandLogo, isAdminRestrictedCategory, isMaltCategorySlug } from "@/data/categories";
import { getDashboardSidebarColor } from "@/lib/dashboardTheme";
import { getVerbatimQuestionOptions, VERBATIMS_TOPICS_ROUTE_SEGMENT } from "@/lib/verbatimTopics";
import { useAuth } from "@/contexts/AuthContext";
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import * as XLSX from "xlsx-js-style";

type PageDef = {
  id: string;
  title: string;
  description: string;
  questionCount: number;
};

type PageAnswer = {
  answer: string;
  count: number;
  pct: number;
};

type PageQuestion = {
  question: string;
  questionLabel: string;
  total: number;
  answers: PageAnswer[];
};

type PageQuestionMonthly = PageQuestion & {
  month?: string;
};
type PageDataMonthlyResponse = {
  questions: PageQuestionMonthly[];
  respondentBaseByMonth?: Record<string, number>;
};

type DemoBucket = {
  value: string;
  count: number;
  pct: number;
};

type OverviewResponse = {
  monthsAvailable: string[];
  totalRespondents: number;
  distributions: Record<string, DemoBucket[]>;
};

type SyncStatusResponse = {
  running?: boolean;
  lastSuccessAt?: string | null;
};

type AwarenessMetricDef = {
  key: string;
  label: string;
  matcher: RegExp;
};

type AwarenessMonthTrend = {
  month: string;
  metrics: Record<string, Record<string, number>>;
  counts: Record<string, Record<string, number>>;
  bases: Record<string, number>;
};

type AwarenessSummaryRow = {
  month: string;
  brand: string;
  brand_order?: number;
  option?: string;
  metric: string;
  mention_n: number;
  base_n: number;
  pct: number;
};

type CustomTableQuestionSelection = CustomTableSelectionPayload["selectedQuestionsByBreak"]["top"][number];
type CustomTableColumnBlock = {
  id: string;
  topQuestion: CustomTableQuestionSelection;
  columnLabels: string[];
  columnGroups?: Array<{ label: string; colSpan: number }>;
  columnLetterLabels: string[];
  columnBases: number[];
  rowColumnBases?: number[][];
  totalColumnBases?: number[];
  counts: number[][];
  significanceLetters: string[][];
  pairRespondents: number;
  chiSquare?: {
    statistic: number;
    degreesOfFreedom: number;
  } | null;
  notes: string[];
};
type CustomTableTable = {
  id: string;
  sideQuestion: CustomTableQuestionSelection;
  rowLabels: string[];
  rowBases: number[];
  totalRespondents: number;
  topBlocks: CustomTableColumnBlock[];
};
type CustomTableDisplayRow = {
  key: string;
  originalIndex: number;
  rowLabel: string;
  brand: string;
  attribute: string;
};
type CustomTableRenderedRow = CustomTableDisplayRow & {
  showAttribute: boolean;
  attributeRowSpan: number;
  isAttributeGroupEnd: boolean;
};
type CustomTableResponse = {
  category: string;
  displayMode: CustomTableSelectionPayload["selectedDisplayModeId"];
  analysisOptions: CustomTableSelectionPayload["selectedAnalysis"];
  formatOptions?: CustomTableFormatOptions;
  totalRespondents: number;
  generatedAt: string;
  tables: CustomTableTable[];
};
const DEFAULT_CUSTOM_TABLE_DISPLAY_MODE: CustomTableSelectionPayload["selectedDisplayModeId"] = "column_pct";

type AwarenessFilterState = {
  region: string[];
  income: string[];
  gender: string[];
  age: string[];
  sec: string[];
  week: string[];
};

type AwarenessFilterOptions = AwarenessFilterState;
type VerbatimTopic = {
  id: string;
  label: string;
  responseCount: number;
  share: number;
  sentiment: string;
  sentimentScore: number;
  keywords: string[];
  samples: string[];
};
type VerbatimWordCloudTerm = {
  text: string;
  value: number;
};
type VerbatimInsightGroup = {
  id: string;
  label: string;
  responseCount: number;
  wordCloud: VerbatimWordCloudTerm[];
  topics: VerbatimTopic[];
};
type VerbatimInsightsResponse = {
  category: string;
  questionCode: string;
  questionLabel: string;
  groupByBrand: boolean;
  totalResponses: number;
  generatedAt: string;
  groups: VerbatimInsightGroup[];
};
type ActiveSelectionChip = {
  key: string;
  label: string;
  value: string;
};
type MonthSelectionMode = "single" | "quarter" | "rolling";
type MonthButtonGroup = {
  id: string;
  label: string;
  months: string[];
  disabled?: boolean;
  subLabel?: string;
};
type AwarenessViewMode = "brand_awareness" | "ad_awareness";
type PurchaseBehaviourMode = "purchase_frequency" | "pack_size_frequency";
type CampaignCheckMode = "ad_recall" | "ad_recall_media_source";
type FlexViewMode = "qfs" | "qfsb" | "qfw";
type FlexQuestionKey = "qfs1" | "qfs3" | "qfsb1" | "qfsb3" | "qfw1" | "qfw3";
type DataTableExportType = "interim" | "full" | "rolling" | "quarter";
type DataTableExportScope = "current" | "all";
type ExportStatusResponse = {
  ok?: boolean;
  types?: Partial<Record<DataTableExportType, { active?: boolean }>>;
};

const DEFAULT_API_BASE =
  typeof window !== "undefined" && ["localhost", "127.0.0.1"].includes(window.location.hostname)
    ? "http://localhost:4000"
    : "";
const API_BASE = trimTrailingSlashes(import.meta.env.VITE_API_BASE_URL || DEFAULT_API_BASE);
const DEMO_FIELDS = ["Region", "D3", "Gender", "Age", "SEC", "Week"];
const CHART_COLORS = ["#18b6ff", "#10b981", "#f59e0b", "#8b5cf6", "#ef4444", "#3b82f6", "#14b8a6"];
const EXCLUDED_PAGE_IDS = new Set(["screener-demographics", "category-questions", "other-metrics"]);
const AWARENESS_PAGE_ID = "awareness-usage";
const AWARENESS_SUBPAGE_PREFIX = `${AWARENESS_PAGE_ID}__`;
const AWARENESS_SUBPAGE_TITLES: Record<string, string> = {
  [`${AWARENESS_SUBPAGE_PREFIX}awareness`]: "Awareness",
  [`${AWARENESS_SUBPAGE_PREFIX}media-source`]: "Media Source",
  [`${AWARENESS_SUBPAGE_PREFIX}usage`]: "Usage",
};
const AWARENESS_SUBPAGE_ID = `${AWARENESS_SUBPAGE_PREFIX}awareness`;
const MEDIA_SOURCE_SUBPAGE_ID = `${AWARENESS_SUBPAGE_PREFIX}media-source`;
const USAGE_SUBPAGE_ID = `${AWARENESS_SUBPAGE_PREFIX}usage`;
const PURCHASE_BEHAVIOUR_PAGE_ID = "purchase-behavior";
const BRAND_IMAGERY_PAGE_ID = "brand-imagery";
const FLAVOUR_FLEX_PAGE_ID = "flavour-flex-section";
const FLAVOUR_FLEX_SUBPAGE_PREFIX = `${FLAVOUR_FLEX_PAGE_ID}__`;
const FLAVOUR_SUBPAGE_ID = `${FLAVOUR_FLEX_SUBPAGE_PREFIX}flavour`;
const FLEX_SUBPAGE_ID = `${FLAVOUR_FLEX_SUBPAGE_PREFIX}flex`;
const CAMPAIGN_CHECK_PAGE_ID = "campaign-check";
const CAMPAIGN_CHECK_SUBPAGE_PREFIX = `${CAMPAIGN_CHECK_PAGE_ID}__`;
const RADIO_JINGLE_SUBPAGE_ID = `${CAMPAIGN_CHECK_SUBPAGE_PREFIX}radio-jingle`;
const FLYER_SUBPAGE_ID = `${CAMPAIGN_CHECK_SUBPAGE_PREFIX}flyer`;
const CUSTOM_TABLE_ROUTE_SEGMENT = "custom-table";
const EXPORT_REPORT_ROUTE_SEGMENT = "export-report";
const DATA_TABLE_EXPORT_TYPES: Array<{ type: DataTableExportType; title: string }> = [
  { type: "interim", title: "Export Interim Data Tables" },
  { type: "full", title: "Export Full Data Tables" },
  { type: "rolling", title: "Export Rolling Data Tables" },
  { type: "quarter", title: "Export Quarter Data Tables" },
];
const CAMPAIGN_CHECK_ALLOWED_SLUGS = new Set(["noodles", "toothpaste", "snacks", "bleach", "toilet-cleaner"]);
const SNACKS_CAMPAIGN_CHECK_SLUG = "snacks";
const FLAVOUR_FLEX_ALLOWED_SLUGS = new Set(["noodles"]);
const FLAVOUR_FLEX_SUBPAGE_TITLES: Record<string, string> = {
  [FLAVOUR_SUBPAGE_ID]: "Flavour",
  [FLEX_SUBPAGE_ID]: "Flex",
};
const CAMPAIGN_CHECK_SUBPAGE_TITLES: Record<string, string> = {
  [RADIO_JINGLE_SUBPAGE_ID]: "Radio Jingle",
  [FLYER_SUBPAGE_ID]: "Flyer",
};
const SNACKS_CAMPAIGN_CHECK_SUBPAGES = [
  { id: RADIO_JINGLE_SUBPAGE_ID, title: "Radio Jingle" },
  { id: FLYER_SUBPAGE_ID, title: "Flyer" },
];
const FIXED_PAGE_TITLES: Record<string, string> = {
  [PURCHASE_BEHAVIOUR_PAGE_ID]: "Purchase Behaviour",
  [BRAND_IMAGERY_PAGE_ID]: "Brand Imagery",
  [CAMPAIGN_CHECK_PAGE_ID]: "Campaign Check",
  [FLAVOUR_FLEX_PAGE_ID]: "Flavour/Flex Section",
};
const LEGACY_BRAND_AWARENESS_SUBPAGE_ID = `${AWARENESS_SUBPAGE_PREFIX}brand-awareness`;
const LEGACY_AD_AWARENESS_SUBPAGE_ID = `${AWARENESS_SUBPAGE_PREFIX}ad-awareness`;
const MONTH_LABEL_FORMATTER = new Intl.DateTimeFormat("en-US", {
  month: "short",
  year: "numeric",
});
const SYNC_TIMESTAMP_FORMATTER = new Intl.DateTimeFormat("en-NG", {
  dateStyle: "medium",
  timeStyle: "short",
});
const AWARENESS_SUMMARY_CACHE_TTL_MS = 30_000;
const DASHBOARD_FETCH_DEBOUNCE_MS = 120;
const awarenessSummaryCache = new Map<string, { ts: number; rows: AwarenessSummaryRow[] }>();
const awarenessSummaryInFlight = new Map<string, Promise<{ rows: AwarenessSummaryRow[] }>>();
const DEFAULT_CUSTOM_TABLE_FORMAT_OPTIONS: CustomTableFormatOptions = {
  showPercentSign: true,
  useDecimalPlaces: false,
  decimalPlaces: 1,
};
const STICKY_TABLE_LEAD_HEADER_CLASS =
  "sticky left-0 z-30 border-r border-black/70 bg-[#d9e2eb] shadow-[10px_0_14px_rgba(217,226,235,0.98)]";
const STICKY_TABLE_LEAD_SUBHEADER_CLASS =
  "sticky left-0 z-20 border-r border-black/70 bg-[#d4dde6] shadow-[10px_0_14px_rgba(212,221,230,0.98)]";
const STICKY_TABLE_LEAD_CELL_CLASS =
  "sticky left-0 z-10 border-r border-black/70 bg-[#cfdae3] shadow-[10px_0_14px_rgba(207,218,227,0.98)]";
const STICKY_TABLE_SECOND_HEADER_CLASS =
  "sticky z-20 border-r border-black/70 bg-[#d9e2eb] shadow-[10px_0_14px_rgba(217,226,235,0.98)]";
const STICKY_TABLE_SECOND_CELL_CLASS =
  "sticky z-10 border-r border-black/70 bg-[#cfdae3] shadow-[10px_0_14px_rgba(207,218,227,0.98)]";
const BRAND_IMAGERY_BRAND_COLUMN_WIDTH_PX = 190;
const BRAND_IMAGERY_ATTRIBUTE_COLUMN_WIDTH_PX = 320;
const FLEX_QFS1_MEDIA_SRC = "/dashbaord_media/ISS.jpg";
const FLEX_QFSB_MEDIA_SRC = "/dashbaord_media/MPV.jpeg";
const FLEX_QFW_MEDIA_SRC = "/dashbaord_media/MNP.mp4";
const TOOTHPASTE_CAMPAIGN_MEDIA_SRC = "/dashbaord_media/CLG.mp4";
const HYPO_CAMPAIGN_MEDIA_SRC = "/dashbaord_media/Hypo.mp4";
const FLAVOUR_METRIC_KEY = "flavour_consideration";
const SNACKS_CAMPAIGN_CHECK_CODES_BY_SUBPAGE: Record<string, string[]> = {
  [RADIO_JINGLE_SUBPAGE_ID]: ["SK_A1", "SK_A2", "SK_A3"],
  [FLYER_SUBPAGE_ID]: ["SK_A5", "SK_A6"],
};
const FLAVOUR_METRIC_DEFS: AwarenessMetricDef[] = [
  { key: FLAVOUR_METRIC_KEY, label: "Flavour", matcher: /(?:^|_)FQ1(?:_|$)/i },
];
const FLEX_MODE_CONFIG: Record<
  FlexViewMode,
  {
    buttonLabel: string;
    questionPattern: string;
    metricDefs: AwarenessMetricDef[];
    questionOptions: Array<{ key: FlexQuestionKey; buttonLabel: string; questionPattern: string }>;
  }
> = {
  qfs: {
    buttonLabel: "Ad Recall 1",
    questionPattern: "QFS(?:1|3)",
    metricDefs: [
      { key: "qfs1", label: "Ad Recall", matcher: /(?:^|_)QFS1(?:_|$)/i },
      { key: "qfs3", label: "Likelihood to purchase Indomie after sampling", matcher: /(?:^|_)QFS3(?:_|$)/i },
    ],
    questionOptions: [
      { key: "qfs1", buttonLabel: "School Sampling Stimuli Ad Recall", questionPattern: "QFS1" },
      { key: "qfs3", buttonLabel: "Likelihood to purchase Indomie after sampling", questionPattern: "QFS3" },
    ],
  },
  qfsb: {
    buttonLabel: "Ad Recall 2",
    questionPattern: "QFSB(?:1|3)",
    metricDefs: [
      { key: "qfsb1", label: "Ad Recall", matcher: /(?:^|_)QFSB1(?:_|$)/i },
      { key: "qfsb3", label: "Minimie purchase likelihood after Ad exposure", matcher: /(?:^|_)QFSB3(?:_|$)/i },
    ],
    questionOptions: [
      { key: "qfsb1", buttonLabel: "Ad Recall", questionPattern: "QFSB1" },
      { key: "qfsb3", buttonLabel: "Minimie purchase likelihood after Ad exposure", questionPattern: "QFSB3" },
    ],
  },
  qfw: {
    buttonLabel: "Ad Recall 3",
    questionPattern: "QFW(?:1|3)",
    metricDefs: [
      { key: "qfw1", label: "Ad Recall", matcher: /(?:^|_)QFW1(?:_|$)/i },
      { key: "qfw3", label: "Likelihood of purchasing Minimie after viewing the ad", matcher: /(?:^|_)QFW3(?:_|$)/i },
    ],
    questionOptions: [
      { key: "qfw1", buttonLabel: "Ad Recall", questionPattern: "QFW1" },
      { key: "qfw3", buttonLabel: "Likelihood of purchasing Minimie after viewing the ad", questionPattern: "QFW3" },
    ],
  },
};

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const maxAttempts = 3;
  let lastError: unknown = null;

  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      const response = await fetch(url, init);
      if (!response.ok) {
        const text = await response.text();
        let message = text || `Request failed (${response.status})`;
        if (text) {
          try {
            const parsed = JSON.parse(text) as { error?: string };
            if (parsed?.error) message = parsed.error;
          } catch (_err) {}
        }
        const retriable = response.status >= 500 && response.status <= 599;
        if (retriable && attempt < maxAttempts) {
          await sleep(200 * attempt);
          continue;
        }
        throw new Error(message);
      }
      return response.json();
    } catch (err) {
      if (isAbortError(err)) throw err;
      lastError = err;
      if (attempt < maxAttempts) {
        await sleep(200 * attempt);
        continue;
      }
    }
  }

  if (lastError instanceof Error) throw lastError;
  throw new Error("Network request failed");
}

function isAbortError(err: unknown): boolean {
  if (err instanceof DOMException && err.name === "AbortError") return true;
  if (err instanceof Error && err.name === "AbortError") return true;
  if (err instanceof Error && /aborted|aborterror/i.test(err.message)) return true;
  return false;
}

function trimTrailingSlashes(value: string): string {
  return value.replace(/\/+$/, "");
}

function formatSyncTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return SYNC_TIMESTAMP_FORMATTER.format(date);
}

function areStringArraysEqual(a: string[], b: string[]): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}

function getPinnedDashboardLabelRank(value: string): number {
  const text = String(value || "").trim();
  if (!text) return 0;
  if (/^others?$/i.test(text)) return 1;
  if (/^none$/i.test(text)) return 2;
  return 0;
}

function compareDashboardLabelsWithPinnedLast(leftValue: string, rightValue: string): number {
  const leftRank = getPinnedDashboardLabelRank(leftValue);
  const rightRank = getPinnedDashboardLabelRank(rightValue);
  if (leftRank !== rightRank) {
    if (leftRank === 0) return -1;
    if (rightRank === 0) return 1;
    return leftRank - rightRank;
  }
  return 0;
}

function takeItemsWithPinnedDashboardLabelsLast<T>(items: T[], limit: number, getLabel: (item: T) => string): T[] {
  const list = Array.isArray(items) ? items : [];
  const hardLimit = Math.max(0, Number(limit) || 0);
  if (hardLimit === 0 || list.length === 0) return [];

  const regular: T[] = [];
  const special: T[] = [];
  list.forEach((item) => {
    if (getPinnedDashboardLabelRank(getLabel(item)) > 0) {
      special.push(item);
    } else {
      regular.push(item);
    }
  });

  const regularSlots = Math.max(0, hardLimit - special.length);
  return [...regular.slice(0, regularSlots), ...special].slice(0, hardLimit);
}

function summarizeSelectionValues(values: string[], maxVisible = 2): string {
  const cleaned = values.map((value) => String(value || "").trim()).filter(Boolean);
  if (cleaned.length === 0) return "";
  if (cleaned.length <= maxVisible) return cleaned.join(", ");
  return `${cleaned.slice(0, maxVisible).join(", ")} +${cleaned.length - maxVisible}`;
}

function ActiveSelectionChipBar({ items, className = "" }: { items: ActiveSelectionChip[]; className?: string }) {
  if (!Array.isArray(items) || items.length === 0) return null;

  return (
    <div className={`flex min-w-0 flex-1 flex-wrap items-center gap-2 ${className}`.trim()}>
      <span className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">Active Filters</span>
      {items.map((item) => (
        <div
          key={item.key}
          className="max-w-full rounded-full border border-white/45 bg-white/28 px-3 py-1 text-xs font-semibold text-slate-700"
          title={`${item.label}: ${item.value}`}
        >
          <span className="text-slate-500">{item.label}:</span>{" "}
          <span className="text-slate-800">{item.value}</span>
        </div>
      ))}
    </div>
  );
}

function formatCustomTableMetricValue(
  count: number,
  rowBase: number,
  columnBase: number,
  totalRespondents: number,
  displayMode: CustomTableSelectionPayload["selectedDisplayModeId"],
  formatOptions?: CustomTableFormatOptions,
) {
  const metricValue = getCustomTableMetricNumericValue(count, rowBase, columnBase, totalRespondents, displayMode);
  if (displayMode === "counts") return Number(metricValue || 0).toLocaleString();
  const normalizedFormatOptions = normalizeCustomTableFormatOptions(formatOptions);
  const decimalPlaces = normalizedFormatOptions.useDecimalPlaces ? normalizedFormatOptions.decimalPlaces : 0;
  const formatted = metricValue.toFixed(decimalPlaces);
  return normalizedFormatOptions.showPercentSign ? `${formatted}%` : formatted;
}

function getCustomTableMetricNumericValue(
  count: number,
  rowBase: number,
  columnBase: number,
  totalRespondents: number,
  displayMode: CustomTableSelectionPayload["selectedDisplayModeId"],
) {
  if (displayMode === "counts") return Number(count || 0);
  const denominator =
    displayMode === "row_pct"
      ? rowBase
      : displayMode === "column_pct"
        ? columnBase
        : totalRespondents;
  return denominator > 0 ? (Number(count || 0) * 100) / denominator : 0;
}

function getCustomTableCellRowBase(
  table: CustomTableTable,
  block: CustomTableColumnBlock,
  rowIndex: number,
  columnIndex: number,
) {
  const periodSpecificBase = block.rowColumnBases?.[rowIndex]?.[columnIndex];
  return periodSpecificBase !== undefined
    ? Number(periodSpecificBase || 0)
    : Number(table.rowBases[rowIndex] || 0);
}

function getCustomTableCellTotalBase(
  table: CustomTableTable,
  block: CustomTableColumnBlock,
  columnIndex: number,
) {
  const periodSpecificBase = block.totalColumnBases?.[columnIndex];
  return periodSpecificBase !== undefined
    ? Number(periodSpecificBase || 0)
    : Number(table.totalRespondents || 0);
}

function getCustomTableTotalColumnBase(table: CustomTableTable) {
  return Number(table.totalRespondents || 0);
}

function getCustomTableTotalColumnCount(table: CustomTableTable, rowIndex: number) {
  return Number(table.rowBases[rowIndex] || 0);
}

function formatCustomTableTotalColumnValue(
  result: CustomTableResponse,
  table: CustomTableTable,
  rowIndex: number,
) {
  const totalCount = getCustomTableTotalColumnCount(table, rowIndex);
  if (result.displayMode !== "counts") {
    return formatCustomTableMetricValue(
      totalCount,
      Number(table.totalRespondents || 0),
      Number(table.totalRespondents || 0),
      Number(table.totalRespondents || 0),
      "total_pct",
      result.formatOptions,
    );
  }
  return formatCustomTableMetricValue(
    totalCount,
    Number(table.rowBases[rowIndex] || 0),
    getCustomTableTotalColumnBase(table),
    Number(table.totalRespondents || 0),
    result.displayMode,
    result.formatOptions,
  );
}

function sanitizeCustomTableNotes(notes: unknown): string[] {
  if (!Array.isArray(notes)) return [];
  return notes
    .map((note) => String(note || "").trim())
    .filter(Boolean)
    .filter((note) => !/^Significance testing is not yet implemented for Custom Table output\.?$/i.test(note));
}

function normalizeCustomTableFormatOptions(formatOptions?: CustomTableFormatOptions | null): CustomTableFormatOptions {
  const decimalPlaces = Number(formatOptions?.decimalPlaces);
  return {
    showPercentSign: formatOptions?.showPercentSign !== false,
    useDecimalPlaces: Boolean(formatOptions?.useDecimalPlaces),
    decimalPlaces: Number.isFinite(decimalPlaces) ? Math.min(4, Math.max(1, Math.trunc(decimalPlaces))) : 1,
  };
}

function isHiddenCustomTableDisplayLabel(value: string): boolean {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return true;
  if (/^(?:-|[\u2012-\u2015]|â€”|â€“)\s*(?:\([^)]*\))?$/i.test(text)) return true;
  if (/^(?:on\s+)?TV,\s*Radio,\s*Digital\/LED\s+Billboard\s*\(Billboard\s+that\s+shows\s+videos?\),\s*Static\s+Billboard\s*\(Billboard\s+that\s+shows\s+pictures?\),\s*Internet,\s*BRT\s+etc\.?$/i.test(text)) return true;
  return false;
}

function filterHiddenCustomTableRows(table: CustomTableTable): CustomTableTable {
  const rowLabels = Array.isArray(table.rowLabels) ? table.rowLabels : [];
  const keptIndexes = rowLabels
    .map((label, index) => ({ label, index }))
    .filter((item) => !isHiddenCustomTableDisplayLabel(item.label))
    .map((item) => item.index);

  if (keptIndexes.length === rowLabels.length) return table;

  return {
    ...table,
    rowLabels: keptIndexes.map((index) => rowLabels[index]),
    rowBases: keptIndexes.map((index) => Number(table.rowBases[index] || 0)),
    topBlocks: table.topBlocks.map((block) => ({
      ...block,
      columnGroups: block.columnGroups || [],
      rowColumnBases: keptIndexes.map((index) => block.rowColumnBases?.[index] || []),
      counts: keptIndexes.map((index) => block.counts[index] || []),
      significanceLetters: keptIndexes.map((index) => block.significanceLetters[index] || []),
    })),
  };
}

function normalizeCustomTableResponse(result: CustomTableResponse | null): CustomTableResponse | null {
  if (!result) return null;
  if (!Array.isArray(result.tables)) {
    return {
      ...result,
      totalRespondents: Number(result.totalRespondents || 0),
      analysisOptions: Array.isArray(result.analysisOptions) ? result.analysisOptions : [],
      formatOptions: normalizeCustomTableFormatOptions(result.formatOptions),
      tables: [],
    };
  }

  const sideTableMap = new Map<string, CustomTableTable>();

  result.tables.forEach((rawTable) => {
    const table = rawTable as Partial<CustomTableTable> & {
      topQuestion?: CustomTableQuestionSelection;
      sideQuestion?: CustomTableQuestionSelection;
      columnLabels?: string[];
      columnGroups?: Array<{ label?: string; colSpan?: number }>;
      columnLetterLabels?: string[];
      columnBases?: number[];
      rowColumnBases?: number[][];
      totalColumnBases?: number[];
      counts?: number[][];
      significanceLetters?: string[][];
      pairRespondents?: number;
      chiSquare?: { statistic: number; degreesOfFreedom: number } | null;
      notes?: string[];
      rowLabels?: string[];
      rowBases?: number[];
      totalRespondents?: number;
    };

    if (Array.isArray(table.topBlocks)) {
      const normalizedTable: CustomTableTable = {
        id: String(table.id || table.sideQuestion?.id || "table"),
        sideQuestion: table.sideQuestion || {
          id: "side_question",
          label: "Side Break",
          sectionId: "",
          sectionTitle: "Side Break",
          questionCodes: [],
        },
        rowLabels: Array.isArray(table.rowLabels) ? table.rowLabels : [],
        rowBases: Array.isArray(table.rowBases) ? table.rowBases.map((value) => Number(value || 0)) : [],
        totalRespondents: Number(table.totalRespondents || result.totalRespondents || 0),
        topBlocks: table.topBlocks.map((block) => ({
          id: String(block.id || block.topQuestion?.id || "top_block"),
          topQuestion: block.topQuestion || {
            id: "top_question",
            label: "Top Break",
            sectionId: "",
            sectionTitle: "Top Break",
            questionCodes: [],
          },
          columnLabels: Array.isArray(block.columnLabels) ? block.columnLabels : [],
          columnGroups: Array.isArray(block.columnGroups)
            ? block.columnGroups
                .map((group) => ({
                  label: String(group?.label || ""),
                  colSpan: Math.max(1, Number(group?.colSpan || 1)),
                }))
                .filter((group) => group.label)
            : [],
          columnLetterLabels: Array.isArray(block.columnLetterLabels) ? block.columnLetterLabels.map((value) => String(value || "")) : [],
          columnBases: Array.isArray(block.columnBases) ? block.columnBases.map((value) => Number(value || 0)) : [],
          rowColumnBases: Array.isArray(block.rowColumnBases)
            ? block.rowColumnBases.map((row) => (Array.isArray(row) ? row.map((value) => Number(value || 0)) : []))
            : [],
          totalColumnBases: Array.isArray(block.totalColumnBases)
            ? block.totalColumnBases.map((value) => Number(value || 0))
            : [],
          counts: Array.isArray(block.counts) ? block.counts.map((row) => (Array.isArray(row) ? row.map((value) => Number(value || 0)) : [])) : [],
          significanceLetters: Array.isArray(block.significanceLetters)
            ? block.significanceLetters.map((row) => (Array.isArray(row) ? row.map((value) => String(value || "")) : []))
            : [],
          pairRespondents: Number(block.pairRespondents || 0),
          chiSquare: block.chiSquare || null,
          notes: sanitizeCustomTableNotes(block.notes),
        })),
      };
      sideTableMap.set(normalizedTable.id, normalizedTable);
      return;
    }

    if (!table.sideQuestion || !table.topQuestion) return;

    const sideKey = String(table.sideQuestion.id || table.id || "table");
    if (!sideTableMap.has(sideKey)) {
      sideTableMap.set(sideKey, {
        id: sideKey,
        sideQuestion: table.sideQuestion,
        rowLabels: Array.isArray(table.rowLabels) ? table.rowLabels : [],
        rowBases: Array.isArray(table.rowBases) ? table.rowBases.map((value) => Number(value || 0)) : [],
        totalRespondents: Number(table.totalRespondents || result.totalRespondents || 0),
        topBlocks: [],
      });
    }

    const sideTable = sideTableMap.get(sideKey);
    if (!sideTable) return;
    sideTable.topBlocks.push({
      id: String(table.topQuestion.id || table.id || "top_block"),
      topQuestion: table.topQuestion,
      columnLabels: Array.isArray(table.columnLabels) ? table.columnLabels : [],
      columnGroups: Array.isArray(table.columnGroups)
        ? table.columnGroups
            .map((group) => ({
              label: String(group?.label || ""),
              colSpan: Math.max(1, Number(group?.colSpan || 1)),
            }))
            .filter((group) => group.label)
        : [],
      columnLetterLabels: Array.isArray(table.columnLetterLabels) ? table.columnLetterLabels.map((value) => String(value || "")) : [],
      columnBases: Array.isArray(table.columnBases) ? table.columnBases.map((value) => Number(value || 0)) : [],
      rowColumnBases: Array.isArray(table.rowColumnBases)
        ? table.rowColumnBases.map((row) => (Array.isArray(row) ? row.map((value) => Number(value || 0)) : []))
        : [],
      totalColumnBases: Array.isArray(table.totalColumnBases)
        ? table.totalColumnBases.map((value) => Number(value || 0))
        : [],
      counts: Array.isArray(table.counts) ? table.counts.map((row) => (Array.isArray(row) ? row.map((value) => Number(value || 0)) : [])) : [],
      significanceLetters: Array.isArray(table.significanceLetters)
        ? table.significanceLetters.map((row) => (Array.isArray(row) ? row.map((value) => String(value || "")) : []))
        : [],
      pairRespondents: Number(table.pairRespondents || 0),
      chiSquare: table.chiSquare || null,
      notes: sanitizeCustomTableNotes(table.notes),
    });
  });

  return {
    ...result,
    totalRespondents: Number(result.totalRespondents || 0),
    analysisOptions: Array.isArray(result.analysisOptions) ? result.analysisOptions : [],
    formatOptions: normalizeCustomTableFormatOptions(result.formatOptions),
    tables: Array.from(sideTableMap.values()).map(filterHiddenCustomTableRows),
  };
}

function sanitizeWorksheetName(value: string, fallback: string): string {
  const cleaned = String(value || "")
    .replace(/[\\/*?:[\]]/g, "")
    .trim();
  return (cleaned || fallback).slice(0, 31);
}

function sanitizeFileNameSegment(value: string, fallback: string): string {
  const cleaned = String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return cleaned || fallback;
}

function isValidExcelXmlCodePoint(codePoint: number) {
  return (
    codePoint === 0x9 ||
    codePoint === 0xa ||
    codePoint === 0xd ||
    (codePoint >= 0x20 && codePoint <= 0xd7ff) ||
    (codePoint >= 0xe000 && codePoint <= 0xfffd) ||
    (codePoint >= 0x10000 && codePoint <= 0x10ffff)
  );
}

function sanitizeExcelText(value: string): string {
  const normalized = String(value || "").replace(/\r\n/g, "\n");
  let result = "";

  for (let index = 0; index < normalized.length; index += 1) {
    const codeUnit = normalized.charCodeAt(index);
    if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
      const nextCodeUnit = normalized.charCodeAt(index + 1);
      if (nextCodeUnit >= 0xdc00 && nextCodeUnit <= 0xdfff) {
        const codePoint = normalized.codePointAt(index);
        if (codePoint !== undefined && isValidExcelXmlCodePoint(codePoint)) {
          result += normalized[index] + normalized[index + 1];
        }
        index += 1;
      }
      continue;
    }

    if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
      continue;
    }

    if (isValidExcelXmlCodePoint(codeUnit)) {
      result += normalized[index];
    }
  }

  return result.slice(0, 32767);
}

function parseBrandImageryCustomTableRowLabel(rowLabel: string): { brand: string; attribute: string } | null {
  const text = String(rowLabel || "").trim();
  const match = text.match(/^(.*)\s+\(([^()]+)\)\s*$/);
  if (!match) return null;

  const attribute = String(match[1] || "").trim();
  const brand = String(match[2] || "").trim();
  if (!attribute || !brand) return null;
  return { brand, attribute };
}

function isBau4CustomTable(table: CustomTableTable): boolean {
  const sideQuestion = table.sideQuestion;
  const text = [
    sideQuestion.label,
    sideQuestion.id,
    ...(Array.isArray(sideQuestion.questionCodes) ? sideQuestion.questionCodes : []),
  ].join(" ");
  return /\bBAU4(?:[._]\d+)*\b/i.test(text) || /\bbau4_[a-z]+_\d+_\d+\b/i.test(text);
}

function getCustomTableGroupedAttributeHeader(table: CustomTableTable): string {
  if (isBau4CustomTable(table)) return "Media Source";
  if (table.sideQuestion.sectionId === BRAND_IMAGERY_PAGE_ID) return "Attribute";
  return "Attribute";
}

function getCustomTableGroupedSecondHeader(table: CustomTableTable): string {
  if (isBau4CustomTable(table)) return "Brand";
  if (table.sideQuestion.sectionId === BRAND_IMAGERY_PAGE_ID) return "Brand";
  return "Brand";
}

function getCustomTablePinnedDisplayLabelRank(value: string): number {
  const text = String(value || "").trim();
  if (!text) return 0;
  const trailingParen = text.match(/\(([^()]+)\)\s*$/);
  if (trailingParen?.[1]) {
    const nestedRank = getCustomTablePinnedDisplayLabelRank(trailingParen[1]);
    if (nestedRank > 0) return nestedRank;
  }
  if (/^others?$/i.test(text)) return 1;
  if (/^none(?:\s+of\s+these)?$/i.test(text)) return 2;
  return 0;
}

function getCustomTableDisplayRows(table: CustomTableTable): {
  rows: CustomTableDisplayRow[];
  usesBrandImageryLayout: boolean;
} {
  const fallbackRows = (Array.isArray(table.rowLabels) ? table.rowLabels : []).map((rowLabel, index) => ({
    key: `${table.id}-${index}`,
    originalIndex: index,
    rowLabel,
    brand: "",
    attribute: rowLabel,
  }));
  const fallbackSortedRows = [...fallbackRows].sort((left, right) => {
    const leftRank = getCustomTablePinnedDisplayLabelRank(left.rowLabel);
    const rightRank = getCustomTablePinnedDisplayLabelRank(right.rowLabel);
    if (leftRank !== rightRank) {
      if (leftRank === 0) return -1;
      if (rightRank === 0) return 1;
      return leftRank - rightRank;
    }
    return left.originalIndex - right.originalIndex;
  });

  if (table.sideQuestion.sectionId !== BRAND_IMAGERY_PAGE_ID && !isBau4CustomTable(table)) {
    return { rows: fallbackSortedRows, usesBrandImageryLayout: false };
  }

  const parsedRows = fallbackRows.map((row) => {
    const parsed = parseBrandImageryCustomTableRowLabel(row.rowLabel);
    if (!parsed) return row;
    if (isBau4CustomTable(table)) {
      return {
        ...row,
        attribute: parsed.attribute,
        brand: parsed.brand,
      };
    }
    return {
      ...row,
      attribute: parsed.attribute,
      brand: parsed.brand,
    };
  });

  const parseableRows = parsedRows.filter((row) => row.brand && row.attribute);
  if (parseableRows.length === 0) {
    return { rows: fallbackSortedRows, usesBrandImageryLayout: false };
  }

  const brandOrder = new Map<string, number>();
  const attributeOrder = new Map<string, number>();
  parsedRows.forEach((row) => {
    if (row.brand && !brandOrder.has(row.brand)) {
      brandOrder.set(row.brand, brandOrder.size);
    }
    const attributeKey = row.attribute || row.rowLabel;
    if (attributeKey && !attributeOrder.has(attributeKey)) {
      attributeOrder.set(attributeKey, attributeOrder.size);
    }
  });

  const rows = [...parsedRows].sort((left, right) => {
    const leftAttributePinnedRank = getCustomTablePinnedDisplayLabelRank(left.attribute);
    const rightAttributePinnedRank = getCustomTablePinnedDisplayLabelRank(right.attribute);
    if (leftAttributePinnedRank !== rightAttributePinnedRank) {
      if (leftAttributePinnedRank === 0) return -1;
      if (rightAttributePinnedRank === 0) return 1;
      return leftAttributePinnedRank - rightAttributePinnedRank;
    }

    const leftAttributeKey = left.attribute || left.rowLabel;
    const rightAttributeKey = right.attribute || right.rowLabel;
    const leftAttributeRank = Number(attributeOrder.get(leftAttributeKey) ?? Number.MAX_SAFE_INTEGER);
    const rightAttributeRank = Number(attributeOrder.get(rightAttributeKey) ?? Number.MAX_SAFE_INTEGER);
    if (leftAttributeRank !== rightAttributeRank) return leftAttributeRank - rightAttributeRank;

    const leftBrandPinnedRank = getCustomTablePinnedDisplayLabelRank(left.brand);
    const rightBrandPinnedRank = getCustomTablePinnedDisplayLabelRank(right.brand);
    if (leftBrandPinnedRank !== rightBrandPinnedRank) {
      if (leftBrandPinnedRank === 0) return -1;
      if (rightBrandPinnedRank === 0) return 1;
      return leftBrandPinnedRank - rightBrandPinnedRank;
    }

    const leftBrandRank = left.brand ? Number(brandOrder.get(left.brand) ?? Number.MAX_SAFE_INTEGER) : Number.MAX_SAFE_INTEGER;
    const rightBrandRank = right.brand ? Number(brandOrder.get(right.brand) ?? Number.MAX_SAFE_INTEGER) : Number.MAX_SAFE_INTEGER;
    if (leftBrandRank !== rightBrandRank) return leftBrandRank - rightBrandRank;

    return left.originalIndex - right.originalIndex;
  });

  return {
    rows,
    usesBrandImageryLayout: true,
  };
}

function getCustomTableRenderedRows(rows: CustomTableDisplayRow[], usesBrandImageryLayout: boolean): CustomTableRenderedRow[] {
  if (!usesBrandImageryLayout) {
    return rows.map((row) => ({
      ...row,
      showAttribute: true,
      attributeRowSpan: 1,
      isAttributeGroupEnd: true,
    }));
  }

  const renderedRows: CustomTableRenderedRow[] = [];
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    let groupSize = 1;
    while (index + groupSize < rows.length && rows[index + groupSize].attribute === row.attribute) {
      groupSize += 1;
    }

    renderedRows.push({
      ...row,
      showAttribute: true,
      attributeRowSpan: groupSize,
      isAttributeGroupEnd: groupSize === 1,
    });

    for (let offset = 1; offset < groupSize; offset += 1) {
      renderedRows.push({
        ...rows[index + offset],
        showAttribute: false,
        attributeRowSpan: 0,
        isAttributeGroupEnd: offset === groupSize - 1,
      });
    }

    index += groupSize - 1;
  }

  return renderedRows;
}

function toExcelCellValue(value: unknown): string | number | boolean {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : 0;
  }
  if (typeof value === "boolean") return value;
  return sanitizeExcelText(String(value ?? ""));
}

function downloadWorkbookBlob(workbook: XLSX.WorkBook, fileName: string, revokeDelayMs = 60000) {
  const buffer = XLSX.write(workbook, {
    bookType: "xlsx",
    type: "array",
    compression: true,
  });
  const blob = new Blob([buffer], {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = fileName;
  anchor.rel = "noopener";
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), revokeDelayMs);
}

function buildCustomTableExportCellValue(
  result: CustomTableResponse,
  table: CustomTableTable,
  block: CustomTableColumnBlock,
  rowIndex: number,
  columnIndex: number,
): string | number {
  const metricValue = getCustomTableMetricNumericValue(
    Number(block.counts[rowIndex]?.[columnIndex] || 0),
    getCustomTableCellRowBase(table, block, rowIndex, columnIndex),
    Number(block.columnBases[columnIndex] || 0),
    getCustomTableCellTotalBase(table, block, columnIndex),
    result.displayMode,
  );
  const significance = String(block.significanceLetters[rowIndex]?.[columnIndex] || "").trim();
  if (significance) {
    const formattedValue = formatCustomTableMetricValue(
      Number(block.counts[rowIndex]?.[columnIndex] || 0),
      getCustomTableCellRowBase(table, block, rowIndex, columnIndex),
      Number(block.columnBases[columnIndex] || 0),
      getCustomTableCellTotalBase(table, block, columnIndex),
      result.displayMode,
      result.formatOptions,
    );
    return `${formattedValue}\n${significance}`;
  }

  if (result.displayMode === "counts") {
    return Math.round(metricValue);
  }

  const formatOptions = normalizeCustomTableFormatOptions(result.formatOptions);
  const decimalPlaces = formatOptions.useDecimalPlaces ? formatOptions.decimalPlaces : 0;
  return Number(metricValue.toFixed(decimalPlaces));
}

function buildCustomTableTotalExportCellValue(result: CustomTableResponse, table: CustomTableTable, rowIndex: number): number {
  const totalCount = getCustomTableTotalColumnCount(table, rowIndex);
  const metricValue = getCustomTableMetricNumericValue(
    totalCount,
    result.displayMode === "counts" ? Number(table.rowBases[rowIndex] || 0) : Number(table.totalRespondents || 0),
    Number(table.totalRespondents || 0),
    Number(table.totalRespondents || 0),
    result.displayMode === "counts" ? "counts" : "total_pct",
  );

  if (result.displayMode === "counts") {
    return Math.round(metricValue);
  }

  const formatOptions = normalizeCustomTableFormatOptions(result.formatOptions);
  const decimalPlaces = formatOptions.useDecimalPlaces ? formatOptions.decimalPlaces : 0;
  return Number(metricValue.toFixed(decimalPlaces));
}

function downloadCustomTableWorkbook(result: CustomTableResponse | null, categoryTitle: string) {
  const normalizedResult = normalizeCustomTableResponse(result);
  if (!normalizedResult || !Array.isArray(normalizedResult.tables) || normalizedResult.tables.length === 0) {
    return;
  }
  const formatOptions = normalizeCustomTableFormatOptions(normalizedResult.formatOptions);

  const workbook = XLSX.utils.book_new();
  const rows: Array<Array<string | number>> = [];
  const mergesAll: XLSX.Range[] = [];
  const tableRanges: Array<{
    startRow: number;
    endRow: number;
    totalCols: number;
    leadColumnCount: number;
    titleRow: number;
    headerRow1: number;
    headerRow2: number;
    baseRow: number;
    noteRows: number[];
  }> = [];
  let hasBrandImageryLayout = false;
  const headerFill = { fgColor: { rgb: "E6EEF7" } };
  const subHeaderFill = { fgColor: { rgb: "F4F7FB" } };
  const borderStyle = {
    top: { style: "thin", color: { rgb: "4B5563" } },
    bottom: { style: "thin", color: { rgb: "4B5563" } },
    left: { style: "thin", color: { rgb: "4B5563" } },
    right: { style: "thin", color: { rgb: "4B5563" } },
  };

  normalizedResult.tables.forEach((table, tableIndex) => {
    const displayRowsResult = getCustomTableDisplayRows(table);
    const displayRows = displayRowsResult.rows;
    const usesBrandImageryLayout = displayRowsResult.usesBrandImageryLayout;
    const groupedAttributeHeader = getCustomTableGroupedAttributeHeader(table);
    const groupedSecondHeader = getCustomTableGroupedSecondHeader(table);
    const renderedRows = getCustomTableRenderedRows(displayRows, usesBrandImageryLayout);
    const showTotalColumn = !customTableUsesMonthGroupingTopBreak(table);
    if (usesBrandImageryLayout) hasBrandImageryLayout = true;
    const topBlocks = Array.isArray(table.topBlocks) ? table.topBlocks : [];
    const uniqueNotes = Array.from(new Set(topBlocks.flatMap((block) => block.notes || [])));
    const blockSpanTotal = topBlocks.reduce((sum, block) => sum + Math.max(1, block.columnLabels.length), 0);
    const leadColumnCount = usesBrandImageryLayout ? 2 : 1;
    const totalCols = leadColumnCount + blockSpanTotal + (showTotalColumn ? 1 : 0);
    const titleRowIndex = rows.length;
    const titleRow: Array<string | number> = [table.sideQuestion.label || `Table ${tableIndex + 1}`];
    for (let i = 1; i < totalCols; i += 1) titleRow.push("");
    rows.push(titleRow);
    mergesAll.push({
      s: { r: titleRowIndex, c: 0 },
      e: { r: titleRowIndex, c: totalCols - 1 },
    });

    const headerRow1: Array<string | number> = usesBrandImageryLayout ? [groupedAttributeHeader, groupedSecondHeader] : [""];
    const headerRow2: Array<string | number> = usesBrandImageryLayout ? ["", ""] : [""];
    let colIndex = leadColumnCount;

    if (usesBrandImageryLayout) {
      mergesAll.push({
        s: { r: titleRowIndex + 1, c: 0 },
        e: { r: titleRowIndex + 2, c: 0 },
      });
      mergesAll.push({
        s: { r: titleRowIndex + 1, c: 1 },
        e: { r: titleRowIndex + 2, c: 1 },
      });
    }

    if (showTotalColumn) {
      headerRow1.push("Total");
      headerRow2.push("");
      mergesAll.push({
        s: { r: titleRowIndex + 1, c: colIndex },
        e: { r: titleRowIndex + 2, c: colIndex },
      });
      colIndex += 1;
    }

    if (topBlocks.length === 0) {
      headerRow1.push("Top Break");
      headerRow2.push("No data");
    } else {
      topBlocks.forEach((block) => {
        const span = Math.max(1, block.columnLabels.length);
        headerRow1.push(block.topQuestion.label || "Top Break");
        for (let i = 1; i < span; i += 1) headerRow1.push("");
        if (span > 1) {
          mergesAll.push({
            s: { r: titleRowIndex + 1, c: colIndex },
            e: { r: titleRowIndex + 1, c: colIndex + span - 1 },
          });
        }
        if (block.columnLabels.length === 0) {
          headerRow2.push("No data");
        } else {
          block.columnLabels.forEach((columnLabel, columnIndex) => {
            const columnLetter = String(block.columnLetterLabels[columnIndex] || "").trim();
            headerRow2.push(columnLetter ? `${columnLabel} (${columnLetter})` : columnLabel);
          });
        }
        colIndex += span;
      });
    }

    rows.push(headerRow1);
    rows.push(headerRow2);

    const baseRow: Array<string | number> = usesBrandImageryLayout ? ["N", ""] : ["N"];
    if (showTotalColumn) baseRow.push(getCustomTableTotalColumnBase(table));
    topBlocks.forEach((block) => {
      if (block.columnLabels.length === 0) {
        baseRow.push("-");
        return;
      }
      block.columnLabels.forEach((_, columnIndex) => baseRow.push(Number(block.columnBases[columnIndex] || 0)));
    });
    rows.push(baseRow);

    const firstDataRowIndex = rows.length;
    renderedRows.forEach((displayRow, displayIndex) => {
      const rowIndex = displayRow.originalIndex;
      const row: Array<string | number> = usesBrandImageryLayout
        ? [displayRow.showAttribute ? displayRow.attribute : "", displayRow.brand]
        : [displayRow.rowLabel];
      if (showTotalColumn) row.push(buildCustomTableTotalExportCellValue(normalizedResult, table, rowIndex));
      if (topBlocks.length === 0) {
        row.push("-");
      } else {
        topBlocks.forEach((block) => {
          if (block.columnLabels.length === 0) {
            row.push("-");
            return;
          }
          block.columnLabels.forEach((_, columnIndex) => {
            row.push(buildCustomTableExportCellValue(normalizedResult, table, block, rowIndex, columnIndex));
          });
        });
      }
      rows.push(row);

      if (usesBrandImageryLayout && displayRow.showAttribute && displayRow.attributeRowSpan > 1) {
        mergesAll.push({
          s: { r: firstDataRowIndex + displayIndex, c: 0 },
          e: { r: firstDataRowIndex + displayIndex + displayRow.attributeRowSpan - 1, c: 0 },
        });
      }
    });

    const noteRows: number[] = [];
    uniqueNotes.forEach((note) => {
      const noteRowIndex = rows.length;
      const noteRow: Array<string | number> = [`Note: ${note}`];
      for (let i = 1; i < totalCols; i += 1) noteRow.push("");
      rows.push(noteRow);
      mergesAll.push({
        s: { r: noteRowIndex, c: 0 },
        e: { r: noteRowIndex, c: totalCols - 1 },
      });
      noteRows.push(noteRowIndex);
    });

    const tableEndRow = rows.length - 1;
    tableRanges.push({
      startRow: titleRowIndex,
      endRow: tableEndRow,
      totalCols,
      leadColumnCount,
      titleRow: titleRowIndex,
      headerRow1: titleRowIndex + 1,
      headerRow2: titleRowIndex + 2,
      baseRow: titleRowIndex + 3,
      noteRows,
    });

    if (tableIndex < normalizedResult.tables.length - 1) {
      rows.push([]);
      rows.push([]);
    }
  });

  const sanitizedRows = rows.map((row) => row.map((value) => toExcelCellValue(value)));
  const worksheet = XLSX.utils.aoa_to_sheet(sanitizedRows);
  const maxCols = Math.max(...tableRanges.map((range) => range.totalCols), 1);
  worksheet["!merges"] = mergesAll;
  worksheet["!cols"] = Array.from({ length: maxCols }, (_, index) => {
    if (hasBrandImageryLayout) {
      if (index === 0) return { wch: 34 };
      if (index === 1) return { wch: 20 };
    }
    return index === 0 ? { wch: 36 } : { wch: 14 };
  });

  const applyStyle = (r: number, c: number, style: Record<string, unknown>) => {
    const cellRef = XLSX.utils.encode_cell({ r, c });
    const cell = worksheet[cellRef] as { s?: Record<string, unknown>; v?: unknown } | undefined;
    if (!cell) return;
    cell.s = { ...(cell.s ?? {}), ...style };
  };

  const percentDecimals = formatOptions.useDecimalPlaces ? formatOptions.decimalPlaces : 0;
  const percentNumFmt = `${percentDecimals === 0 ? "0" : `0.${"0".repeat(percentDecimals)}`}${formatOptions.showPercentSign ? "\\%" : ""}`;
  const dataNumFmt = normalizedResult.displayMode === "counts" ? "0" : percentNumFmt;

  tableRanges.forEach((range) => {
    for (let r = range.startRow; r <= range.endRow; r += 1) {
      for (let c = 0; c < range.totalCols; c += 1) {
        applyStyle(r, c, {
          border: borderStyle,
          alignment: { vertical: "center", wrapText: true, horizontal: c < range.leadColumnCount ? "left" : "center" },
        });
      }
    }

    for (let c = 0; c < range.totalCols; c += 1) {
      applyStyle(range.titleRow, c, {
        font: { bold: true, sz: 14, color: { rgb: "1E293B" } },
        alignment: { horizontal: "left", vertical: "center" },
      });
      applyStyle(range.headerRow1, c, {
        font: { bold: true, color: { rgb: "1E293B" } },
        fill: headerFill,
        alignment: { horizontal: c < range.leadColumnCount ? "left" : "center", vertical: "center" },
      });
      applyStyle(range.headerRow2, c, {
        font: { bold: true, color: { rgb: "1E293B" } },
        fill: subHeaderFill,
        alignment: { horizontal: c < range.leadColumnCount ? "left" : "center", vertical: "center" },
      });
      applyStyle(range.baseRow, c, {
        font: { bold: true, color: { rgb: "1E293B" } },
        alignment: { horizontal: c < range.leadColumnCount ? "left" : "center", vertical: "center" },
      });
    }

    for (let c = range.leadColumnCount; c < range.totalCols; c += 1) {
      applyStyle(range.baseRow, c, { numFmt: "0" });
    }

    const noteRowSet = new Set(range.noteRows);
    for (let r = range.baseRow + 1; r <= range.endRow; r += 1) {
      if (noteRowSet.has(r)) {
        applyStyle(r, 0, {
          font: { italic: true, color: { rgb: "7C2D12" } },
          alignment: { horizontal: "left", vertical: "center", wrapText: true },
        });
        continue;
      }

      for (let c = 0; c < Math.min(range.leadColumnCount, range.totalCols); c += 1) {
        applyStyle(r, c, { font: { bold: true, color: { rgb: "1E293B" } } });
      }
      for (let c = range.leadColumnCount; c < range.totalCols; c += 1) {
        const cellRef = XLSX.utils.encode_cell({ r, c });
        const cell = worksheet[cellRef] as { v?: unknown } | undefined;
        if (!cell || typeof cell.v !== "number") continue;
        applyStyle(r, c, { numFmt: dataNumFmt });
      }
    }
  });

  const sheetName = sanitizeWorksheetName("Data Tables", "Data Tables");
  XLSX.utils.book_append_sheet(workbook, worksheet, sheetName);
  const fileName = `${sanitizeFileNameSegment(categoryTitle, "category")}_custom_tables.xlsx`;
  downloadWorkbookBlob(workbook, fileName, 120000);
}

function buildCustomTableRequestKey(payload: CustomTableSelectionPayload, months: string[], monthGroups: MonthButtonGroup[] = []) {
  const selectedQuestionsByBreak = payload?.selectedQuestionsByBreak || { top: [], side: [] };
  const selectedAnalysis = Array.isArray(payload?.selectedAnalysis) ? payload.selectedAnalysis : [];
  return JSON.stringify({
    displayMode: payload?.selectedDisplayModeId || DEFAULT_CUSTOM_TABLE_DISPLAY_MODE,
    analysis: [...selectedAnalysis].sort(),
    formatOptions: normalizeCustomTableFormatOptions(payload?.formatOptions),
    months: [...months].sort(),
    monthGroups: monthGroups.map((group) => ({
      id: group.id,
      label: group.label,
      months: [...group.months].sort(),
    })),
    top: (Array.isArray(selectedQuestionsByBreak.top) ? selectedQuestionsByBreak.top : []).map((item) => ({
      id: item.id,
      codes: Array.isArray(item.questionCodes) ? [...item.questionCodes].sort() : [],
    })),
    side: (Array.isArray(selectedQuestionsByBreak.side) ? selectedQuestionsByBreak.side : []).map((item) => ({
      id: item.id,
      codes: Array.isArray(item.questionCodes) ? [...item.questionCodes].sort() : [],
    })),
  });
}

function customTableUsesMonthGroupingTopBreak(table: CustomTableTable): boolean {
  return (Array.isArray(table.topBlocks) ? table.topBlocks : []).some((block) => (block.columnGroups || []).length > 0);
}

function getCustomTableDisplayModeLabel(displayMode: CustomTableSelectionPayload["selectedDisplayModeId"]): string {
  if (displayMode === "column_pct") return "COLUMN %";
  if (displayMode === "row_pct") return "ROW %";
  if (displayMode === "total_pct") return "TOTAL %";
  return "COUNTS";
}

function CustomTableResultsView({
  result,
  loading,
  error,
  activeSelections = [],
}: {
  result: CustomTableResponse | null;
  loading: boolean;
  error: string;
  activeSelections?: ActiveSelectionChip[];
}) {
  if (loading && !result) {
    return (
      <section className="space-y-4">
        {Array.from({ length: 2 }).map((_, index) => (
          <div key={index} className="morphism-card rounded-2xl p-5">
            <div className="h-5 w-48 rounded bg-white/25" />
            <div className="mt-3 h-3 w-80 rounded bg-white/15" />
            <div className="mt-4 h-48 rounded-2xl bg-white/12" />
          </div>
        ))}
      </section>
    );
  }

  if (error && !result) {
    return <div className="rounded-xl border border-rose-300/50 bg-rose-100/70 p-4 text-sm text-rose-800">{error}</div>;
  }

  const normalizedResult = normalizeCustomTableResponse(result);
  if (!normalizedResult) return null;

  return (
    <section className="space-y-4">
      {error ? <div className="rounded-xl border border-rose-300/50 bg-rose-100/70 p-4 text-sm text-rose-800">{error}</div> : null}

      {loading ? (
        <div className="sticky top-24 z-10 flex justify-end">
          <div className="rounded-xl border border-sky-300/55 bg-sky-100/85 px-3 py-2 text-xs font-semibold text-sky-900 shadow-sm backdrop-blur-sm">
            Updating custom table...
          </div>
        </div>
      ) : null}

      {normalizedResult.tables.map((table) => {
        const displayRowsResult = getCustomTableDisplayRows(table);
        const displayRows = displayRowsResult.rows;
        const usesBrandImageryLayout = displayRowsResult.usesBrandImageryLayout;
        const renderedRows = getCustomTableRenderedRows(displayRows, usesBrandImageryLayout);
        const getBrandImageryRowBorderClass = (row: CustomTableRenderedRow) =>
          row.isAttributeGroupEnd ? "border-b-2 border-black/70" : "border-b border-black/60";
        const topBlocks = Array.isArray(table.topBlocks) ? table.topBlocks : [];
        const uniqueNotes = Array.from(new Set(topBlocks.flatMap((block) => block.notes || [])));
        const hasRenderableColumns = topBlocks.some((block) => block.columnLabels.length > 0);
        const showTotalColumn = !customTableUsesMonthGroupingTopBreak(table);
        const hasNestedMonthColumns = customTableUsesMonthGroupingTopBreak(table);
        const hasSignificanceLetters = topBlocks.some((block) =>
          (block.significanceLetters || []).some((row) => row.some((value) => String(value || "").trim().length > 0)),
        );
        const topBreakSummary = topBlocks.map((block) => block.topQuestion.label).filter(Boolean).join(" + ");
        const chiSquareBlocks = topBlocks.filter((block) => block.chiSquare);
        const groupedAttributeHeader = getCustomTableGroupedAttributeHeader(table);
        const groupedSecondHeader = getCustomTableGroupedSecondHeader(table);

        return (
          <article
            key={table.id}
            aria-busy={loading}
            className={`relative overflow-hidden rounded-[28px] border border-white/40 bg-[#d8e2ec]/90 shadow-[0_22px_45px_rgba(15,23,42,0.12)] backdrop-blur-sm transition-all duration-200 ${
              loading ? "[&>div:not(.custom-table-refresh-overlay)]:blur-[1.5px] [&>div:not(.custom-table-refresh-overlay)]:opacity-70" : ""
            }`}
          >
            {loading ? (
              <div className="custom-table-refresh-overlay pointer-events-none absolute inset-0 z-20 rounded-[28px] bg-white/20 ring-1 ring-white/30" />
            ) : null}
            <div className="border-b border-white/40 px-6 py-5">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <h3 className="text-[1.7rem] font-semibold leading-tight text-slate-900">{table.sideQuestion.label}</h3>
                  <p className="mt-2 text-base text-slate-600">
                    Top break: <span className="font-medium text-slate-700">{topBreakSummary || "None selected"}</span>
                  </p>
                  {chiSquareBlocks.map((block) => (
                    <p key={`${table.id}-${block.id}-chi-line`} className="mt-1 text-sm text-slate-600">
                      Chi-square ({block.topQuestion.label}): {Number(block.chiSquare?.statistic || 0).toFixed(3)} (df=
                      {block.chiSquare?.degreesOfFreedom})
                    </p>
                  ))}
                </div>
                <div className="pt-1 text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">
                  {getCustomTableDisplayModeLabel(normalizedResult.displayMode)}
                </div>
              </div>

              <ActiveSelectionChipBar items={activeSelections} className="mt-4" />

              {hasSignificanceLetters ? (
                <p className="mt-3 text-xs font-semibold text-emerald-700">
                  Green letters show the columns this cell is significantly higher than at 95% confidence.
                </p>
              ) : null}

              {uniqueNotes.length > 0 ? (
                <div className="mt-3 rounded-2xl border border-amber-300/55 bg-amber-100/70 p-3 text-xs text-amber-900">
                  {uniqueNotes.map((note, index) => (
                    <p key={`${table.id}-note-${index}`}>{note}</p>
                  ))}
                </div>
              ) : null}
            </div>

            {renderedRows.length === 0 || !hasRenderableColumns ? (
              <div className="px-6 py-5 text-sm text-slate-700">
                No overlapping response data was found for this side break table.
              </div>
            ) : (
              <div className="px-6 py-5">
                <div className="overflow-x-auto rounded-[20px] border-2 border-black/70 bg-white/20">
                <table className="min-w-full border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-black/70 bg-white/35">
                      {usesBrandImageryLayout ? (
                        <>
                          <th
                            rowSpan={hasNestedMonthColumns ? 3 : 2}
                            className={`${STICKY_TABLE_LEAD_HEADER_CLASS} px-4 py-3 text-left font-semibold text-slate-900 whitespace-normal break-words leading-snug`}
                            style={{ width: BRAND_IMAGERY_ATTRIBUTE_COLUMN_WIDTH_PX, minWidth: BRAND_IMAGERY_ATTRIBUTE_COLUMN_WIDTH_PX }}
                          >
                            {groupedAttributeHeader}
                          </th>
                          <th
                            rowSpan={hasNestedMonthColumns ? 3 : 2}
                            className={`${STICKY_TABLE_SECOND_HEADER_CLASS} px-4 py-3 text-left font-semibold text-slate-900 whitespace-normal break-words leading-snug`}
                            style={{
                              left: BRAND_IMAGERY_ATTRIBUTE_COLUMN_WIDTH_PX,
                              width: BRAND_IMAGERY_BRAND_COLUMN_WIDTH_PX,
                              minWidth: BRAND_IMAGERY_BRAND_COLUMN_WIDTH_PX,
                            }}
                          >
                            {groupedSecondHeader}
                          </th>
                        </>
                      ) : (
                        <th
                          rowSpan={hasNestedMonthColumns ? 3 : 2}
                          className={`${STICKY_TABLE_LEAD_HEADER_CLASS} px-4 py-2 text-left font-semibold text-slate-900`}
                        >
                          Variable
                        </th>
                      )}
                      {showTotalColumn ? (
                        <th
                          rowSpan={hasNestedMonthColumns ? 3 : 2}
                          className={usesBrandImageryLayout
                            ? "border-r-2 border-black/70 px-3 py-3 text-center text-xs font-semibold uppercase tracking-wide text-slate-800 whitespace-normal break-words leading-snug"
                            : "border-r-2 border-black/70 px-3 py-2 text-center text-xs font-semibold uppercase tracking-wide text-slate-800"}
                        >
                          Total
                        </th>
                      ) : null}
                      {topBlocks.map((block) => (
                        <th
                          key={`${table.id}-${block.id}-group`}
                          colSpan={Math.max(1, block.columnLabels.length)}
                          className={usesBrandImageryLayout
                            ? "border-r-2 border-black/70 px-3 py-3 text-left text-xs font-semibold uppercase tracking-wide text-slate-800 whitespace-normal break-words leading-snug"
                            : "border-r-2 border-black/70 px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-slate-800"}
                        >
                          {block.topQuestion.label}
                        </th>
                      ))}
                    </tr>
                    <tr className="border-b border-black/70 bg-white/25">
                      {hasNestedMonthColumns ? (
                        topBlocks.flatMap((block) => {
                          const groups = block.columnGroups || [];
                          if (!groups.length) {
                            return [
                              <th
                                key={`${table.id}-${block.id}-group-empty`}
                                className={usesBrandImageryLayout
                                  ? "border-r border-black/60 px-3 py-3 text-center text-xs font-semibold text-slate-500"
                                  : "border-r border-black/60 px-3 py-2 text-center text-xs font-semibold text-slate-500"}
                              >
                                No data
                              </th>,
                            ];
                          }
                          return groups.map((group, groupIndex) => (
                            <th
                              key={`${table.id}-${block.id}-month-group-${groupIndex}-${group.label}`}
                              colSpan={Math.max(1, group.colSpan)}
                              className={usesBrandImageryLayout
                                ? "border-r border-black/60 px-3 py-3 text-center text-xs font-semibold text-sky-800 whitespace-normal break-words leading-snug"
                                : "border-r border-black/60 px-3 py-2 text-center text-xs font-semibold text-sky-800"}
                            >
                              {group.label}
                            </th>
                          ));
                        })
                      ) : (
                        topBlocks.flatMap((block) =>
                          block.columnLabels.length > 0
                            ? block.columnLabels.map((columnLabel, columnIndex) => (
                                <th
                                  key={`${table.id}-${block.id}-column-${columnIndex}-${columnLabel}`}
                                  className={usesBrandImageryLayout
                                    ? "border-r border-black/60 px-3 py-3 text-center text-xs font-semibold text-sky-700 whitespace-normal break-words leading-snug"
                                    : "border-r border-black/60 px-3 py-2 text-center text-xs font-semibold text-sky-700"}
                                >
                                  <div className="flex min-w-[88px] max-w-[120px] flex-col items-center gap-1 whitespace-normal break-words leading-snug">
                                    <span>{columnLabel}</span>
                                    {block.columnLetterLabels[columnIndex] ? (
                                      <span className="text-[11px] font-bold uppercase tracking-[0.14em] text-emerald-600">
                                        {block.columnLetterLabels[columnIndex]}
                                      </span>
                                    ) : null}
                                  </div>
                                </th>
                              ))
                            : [
                                <th
                                  key={`${table.id}-${block.id}-column-empty`}
                                  className={usesBrandImageryLayout
                                    ? "border-r border-black/60 px-3 py-3 text-center text-xs font-semibold text-slate-500"
                                    : "border-r border-black/60 px-3 py-2 text-center text-xs font-semibold text-slate-500"}
                                >
                                  No data
                                </th>,
                              ],
                        )
                      )}
                    </tr>
                    {hasNestedMonthColumns ? (
                      <tr className="border-b border-black/70 bg-white/20">
                        {topBlocks.flatMap((block) =>
                          block.columnLabels.length > 0
                            ? block.columnLabels.map((columnLabel, columnIndex) => (
                              <th
                                key={`${table.id}-${block.id}-column-${columnIndex}-${columnLabel}`}
                                className={usesBrandImageryLayout
                                  ? "border-r border-black/60 px-3 py-3 text-center text-xs font-semibold text-sky-700 whitespace-normal break-words leading-snug"
                                  : "border-r border-black/60 px-3 py-2 text-center text-xs font-semibold text-sky-700"}
                              >
                                <div className="flex min-w-[88px] max-w-[120px] flex-col items-center gap-1 whitespace-normal break-words leading-snug">
                                  <span>{columnLabel}</span>
                                  {block.columnLetterLabels[columnIndex] ? (
                                    <span className="text-[11px] font-bold uppercase tracking-[0.14em] text-emerald-600">
                                      {block.columnLetterLabels[columnIndex]}
                                    </span>
                                  ) : null}
                                </div>
                              </th>
                            ))
                          : [
                              <th
                                key={`${table.id}-${block.id}-column-empty`}
                                className={usesBrandImageryLayout
                                  ? "border-r border-black/60 px-3 py-3 text-center text-xs font-semibold text-slate-500"
                                  : "border-r border-black/60 px-3 py-2 text-center text-xs font-semibold text-slate-500"}
                              >
                                No data
                              </th>,
                            ],
                        )}
                      </tr>
                    ) : null}
                  </thead>
                  <tbody>
                    <tr className="border-b border-black/60 bg-white/20">
                      {usesBrandImageryLayout ? (
                        <>
                          <td
                            className={`${STICKY_TABLE_LEAD_CELL_CLASS} px-4 py-3 font-semibold text-slate-900`}
                            style={{ width: BRAND_IMAGERY_ATTRIBUTE_COLUMN_WIDTH_PX, minWidth: BRAND_IMAGERY_ATTRIBUTE_COLUMN_WIDTH_PX }}
                          >
                            N
                          </td>
                          <td
                            className={`${STICKY_TABLE_SECOND_CELL_CLASS} px-4 py-3 text-slate-500`}
                            style={{
                              left: BRAND_IMAGERY_ATTRIBUTE_COLUMN_WIDTH_PX,
                              width: BRAND_IMAGERY_BRAND_COLUMN_WIDTH_PX,
                              minWidth: BRAND_IMAGERY_BRAND_COLUMN_WIDTH_PX,
                            }}
                          >
                            &nbsp;
                          </td>
                        </>
                      ) : (
                        <td className={`${STICKY_TABLE_LEAD_CELL_CLASS} px-4 py-2 font-semibold text-slate-900`}>
                          N
                        </td>
                      )}
                      {showTotalColumn ? (
                        <td
                          className={usesBrandImageryLayout
                            ? "border-r-2 border-black/70 px-3 py-3 text-center font-bold text-slate-900"
                            : "border-r-2 border-black/70 px-3 py-2 text-center font-bold text-slate-900"}
                        >
                          {getCustomTableTotalColumnBase(table).toLocaleString()}
                        </td>
                      ) : null}
                      {topBlocks.flatMap((block) =>
                        block.columnLabels.length > 0
                          ? block.columnLabels.map((columnLabel, columnIndex) => (
                              <td
                                key={`${table.id}-${block.id}-column-base-${columnIndex}-${columnLabel}`}
                                className={usesBrandImageryLayout
                                  ? "border-r border-black/60 px-3 py-3 text-center font-bold text-slate-900"
                                  : "border-r border-black/60 px-3 py-2 text-center font-bold text-slate-900"}
                              >
                                {Number(block.columnBases[columnIndex] || 0).toLocaleString()}
                              </td>
                            ))
                          : [
                              <td
                                key={`${table.id}-${block.id}-column-base-empty`}
                                className={usesBrandImageryLayout
                                  ? "border-r border-black/60 px-3 py-3 text-center font-bold text-slate-500"
                                  : "border-r border-black/60 px-3 py-2 text-center font-bold text-slate-500"}
                              >
                                -
                              </td>,
                            ],
                      )}
                    </tr>

                    {renderedRows.map((displayRow) => (
                      <tr
                        key={`${table.id}-row-${displayRow.key}`}
                        className={usesBrandImageryLayout ? getBrandImageryRowBorderClass(displayRow) : "border-b border-black/60 last:border-b-0"}
                      >
                        {usesBrandImageryLayout ? (
                          <>
                            {displayRow.showAttribute ? (
                              <td
                                rowSpan={displayRow.attributeRowSpan}
                                className={`${STICKY_TABLE_LEAD_CELL_CLASS} ${getBrandImageryRowBorderClass(displayRow)} px-4 py-3 align-top font-semibold text-slate-900 whitespace-normal break-words leading-snug`}
                                style={{ width: BRAND_IMAGERY_ATTRIBUTE_COLUMN_WIDTH_PX, minWidth: BRAND_IMAGERY_ATTRIBUTE_COLUMN_WIDTH_PX }}
                              >
                                {displayRow.attribute}
                              </td>
                            ) : null}
                            <td
                              className={`${STICKY_TABLE_SECOND_CELL_CLASS} ${getBrandImageryRowBorderClass(displayRow)} px-4 py-3 font-medium text-slate-900 whitespace-normal break-words leading-snug`}
                              style={{
                                left: BRAND_IMAGERY_ATTRIBUTE_COLUMN_WIDTH_PX,
                                width: BRAND_IMAGERY_BRAND_COLUMN_WIDTH_PX,
                                minWidth: BRAND_IMAGERY_BRAND_COLUMN_WIDTH_PX,
                              }}
                            >
                              {displayRow.brand || "—"}
                            </td>
                          </>
                        ) : (
                          <td className={`${STICKY_TABLE_LEAD_CELL_CLASS} px-4 py-2 font-semibold text-slate-900`}>
                            {displayRow.rowLabel}
                          </td>
                        )}
                        {showTotalColumn ? (
                          <td
                            className={usesBrandImageryLayout
                              ? `${getBrandImageryRowBorderClass(displayRow)} border-r-2 border-black/70 px-3 py-3 text-center text-slate-800`
                              : "border-r-2 border-black/70 px-3 py-2 text-center text-slate-800"}
                          >
                            {formatCustomTableTotalColumnValue(normalizedResult, table, displayRow.originalIndex)}
                          </td>
                        ) : null}
                        {topBlocks.flatMap((block) =>
                          block.columnLabels.length > 0
                            ? block.columnLabels.map((columnLabel, columnIndex) => (
                                <td
                                  key={`${table.id}-${block.id}-cell-${displayRow.key}-${columnIndex}-${columnLabel}`}
                                className={usesBrandImageryLayout
                                  ? `${getBrandImageryRowBorderClass(displayRow)} border-r border-black/60 px-3 py-3 text-center text-slate-800`
                                  : "border-r border-black/60 px-3 py-2 text-center text-slate-800"}
                              >
                                  <div className="flex flex-col items-center justify-center gap-0.5 leading-tight">
                                    <span>
                                      {formatCustomTableMetricValue(
                                        Number(block.counts[displayRow.originalIndex]?.[columnIndex] || 0),
                                        getCustomTableCellRowBase(table, block, displayRow.originalIndex, columnIndex),
                                        Number(block.columnBases[columnIndex] || 0),
                                        getCustomTableCellTotalBase(table, block, columnIndex),
                                        normalizedResult.displayMode,
                                        normalizedResult.formatOptions,
                                      )}
                                    </span>
                                    {block.significanceLetters[displayRow.originalIndex]?.[columnIndex] ? (
                                      <span className="block text-[11px] font-bold uppercase tracking-[0.14em] text-emerald-600">
                                        {block.significanceLetters[displayRow.originalIndex][columnIndex]}
                                      </span>
                                    ) : null}
                                  </div>
                                </td>
                              ))
                            : [
                                <td
                                  key={`${table.id}-${block.id}-cell-empty-${displayRow.key}`}
                                  className={usesBrandImageryLayout
                                    ? `${getBrandImageryRowBorderClass(displayRow)} border-r border-black/60 px-3 py-3 text-center text-slate-500`
                                    : "border-r border-black/60 px-3 py-2 text-center text-slate-500"}
                                >
                                  -
                                </td>,
                              ],
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
                </div>
              </div>
            )}
          </article>
        );
      })}
    </section>
  );
}
function getAwarenessColumnFilters(filters: AwarenessFilterState): Record<string, string[]> {
  return {
    Region: filters.region,
    D3: filters.income,
    Gender: filters.gender,
    Age: filters.age,
    SEC: filters.sec,
    Week: filters.week,
  };
}

function inferAwarenessMetric(question: string, questionLabel: string): string | null {
  const q = String(question || "").trim();
  const label = String(questionLabel || "");
  const text = `${q} ${label}`;
  const normalized = text.replace(/[^a-z0-9]/gi, "").toUpperCase();
  const hasCode = (code: string) => normalized.includes(code.toUpperCase());
  const hasPrefixedCode = (code: string) => new RegExp(`\\b[A-Z0-9]+_${code}(?:_\\d+)?\\b`, "i").test(text);

  if (/(^|_)BAU1A$/i.test(q) || /\bbrand tom\b/i.test(text)) return "brand_tom";
  if (/(^|_)BAU1B(_\d+)?$/i.test(q) || /\bbrand spont\b/i.test(text)) return "brand_spont";
  if (/(^|_)BAU1C$/i.test(q) || /\bad tom\b/i.test(text)) return "ad_tom";
  if (/(^|_)BAU1D(_\d+)?$/i.test(q) || /\bad spont\b/i.test(text)) return "ad_spont";
  if (/(^|_)BAU2(_\d+)?$/i.test(q) || /\baided\b(?!\s*ad)/i.test(text)) return "aided";
  if (/(^|_)BAU3(_\d+)?$/i.test(q) || /\baided ad\b/i.test(text)) return "aided_ad";
  if (/\bBAU4(?:_\d+){0,2}\b/i.test(text)) return "media_source";
  if (/\bQBI(?:_\d+){0,3}\b/i.test(text)) return "brand_imagery";
  if (/(^|_)BAU5A(_\d+)?$/i.test(q) || hasPrefixedCode("BAU5A") || hasCode("BAU5A")) return "ever_consumed";
  if (/(^|_)BAU5C(_\d+)?$/i.test(q) || hasPrefixedCode("BAU5C") || hasCode("BAU5C")) return "last_3_months";
  if (/(^|_)BAU6A(_\d+)?$/i.test(q) || hasPrefixedCode("BAU6A") || hasCode("BAU6A")) return "last_1_month";
  if (/(^|_)BAU6B(_\d+)?$/i.test(q) || hasPrefixedCode("BAU6B") || hasCode("BAU6B")) return "last_7_days";
  if (/(^|_)BAU6C(_\d+)?$/i.test(q) || hasPrefixedCode("BAU6C") || hasCode("BAU6C")) {
    return "most_often_used";
  }
  if (/(^|_)BAU8(_\d+)?$/i.test(q) || hasPrefixedCode("BAU8") || hasCode("BAU8")) {
    return "prefrence";
  }
  return null;
}

function isBau4Question(question: string, questionLabel: string): boolean {
  const q = String(question || "").trim();
  const label = String(questionLabel || "").trim();
  return /\bBAU4(?:_\d+){0,2}\b/i.test(`${q} ${label}`);
}

function inferBrandFromMediaQuestion(question: string, questionLabel: string): string {
  const candidates = [String(question || "").trim(), String(questionLabel || "").trim()];

  for (const text of candidates) {
    if (!text) continue;
    if (/\{0\}/.test(text)) continue;

    // Preferred: "(BRAND) ... BAU4 ..."
    const paren = text.match(/^\s*\(([^)]+)\)/);
    if (paren && paren[1] && !/^\{0\}$/i.test(paren[1].trim())) {
      return paren[1].trim();
    }

    // Fallback: "BRAND_BAU4..."
    const coded = text.match(/^\s*([A-Za-z0-9][A-Za-z0-9 '&.\-]{0,50})_BAU4/i);
    if (coded && coded[1]) {
      return coded[1].trim();
    }
  }

  return "";
}

function inferBrandFromQbiQuestion(question: string, questionLabel: string): string {
  const candidates = [String(questionLabel || "").trim(), String(question || "").trim()];

  for (const text of candidates) {
    if (!text) continue;

    const allParens = Array.from(text.matchAll(/\(([^)]+)\)/g));
    if (allParens.length > 0) {
      const last = allParens[allParens.length - 1]?.[1]?.trim() || "";
      if (last && !/^\{0\}$/i.test(last) && !/^\{1\}$/i.test(last)) {
        return last;
      }
    }

    const coded = text.match(/^\s*([A-Za-z0-9][A-Za-z0-9 '&.\-]{0,50})_QBI/i);
    if (coded && coded[1]) {
      return coded[1].trim();
    }
  }

  return "";
}

function inferQbiStatementFromQuestion(question: string, questionLabel: string): string {
  const text = String(questionLabel || question || "").trim();
  if (!text) return "";
  const allParens = Array.from(text.matchAll(/\(([^)]+)\)/g));
  if (allParens.length > 0) {
    const first = allParens[0]?.[1]?.trim() || "";
    if (first && !/^\{0\}$/i.test(first)) {
      return first;
    }
    const last = allParens[allParens.length - 1]?.[1]?.trim() || "";
    if (last && !/^\{0\}$/i.test(last)) {
      return last;
    }
  }

  let cleaned = text;
  cleaned = cleaned.replace(/^\s*[A-Za-z0-9]+_QBI[0-9A-Za-z_]*\.?\s*/u, "");
  cleaned = cleaned.replace(/^\s*QBI[0-9A-Za-z_]*\.?\s*/iu, "");
  cleaned = cleaned.replace(/^\s*\{0\}\s*/u, "");
  const dotIndex = cleaned.indexOf(". ");
  if (dotIndex >= 0 && dotIndex < cleaned.length - 2) cleaned = cleaned.slice(dotIndex + 2);
  return cleaned.replace(/\s+/g, " ").trim();
}

function extractParentheticalOption(value: string): string {
  const text = String(value || "").trim();
  if (!text) return "";
  const matches = Array.from(text.matchAll(/\(([^)]+)\)/g));
  for (const match of matches) {
    const candidate = String(match[1] || "").trim();
    if (!candidate || /^\{0\}$/i.test(candidate)) continue;
    return candidate;
  }
  return "";
}

function extractFlavourOptionLabel(question: PageQuestionMonthly): string {
  const answerOption = String(question.answers?.[0]?.answer || "").trim();
  if (answerOption && answerOption !== "(No response)") return answerOption;
  return extractParentheticalOption(String(question.questionLabel || question.question || ""));
}

function inferFlexMetricKey(questionCode: string, mode: FlexViewMode): string {
  const code = String(questionCode || "").trim();
  const config = FLEX_MODE_CONFIG[mode];
  const metric = config.metricDefs.find((item) => item.matcher.test(code));
  return metric?.key || "";
}

function isPositiveMultiSelectAnswer(answer: string): boolean {
  const normalized = String(answer || "").trim().toLowerCase();
  if (!normalized) return false;
  if (["1", "1.0", "yes", "true", "selected", "agree", "agreed"].includes(normalized)) return true;
  if (/^strongly\s+agree$/i.test(normalized)) return true;
  return false;
}

async function fetchAwarenessSummary(
  slug: string,
  months?: string[],
  filters: AwarenessFilterState = { region: [], income: [], gender: [], age: [], sec: [], week: [] },
  subpageId?: string,
  signal?: AbortSignal,
  awarenessViewMode: AwarenessViewMode = "brand_awareness",
): Promise<{ rows: AwarenessSummaryRow[] }> {
  const normalizedSlug = String(slug || "").trim();
  if (!normalizedSlug) {
    return { rows: [] };
  }
  const base = trimTrailingSlashes(API_BASE);
  const monthsList = Array.isArray(months) ? months.filter(Boolean) : [];
  const awarenessFilters = filters;
  const normalizedSubpageId =
    subpageId === MEDIA_SOURCE_SUBPAGE_ID
      ? "media-source"
      : subpageId === USAGE_SUBPAGE_ID
        ? "usage"
        : subpageId === AWARENESS_SUBPAGE_ID || subpageId === LEGACY_BRAND_AWARENESS_SUBPAGE_ID || subpageId === LEGACY_AD_AWARENESS_SUBPAGE_ID
          ? "awareness"
          : subpageId || "";
  const cacheKey = JSON.stringify({
    slug: normalizedSlug,
    months: [...monthsList].sort(),
    filters: awarenessFilters,
    subpageId: normalizedSubpageId,
    awarenessViewMode,
  });
  const hasAnyAwarenessFilter = Object.values(awarenessFilters).some((items) => Array.isArray(items) && items.length > 0);

  const now = Date.now();
  const cached = awarenessSummaryCache.get(cacheKey);
  if (
    cached &&
    now - cached.ts <= AWARENESS_SUMMARY_CACHE_TTL_MS &&
    (cached.rows.length > 0 || hasAnyAwarenessFilter)
  ) {
    return { rows: cached.rows.slice() };
  }
  const inFlight = awarenessSummaryInFlight.get(cacheKey);
  if (inFlight) return inFlight;

  const loadPromise = (async () => {
    const data = await fetchJson<{ rows: AwarenessSummaryRow[] }>(`${base}/api/awareness-summary`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        slug: normalizedSlug,
        months: monthsList,
        filters: awarenessFilters,
        subpageId: normalizedSubpageId,
        mode: normalizedSubpageId,
      }),
      signal,
    });
    if (!data || !Array.isArray(data.rows)) {
      throw new Error("Invalid awareness summary response from backend");
    }
    const expectedMetrics = new Set(
      getAwarenessMetricDefs(subpageId || "", awarenessViewMode).map((metric) => metric.key.toLowerCase()),
    );
    const normalizedRows = (data.rows || []).filter((row) =>
      expectedMetrics.size === 0 ? true : expectedMetrics.has(String(row.metric || "").toLowerCase()),
    );

    if (normalizedRows.length > 0 || hasAnyAwarenessFilter) {
      awarenessSummaryCache.set(cacheKey, { ts: now, rows: normalizedRows });
    } else {
      awarenessSummaryCache.delete(cacheKey);
    }
    return { rows: normalizedRows };
  })();
  awarenessSummaryInFlight.set(cacheKey, loadPromise);
  try {
    return await loadPromise;
  } finally {
    awarenessSummaryInFlight.delete(cacheKey);
  }

}

async function fetchFlavourSummary(
  slug: string,
  months: string[],
  filters: AwarenessFilterState,
  signal?: AbortSignal,
): Promise<{ rows: AwarenessSummaryRow[]; metricDefs: AwarenessMetricDef[] }> {
  const normalizedSlug = String(slug || "").trim();
  if (!normalizedSlug) {
    return {
      rows: [],
      metricDefs: FLAVOUR_METRIC_DEFS,
    };
  }
  const columnFilters = getAwarenessColumnFilters(filters || { region: [], income: [], gender: [], age: [], sec: [], week: [] });
  const response = await fetchJson<PageDataMonthlyResponse>(`${API_BASE}/api/page-data-monthly`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal,
    body: JSON.stringify({
      slug: normalizedSlug,
      pageId: FLAVOUR_FLEX_PAGE_ID,
      months,
      questionPattern: "FQ1",
      filters: Object.keys(columnFilters).length > 0 ? columnFilters : undefined,
      includeRespondentBase: true,
      limitQuestions: 2000,
      limitAnswers: 50,
    }),
  });

  const respondentBaseByMonth = response.respondentBaseByMonth || {};
  const rowMap = new Map<string, AwarenessSummaryRow>();
  (response.questions || []).forEach((question) => {
    const monthKey = String(question.month || "").trim();
    if (!monthKey) return;
    (question.answers || []).forEach((answer) => {
      const answerLabel = String(answer.answer || "").trim();
      const optionLabel =
        answerLabel && answerLabel !== "(No response)" && !isPositiveMultiSelectAnswer(answerLabel)
          ? answerLabel
          : extractFlavourOptionLabel(question);
      if (!optionLabel || optionLabel === "(No response)") return;
      const mention = Number(answer.count || question.total || 0);
      const base = Math.max(Number(respondentBaseByMonth[monthKey] || 0), mention);
      const key = `${monthKey}__${optionLabel.toLowerCase()}`;
      const existing = rowMap.get(key);
      if (existing) {
        existing.mention_n = Math.max(existing.mention_n, mention);
        existing.base_n = Math.max(existing.base_n, base);
        existing.pct = existing.base_n > 0 ? Number(((existing.mention_n / existing.base_n) * 100).toFixed(1)) : 0;
        return;
      }
      rowMap.set(key, {
        month: monthKey,
        brand: optionLabel,
        metric: FLAVOUR_METRIC_KEY,
        mention_n: mention,
        base_n: base,
        pct: base > 0 ? Number(((mention / base) * 100).toFixed(1)) : 0,
      });
    });
  });

  return {
    rows: Array.from(rowMap.values()),
    metricDefs: FLAVOUR_METRIC_DEFS,
  };
}

async function fetchFlexSummary(
  slug: string,
  months: string[],
  filters: AwarenessFilterState,
  mode: FlexViewMode,
  selectedQuestionKey: FlexQuestionKey,
  signal?: AbortSignal,
): Promise<{ rows: AwarenessSummaryRow[]; metricDefs: AwarenessMetricDef[] }> {
  const normalizedSlug = String(slug || "").trim();
  const config = FLEX_MODE_CONFIG[mode];
  const selectedQuestionOption = config.questionOptions.find((option) => option.key === selectedQuestionKey) || config.questionOptions[0];
  const selectedMetricDef = config.metricDefs.find((metric) => metric.key === selectedQuestionOption?.key) || config.metricDefs[0];
  if (!normalizedSlug) {
    return {
      rows: [],
      metricDefs: selectedMetricDef ? [selectedMetricDef] : [],
    };
  }

  const columnFilters = getAwarenessColumnFilters(filters || { region: [], income: [], gender: [], age: [], sec: [], week: [] });
  const response = await fetchJson<PageDataMonthlyResponse>(`${API_BASE}/api/question-data-monthly`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal,
    body: JSON.stringify({
      slug: normalizedSlug,
      months,
      questionPattern: selectedQuestionOption?.questionPattern || config.questionPattern,
      filters: Object.keys(columnFilters).length > 0 ? columnFilters : undefined,
      limitQuestions: 2000,
      limitAnswers: 200,
    }),
  });

  const rows: AwarenessSummaryRow[] = [];
  (response.questions || []).forEach((question) => {
    const metricKey = inferFlexMetricKey(String(question.question || ""), mode);
    const base = Number(question.total || 0);
    const monthKey = String(question.month || "").trim();
    if (!metricKey || metricKey !== selectedMetricDef?.key || !monthKey || base <= 0) return;

    (question.answers || []).forEach((answer) => {
      const label = String(answer.answer || "").trim();
      const mention = Number(answer.count || 0);
      if (!label || label === "(No response)") return;
      rows.push({
        month: monthKey,
        brand: label,
        metric: metricKey,
        mention_n: mention,
        base_n: base,
        pct: base > 0 ? Number(((mention / base) * 100).toFixed(1)) : 0,
      });
    });
  });

  return {
    rows,
    metricDefs: selectedMetricDef ? [selectedMetricDef] : [],
  };
}

function getLatestMonthKey(months: string[]): string {
  return [...months]
    .filter((month) => parseMonthKey(month))
    .sort((a, b) => (a > b ? -1 : a < b ? 1 : 0))[0] || "";
}

function formatMonthLabel(month: string, latestMonthKey = ""): string {
  void latestMonthKey;
  const monthMatch = month.trim().match(/^(\d{4})-(\d{2})$/);
  if (!monthMatch) return month;

  const [, year, monthNum] = monthMatch;
  const date = new Date(Number(year), Number(monthNum) - 1, 1);
  if (Number.isNaN(date.getTime())) return month;

  return MONTH_LABEL_FORMATTER.format(date).replace(" ", "-");
}

function toFileToken(value: string): string {
  return String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/_+/g, "_");
}

function downloadOverviewCardData(title: string, values: DemoBucket[], categoryTitle = "category"): void {
  const leftHeader = String(title || "Value");
  const rows: (string | number)[][] = [[leftHeader, "Count", "Percent"]];
  values.forEach((item) => {
    rows.push([String(item.value || ""), Number(item.count || 0), Number(item.pct || 0)]);
  });
  const worksheet = XLSX.utils.aoa_to_sheet(rows);
  (worksheet as unknown as { ["!cols"]?: Array<{ wch: number }> })["!cols"] = [
    { wch: 34 },
    { wch: 14 },
    { wch: 14 },
  ];

  const borderStyle = {
    top: { style: "thin", color: { rgb: "000000" } },
    bottom: { style: "thin", color: { rgb: "000000" } },
    left: { style: "thin", color: { rgb: "000000" } },
    right: { style: "thin", color: { rgb: "000000" } },
  };

  const applyStyle = (r: number, c: number, style: Record<string, unknown>) => {
    const cellRef = XLSX.utils.encode_cell({ r, c });
    const cell = (worksheet as Record<string, unknown>)[cellRef] as { s?: Record<string, unknown> } | undefined;
    if (!cell) return;
    cell.s = { ...(cell.s ?? {}), ...style };
  };

  for (let r = 0; r < rows.length; r += 1) {
    for (let c = 0; c < 3; c += 1) {
      applyStyle(r, c, { border: borderStyle });
    }
  }

  for (let c = 0; c < 3; c += 1) {
    applyStyle(0, c, {
      font: { bold: true },
      fill: { fgColor: { rgb: "E6E6E6" } },
      alignment: { horizontal: "center", vertical: "center" },
    });
  }

  for (let r = 1; r < rows.length; r += 1) {
    applyStyle(r, 0, { alignment: { horizontal: "left", vertical: "center" } });
    applyStyle(r, 1, { alignment: { horizontal: "center", vertical: "center" }, numFmt: "0" });
    applyStyle(r, 2, { alignment: { horizontal: "center", vertical: "center" }, numFmt: "0.0\\%" });
  }

  const workbook = XLSX.utils.book_new();
  const sheetName = String(title || "Overview").replace(/[\\/*?:[\]]/g, "").slice(0, 31);
  XLSX.utils.book_append_sheet(workbook, worksheet, sheetName || "Overview");
  const filename = `${toFileToken(categoryTitle)}_${toFileToken(title)}_overview_card`
    .replace(/^_+|_+$/g, "")
    .replace(/_+/g, "_");
  XLSX.writeFile(workbook, `${filename || "overview_card"}.xlsx`, { bookType: "xlsx", compression: true });
}

function parseMonthKey(month: string): { year: number; month: number } | null {
  const match = String(month || "").trim().match(/^(\d{4})-(\d{2})$/);
  if (!match) return null;
  const year = Number(match[1]);
  const monthNum = Number(match[2]);
  if (!Number.isFinite(year) || !Number.isFinite(monthNum) || monthNum < 1 || monthNum > 12) return null;
  return { year, month: monthNum };
}

async function fetchPurchaseBehaviourSummary(
  slug: string,
  months: string[],
  filters: AwarenessFilterState,
  mode: PurchaseBehaviourMode,
  signal?: AbortSignal,
): Promise<{ rows: AwarenessSummaryRow[]; metricDefs: AwarenessMetricDef[] }> {
  const normalizedSlug = String(slug || "").trim();
  const normalizedMonths = Array.isArray(months) ? months.filter(Boolean) : [];
  const modeConfig =
    mode === "pack_size_frequency"
      ? {
          codeMatcher: /(^|_)BAU10(?:_[0-9]+)?$/i,
          textMatcher: /\bBAU10\b/i,
          key: "pack_size_frequency",
          label: "Pack Size Frequency",
        }
      : {
          codeMatcher: /(^|_)BAU9(?:B)?(?:_[0-9]+)?$/i,
          textMatcher: /\bBAU9B?\b|how often do you purchase/i,
          key: "purchase_frequency",
          label: "Purchase Frequency",
        };

  if (!normalizedSlug) {
    return {
      rows: [],
      metricDefs: [{ key: modeConfig.key, label: modeConfig.label, matcher: new RegExp(modeConfig.key, "i") }],
    };
  }

  const columnFilters = getAwarenessColumnFilters(filters || { region: [], income: [], gender: [], age: [], sec: [], week: [] });
  const runForMonths = async (monthList: string[]): Promise<AwarenessSummaryRow[]> => {
    const payload: {
      slug: string;
      pageId: string;
      months?: string[];
      questionPattern?: string;
      filters?: Record<string, string[]>;
      limitQuestions: number;
      limitAnswers: number;
    } = {
      slug: normalizedSlug,
      pageId: AWARENESS_PAGE_ID,
      questionPattern: mode === "pack_size_frequency" ? "BAU10" : "BAU9|how often do you purchase",
      limitQuestions: 2000,
      limitAnswers: 200,
    };
    if (monthList.length > 0) payload.months = monthList;
    if (Object.keys(columnFilters).length > 0) payload.filters = columnFilters;

    const response = await fetchJson<{ questions: PageQuestionMonthly[] }>(`${API_BASE}/api/page-data-monthly`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
    });

    const rowMap = new Map<string, AwarenessSummaryRow>();
    (response.questions || []).forEach((question) => {
      const month = String(question.month || "");
      const questionCode = String(question.question || "").trim();
      const questionText = `${questionCode} ${String(question.questionLabel || "")}`;
      if (!modeConfig.codeMatcher.test(questionCode) && !modeConfig.textMatcher.test(questionText)) return;
      const base = Number(question.total || 0);

      (question.answers || []).forEach((answer) => {
        const brand = String(answer.answer || "").trim();
        if (!brand || brand === "(No response)") return;
        const mention = Number(answer.count || 0);
        const key = `${month}__${brand.toLowerCase()}`;
        const existing = rowMap.get(key);
        if (existing) {
          existing.mention_n = Math.max(existing.mention_n, mention);
          existing.base_n = Math.max(existing.base_n, base);
          existing.pct = existing.base_n > 0
            ? Number(((existing.mention_n / existing.base_n) * 100).toFixed(1))
            : 0;
          return;
        }
        rowMap.set(key, {
          month,
          brand,
          metric: modeConfig.key,
          mention_n: mention,
          base_n: base,
          pct: base > 0 ? Number(((mention / base) * 100).toFixed(1)) : 0,
        });
      });
    });
    return Array.from(rowMap.values());
  };

  const monthList = normalizedMonths;
  let allRows = await runForMonths(monthList);
  if (allRows.length === 0 && monthList.length > 0) {
    allRows = await runForMonths([]);
  }

  return {
    rows: allRows,
    metricDefs: [{ key: modeConfig.key, label: modeConfig.label, matcher: new RegExp(modeConfig.key, "i") }],
  };
}

async function fetchBrandImagerySummary(
  slug: string,
  months: string[],
  filters: AwarenessFilterState,
  signal?: AbortSignal,
): Promise<{ rows: AwarenessSummaryRow[]; metricDefs: AwarenessMetricDef[] }> {
  const normalizedSlug = String(slug || "").trim();
  const normalizedMonths = Array.isArray(months) ? months.filter(Boolean) : [];
  if (!normalizedSlug) {
    return {
      rows: [],
      metricDefs: [{ key: "brand_imagery", label: "Brand Imagery", matcher: /_QBI\b|QBI\b|brand imagery/i }],
    };
  }
  const columnFilters = getAwarenessColumnFilters(filters || { region: [], income: [], gender: [], age: [], sec: [], week: [] });
  const runForMonths = async (monthList: string[]): Promise<AwarenessSummaryRow[]> => {
    const payload: {
      slug: string;
      pageId: string;
      months?: string[];
      questionPattern?: string;
      filters?: Record<string, string[]>;
      limitQuestions: number;
      limitAnswers: number;
    } = {
      slug: normalizedSlug,
      pageId: BRAND_IMAGERY_PAGE_ID,
      questionPattern: "QBI",
      limitQuestions: 5000,
      limitAnswers: 200,
    };
    if (monthList.length > 0) payload.months = monthList;
    if (Object.keys(columnFilters).length > 0) payload.filters = columnFilters;

    const response = await fetchJson<{ questions: PageQuestionMonthly[] }>(`${API_BASE}/api/page-data-monthly`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
    });

    const rowMap = new Map<string, AwarenessSummaryRow>();
    (response.questions || []).forEach((question) => {
      const month = String(question.month || "");
      const questionCode = String(question.question || "").trim();
      const questionText = `${questionCode} ${String(question.questionLabel || "")}`;
      if (!/(^|_)QBI_[0-9]+$/i.test(questionCode) && !/\bQBI(?:_\d+){0,3}\b/i.test(questionText)) return;
      const statement = inferQbiStatementFromQuestion(question.question, question.questionLabel);
      if (!statement || statement === "(No response)") return;
      const base = Number(question.total || 0);
      (question.answers || []).forEach((answer) => {
        const answerText = String(answer.answer || "").trim();
        const brand = inferBrandFromQbiQuestion(answerText, answerText);
        if (!brand || brand === "(No response)" || /^none(?: of these)?$/i.test(brand)) return;
        if (/^other/i.test(brand) || /^don't know$/i.test(brand)) return;
        const mention = Number(answer.count || 0);
        const key = `${month}__${statement.toLowerCase()}__${brand.toLowerCase()}`;
        const existing = rowMap.get(key);
        if (existing) {
          existing.mention_n = Math.max(existing.mention_n, mention);
          existing.base_n = Math.max(existing.base_n, base);
          existing.pct = existing.base_n > 0
            ? Number(((existing.mention_n / existing.base_n) * 100).toFixed(1))
            : 0;
          return;
        }
        rowMap.set(key, {
          month,
          brand,
          option: statement,
          metric: "brand_imagery",
          mention_n: mention,
          base_n: base,
          pct: base > 0 ? Number(((mention / base) * 100).toFixed(1)) : 0,
        });
      });
    });
    return Array.from(rowMap.values());
  };

  const monthList = normalizedMonths;
  let allRows = await runForMonths(monthList);
  if (allRows.length === 0 && monthList.length > 0) {
    allRows = await runForMonths([]);
  }

  return {
    rows: allRows,
    metricDefs: [{ key: "brand_imagery", label: "Brand Imagery", matcher: /_QBI\b|QBI\b|brand imagery/i }],
  };
}

function inferCampaignMediaSource(label: string): string {
  const text = String(label || "").trim();
  if (!text) return "";
  const matches = Array.from(text.matchAll(/\(([^)]+)\)/g));
  if (matches.length > 0) {
    const last = matches[matches.length - 1]?.[1]?.trim() || "";
    if (last && !/^other/i.test(last)) return last;
    if (last) return last;
  }
  const dotIdx = text.indexOf(". ");
  if (dotIdx >= 0 && dotIdx < text.length - 2) return text.slice(dotIdx + 2).trim();
  return text;
}

async function fetchCampaignCheckSummary(
  slug: string,
  months: string[],
  filters: AwarenessFilterState,
  mode: CampaignCheckMode,
  signal?: AbortSignal,
): Promise<{ rows: AwarenessSummaryRow[]; metricDefs: AwarenessMetricDef[] }> {
  const normalizedSlug = String(slug || "").trim();
  const columnFilters = getAwarenessColumnFilters(filters || { region: [], income: [], gender: [], age: [], sec: [], week: [] });
  const monthList = Array.isArray(months) ? months.filter(Boolean) : [];
  const metricDefs: AwarenessMetricDef[] = [
    {
      key: mode === "ad_recall" ? "ad_recall" : "ad_recall_media_source",
      label: mode === "ad_recall" ? "Ad Recall" : "Ad Recall Media Source",
      matcher: mode === "ad_recall" ? /A1|A5|QB1|QFH1|QFW1|QFS1|ad recall|remember seeing|seen this ad/i : /A2|QB2|media source|where did you see/i,
    },
  ];

  if (!normalizedSlug) {
    return {
      rows: [],
      metricDefs,
    };
  }

  const loadQuestions = async (targetMonths: string[]): Promise<PageQuestionMonthly[]> => {
    const payload: {
      slug: string;
      pageId: string;
      months?: string[];
      questionPattern?: string;
      filters?: Record<string, string[]>;
      limitQuestions: number;
      limitAnswers: number;
    } = {
      slug: normalizedSlug,
      pageId: CAMPAIGN_CHECK_PAGE_ID,
      questionPattern: mode === "ad_recall"
        ? "A1|A5|QB1|QFH1|QFW1|QFS1|do you remember seeing this advert recently|have you seen this ad before"
        : "A2|QB2|where did you see this advert|media source",
      limitQuestions: 2000,
      limitAnswers: 200,
    };
    if (targetMonths.length > 0) payload.months = targetMonths;
    if (Object.keys(columnFilters).length > 0) payload.filters = columnFilters;
    const response = await fetchJson<{ questions: PageQuestionMonthly[] }>(`${API_BASE}/api/page-data-monthly`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
    });
    return response.questions || [];
  };

  const extractRows = (questions: PageQuestionMonthly[]): AwarenessSummaryRow[] => {
    const rows: AwarenessSummaryRow[] = [];
    const byMonth = new Map<string, PageQuestionMonthly[]>();
    questions.forEach((question) => {
      const monthKey = String(question.month || "");
      const list = byMonth.get(monthKey) || [];
      list.push(question);
      byMonth.set(monthKey, list);
    });

    byMonth.forEach((monthQuestions, monthKey) => {
      const a1Questions = monthQuestions.filter((question) =>
        /(^|_)(?:A1|A5|QB1|QFH1|QFW1|QFS1)(?:_[0-9]+)?$/i.test(String(question.question || "").trim()) ||
        /do you remember seeing this advert recently|have you seen this ad before/i.test(String(question.questionLabel || "")),
      );
      const a1YesBase = a1Questions.reduce((maxBase, question) => {
        const yesAnswer = (question.answers || []).find((ans) => /^yes$/i.test(String(ans.answer || "").trim()));
        const yesCount = Number(yesAnswer?.count || 0);
        const qTotal = Number(question.total || 0);
        return Math.max(maxBase, yesCount || qTotal || 0);
      }, 0);

      monthQuestions.forEach((question) => {
        const code = String(question.question || "").trim();
        const label = String(question.questionLabel || "").trim();
        const isA1 =
          /(^|_)(?:A1|A5|QB1|QFH1|QFW1|QFS1)(?:_[0-9]+)?$/i.test(code) ||
          /do you remember seeing this advert recently|have you seen this ad before/i.test(label);
        const isA2 =
          /(^|_)(?:A2|QB2)(?:_[0-9]+)?$/i.test(code) ||
          /where did you see this advert|media source/i.test(label);
        if (mode === "ad_recall" && !isA1) return;
        if (mode === "ad_recall_media_source" && !isA2) return;
        const base = Number(question.total || 0);
        if (mode === "ad_recall") {
          (question.answers || []).forEach((answer) => {
            const answerText = String(answer.answer || "").trim();
            if (!answerText || answerText === "(No response)") return;
            const mention = Number(answer.count || 0);
            rows.push({
              month: monthKey,
              brand: answerText,
              metric: "ad_recall",
              mention_n: mention,
              base_n: base,
              pct: base > 0 ? Number(((mention / base) * 100).toFixed(1)) : 0,
            });
          });
          return;
        }
        const sourceFromAnswer = String(question.answers?.[0]?.answer || "").trim();
        const source = sourceFromAnswer || inferCampaignMediaSource(label);
        if (!source) return;
        const mention = Number(question.answers?.[0]?.count || question.total || 0);
        const mediaBase = a1YesBase > 0 ? a1YesBase : base;
        rows.push({
          month: monthKey,
          brand: source,
          metric: "ad_recall_media_source",
          mention_n: mention,
          base_n: mediaBase,
          pct: mediaBase > 0 ? Number(((mention / mediaBase) * 100).toFixed(1)) : 0,
        });
      });
    });

    return rows;
  };

  let allRows = extractRows(await loadQuestions(monthList));
  if (allRows.length === 0 && monthList.length > 0) {
    allRows = extractRows(await loadQuestions([]));
  }

  return { rows: allRows, metricDefs };
}

function buildQuarterGroups(months: string[]): MonthButtonGroup[] {
  const monthSet = new Set(months);
  const quarterMap = new Map<string, { year: number; quarter: number; months: string[] }>();

  months.forEach((monthKey) => {
    const parsed = parseMonthKey(monthKey);
    if (!parsed) return;
    const { year, month } = parsed;
    const quarter = Math.ceil(month / 3);
    const quarterStart = (quarter - 1) * 3 + 1;
    const quarterMonths = [quarterStart, quarterStart + 1, quarterStart + 2];
    const quarterMonthKeys = quarterMonths.map((m) => `${year}-${String(m).padStart(2, "0")}`);
    const key = `${year}-Q${quarter}`;
    if (!quarterMap.has(key)) {
      quarterMap.set(key, { year, quarter, months: quarterMonthKeys });
    }
  });

  return Array.from(quarterMap.values())
    .sort((a, b) => (b.year - a.year) || (b.quarter - a.quarter))
    .map((group) => {
      const availableMonths = group.months.filter((m) => monthSet.has(m));
      const hasAnyData = availableMonths.length > 0;
      return {
        id: `${group.year}-Q${group.quarter}`,
        label: `Q${group.quarter}-${group.year}`,
        months: availableMonths,
        disabled: !hasAnyData,
      };
    });
}

function buildRollingGroups(months: string[], latestMonthKey = ""): MonthButtonGroup[] {
  const asc = [...months].sort((a, b) => (a > b ? 1 : a < b ? -1 : 0));
  if (asc.length < 2) return [];
  const groups: MonthButtonGroup[] = [];
  for (let i = asc.length - 1; i >= 1; i -= 1) {
    const prev = asc[i - 1];
    const curr = asc[i];
    const prevLabel = formatMonthLabel(prev, latestMonthKey).replace(/-(\d{4})$/, (_, y) => `-${String(y).slice(-2)}`);
    const currLabel = formatMonthLabel(curr, latestMonthKey).replace(/-(\d{4})$/, (_, y) => `-${String(y).slice(-2)}`);
    groups.push({
      id: `rolling-${prev}-${curr}`,
      label: `${currLabel} - ${prevLabel}`,
      months: [prev, curr],
      disabled: false,
    });
  }
  return groups;
}

function buildMonthButtonGroupsForMode(mode: MonthSelectionMode, months: string[], latestMonthKey = ""): MonthButtonGroup[] {
  if (mode === "quarter") {
    const groups = buildQuarterGroups(months);
    if (groups.length > 0) return groups;
  }
  if (mode === "rolling") {
    const groups = buildRollingGroups(months, latestMonthKey);
    if (groups.length > 0) return groups;
  }
  return months.map((month) => ({
    id: month,
    label: formatMonthLabel(month, latestMonthKey),
    months: [month],
    disabled: false,
  }));
}

function selectMonthGroupIdsForMonths(groups: MonthButtonGroup[], months: string[]): string[] {
  const selectedMonthSet = new Set(months.filter(Boolean));
  const enabledGroups = groups.filter((group) => !group.disabled);
  if (!enabledGroups.length || !selectedMonthSet.size) return [];

  const fullyContained = enabledGroups.filter(
    (group) => group.months.length > 0 && group.months.every((month) => selectedMonthSet.has(month)),
  );
  const matchedGroups = fullyContained.length > 0
    ? fullyContained
    : enabledGroups.filter((group) => group.months.some((month) => selectedMonthSet.has(month)));

  return matchedGroups.map((group) => group.id);
}

function filterAwarenessSubpageQuestions(subpageId: string, questions: PageQuestion[]): PageQuestion[] {
  if (!subpageId.startsWith(AWARENESS_SUBPAGE_PREFIX)) return questions;

  const matchers: Record<string, RegExp> = {
    [`${AWARENESS_SUBPAGE_PREFIX}usage`]: /\b(usage|used|use|consume|consumption|trial)\b/i,
  };

  const matcher = matchers[subpageId];
  if (!matcher) return questions;

  const filtered = questions.filter((item) => matcher.test(`${item.question || ""} ${item.questionLabel || ""}`));
  return filtered.length > 0 ? filtered : questions;
}

function questionMatchesCode(questionCode: string, expectedCode: string): boolean {
  const normalizedQuestionCode = String(questionCode || "").trim().toUpperCase();
  const normalizedExpectedCode = String(expectedCode || "").trim().toUpperCase();
  if (!normalizedQuestionCode || !normalizedExpectedCode) return false;
  return normalizedQuestionCode === normalizedExpectedCode || normalizedQuestionCode.startsWith(`${normalizedExpectedCode}_`);
}

function filterCampaignCheckSubpageQuestions(slug: string, subpageId: string, questions: PageQuestion[]): PageQuestion[] {
  if (slug !== SNACKS_CAMPAIGN_CHECK_SLUG) return questions;
  if (!subpageId.startsWith(CAMPAIGN_CHECK_SUBPAGE_PREFIX)) return questions;

  const allowedQuestionCodes = SNACKS_CAMPAIGN_CHECK_CODES_BY_SUBPAGE[subpageId] || [];
  if (allowedQuestionCodes.length === 0) return [];

  return questions.filter((item) => allowedQuestionCodes.some((code) => questionMatchesCode(item.question, code)));
}

function filterPageQuestionsForSelectedPage(slug: string, selectedPageId: string, questions: PageQuestion[]): PageQuestion[] {
  const awarenessFiltered = filterAwarenessSubpageQuestions(selectedPageId, questions);
  return filterCampaignCheckSubpageQuestions(slug, selectedPageId, awarenessFiltered);
}

function getAwarenessMetricDefs(subpageId: string, awarenessViewMode: AwarenessViewMode = "brand_awareness"): AwarenessMetricDef[] {
  if (subpageId === MEDIA_SOURCE_SUBPAGE_ID) {
    return [{ key: "media_source", label: "Media Source", matcher: /_BAU4\b|BAU4\b|media source/i }];
  }
  if (subpageId === BRAND_IMAGERY_PAGE_ID) {
    return [{ key: "brand_imagery", label: "Brand Imagery", matcher: /_QBI\b|QBI\b|brand imagery/i }];
  }

  if (subpageId === USAGE_SUBPAGE_ID) {
    return [
      { key: "ever_consumed", label: "Ever Consumed", matcher: /_BAU5a\b|BAU5a\b|ever consumed/i },
      { key: "last_3_months", label: "Last 3 Months", matcher: /_BAU5c\b|BAU5c\b|last 3 months/i },
      { key: "last_1_month", label: "Last 1 Month", matcher: /_BAU6a\b|BAU6a\b|last 1 month/i },
      { key: "last_7_days", label: "Last 7 Days", matcher: /_BAU6b\b|BAU6b\b|last 7 days/i },
      { key: "most_often_used", label: "Most Often Used", matcher: /_BAU6c\b|BAU6c\b|most often used/i },
      { key: "prefrence", label: "Prefrence", matcher: /_BAU8\b|BAU8\b|preference|prefrence/i },
    ];
  }

  if (
    subpageId === AWARENESS_SUBPAGE_ID ||
    subpageId === LEGACY_BRAND_AWARENESS_SUBPAGE_ID ||
    subpageId === LEGACY_AD_AWARENESS_SUBPAGE_ID
  ) {
    const forceBrand = subpageId === LEGACY_BRAND_AWARENESS_SUBPAGE_ID;
    const forceAd = subpageId === LEGACY_AD_AWARENESS_SUBPAGE_ID;
    const mode: AwarenessViewMode = forceBrand
      ? "brand_awareness"
      : forceAd
        ? "ad_awareness"
        : awarenessViewMode;
    if (mode === "ad_awareness") {
      return [
        { key: "ad_tom", label: "AD TOM", matcher: /_BAU1c\b|BAU1c\b|ad tom/i },
        { key: "ad_spont", label: "AD SPONT", matcher: /_BAU1d\b|BAU1d\b|ad spont/i },
        { key: "aided_ad", label: "AIDED AD", matcher: /_BAU3\b|BAU3\b|aided ad/i },
        { key: "total_ad_awareness", label: "Total Ad Awareness", matcher: /total ad awareness/i },
      ];
    }
    return [
      { key: "brand_tom", label: "Brand TOM", matcher: /_BAU1a\b|BAU1a\b|brand tom/i },
      { key: "brand_spont", label: "Brand SPONT", matcher: /_BAU1b\b|BAU1b\b|brand spont/i },
      { key: "aided", label: "AIDED", matcher: /_BAU2\b|BAU2\b|\baided\b(?!\s*ad)/i },
      { key: "total_awareness", label: "Total Awareness", matcher: /total awareness/i },
    ];
  }
  return [];
}

function AwarenessTrendTable({
  metricDefs,
  months,
  data,
  brandUniverse = [],
  optionUniverse = [],
  loading = false,
  mediaRows = [],
  tableTitle = "Awareness",
  categoryTitle = "",
  classicOnly = false,
  lineAsPie = false,
  hideChartSelectors = false,
  activeSelections = [],
  useGoldAccent = false,
  rowHeaderLabel,
  emptyStateMessage,
}: {
  metricDefs: AwarenessMetricDef[];
  months: string[];
  data: AwarenessMonthTrend[];
  brandUniverse?: string[];
  optionUniverse?: string[];
  loading?: boolean;
  mediaRows?: AwarenessSummaryRow[];
  tableTitle?: string;
  categoryTitle?: string;
  classicOnly?: boolean;
  lineAsPie?: boolean;
  hideChartSelectors?: boolean;
  activeSelections?: ActiveSelectionChip[];
  useGoldAccent?: boolean;
  rowHeaderLabel?: string;
  emptyStateMessage?: string;
}) {
  const monthMap = useMemo(() => {
    const map = new Map<string, AwarenessMonthTrend>();
    data.forEach((item) => map.set(item.month, item));
    return map;
  }, [data]);

  const brands = useMemo(() => {
    const set = new Set<string>();
    brandUniverse.forEach((b) => {
      if (b && b !== "(No response)") set.add(b);
    });
    data.forEach((month) => {
      metricDefs.forEach((metric) => {
        Object.keys(month.metrics[metric.key] || {}).forEach((brand) => {
          if (brand && brand !== "(No response)") set.add(brand);
        });
      });
    });
    return Array.from(set);
  }, [brandUniverse, data, metricDefs]);

  const sortedBrands = useMemo(() => {
    if (!brands.length) return [];
    const universeOrder = new Map(brandUniverse.map((brand, index) => [brand, index]));
    return [...brands].sort((a, b) => {
      const specialOrder = compareDashboardLabelsWithPinnedLast(a, b);
      if (specialOrder !== 0) return specialOrder;
      const aOrder = universeOrder.get(a) ?? Number.MAX_SAFE_INTEGER;
      const bOrder = universeOrder.get(b) ?? Number.MAX_SAFE_INTEGER;
      if (aOrder !== bOrder) return aOrder - bOrder;
      return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
    });
  }, [brandUniverse, brands]);
  const isOptionByBrandMode =
    metricDefs.length === 1 && (metricDefs[0]?.key === "media_source" || metricDefs[0]?.key === "brand_imagery");
  const mediaOptions = useMemo(() => {
    const set = new Set<string>();
    const totals = new Map<string, number>();
    optionUniverse.forEach((value) => {
      const option = String(value || "").trim();
      if (!option || option === "(No response)") return;
      set.add(option);
      if (!totals.has(option)) totals.set(option, 0);
    });
    mediaRows.forEach((row) => {
      const option = String(row.option || "").trim();
      if (option && option !== "(No response)") {
        set.add(option);
        totals.set(option, Number(totals.get(option) || 0) + Number(row.mention_n || 0));
      }
    });
    return Array.from(set).sort((a, b) => {
      const specialOrder = compareDashboardLabelsWithPinnedLast(a, b);
      if (specialOrder !== 0) return specialOrder;
      const totalDiff = Number(totals.get(b) || 0) - Number(totals.get(a) || 0);
      if (totalDiff !== 0) return totalDiff;
      return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
    });
  }, [mediaRows, optionUniverse]);
  const mediaCellMap = useMemo(() => {
    const map = new Map<string, { pct: number; count: number; base: number }>();
    mediaRows.forEach((row) => {
      const option = String(row.option || "").trim();
      if (!option) return;
      const key = `${row.option}__${row.brand}__${row.month}`;
      map.set(key, { pct: Number(row.pct || 0), count: Number(row.mention_n || 0), base: Number(row.base_n || 0) });
    });
    return map;
  }, [mediaRows]);
  const getMediaCellNumericValue = (option: string, month: string, brand: string) => {
    const cell = mediaCellMap.get(`${option}__${brand}__${month}`);
    return valueMode === "count" ? Number(cell?.count || 0) : Number(cell?.pct || 0);
  };

  const [viewMode, setViewMode] = useState<"table" | "chart">("table");
  const [chartType, setChartType] = useState<"classic" | "grouped" | "line" | "pie">("classic");
  const [valueMode, setValueMode] = useState<"percent" | "count">("percent");
  const [percentFormatStep, setPercentFormatStep] = useState(0);
  const percentDecimals = percentFormatStep % 3;
  const showPercentSymbol = percentFormatStep < 3;
  const [groupedMetricKey, setGroupedMetricKey] = useState<string>("");
  const [lineMetricKey, setLineMetricKey] = useState<string>("");
  const resolvedRowHeaderLabel = rowHeaderLabel || (isOptionByBrandMode ? (metricDefs[0]?.key === "brand_imagery" ? "Brand Imagery" : "Media Source") : "Brand");

  const getCellNumericValue = (metricKey: string, month: string, brand: string) =>
    valueMode === "count"
      ? Number(monthMap.get(month)?.counts?.[metricKey]?.[brand] || 0)
      : Number(monthMap.get(month)?.metrics?.[metricKey]?.[brand] || 0);

  const formatCellValue = (value: number) =>
    valueMode === "count"
      ? `${Math.round(value)}`
      : `${Number(value).toFixed(percentDecimals)}${showPercentSymbol ? "%" : ""}`;

  const formatChartLabelValue = (value: number) => {
    if (valueMode === "count") return `${Math.round(value)}`;
    return `${Number(value).toFixed(percentDecimals)}${showPercentSymbol ? "%" : ""}`;
  };
  const renderPieValueLabel = ({
    cx,
    cy,
    midAngle,
    outerRadius,
    value,
  }: {
    cx?: number;
    cy?: number;
    midAngle?: number;
    outerRadius?: number;
    value?: number;
  }) => {
    if (cx === undefined || cy === undefined || midAngle === undefined || outerRadius === undefined || value === undefined) {
      return null;
    }
    const RADIAN = Math.PI / 180;
    const radius = outerRadius + 14;
    const x = cx + radius * Math.cos(-midAngle * RADIAN);
    const y = cy + radius * Math.sin(-midAngle * RADIAN);
    return (
      <text
        x={x}
        y={y}
        fill="#1f2937"
        fontSize={12}
        fontWeight={800}
        stroke="rgba(255,255,255,0.95)"
        strokeWidth={3}
        paintOrder="stroke"
        textAnchor={x > cx ? "start" : "end"}
        dominantBaseline="central"
      >
        {formatChartLabelValue(Number(value || 0))}
      </text>
    );
  };

  useEffect(() => {
    if (classicOnly && chartType !== "classic") {
      setChartType("classic");
    }
  }, [classicOnly, chartType]);

  useEffect(() => {
    if (!lineAsPie) return;
    if (chartType === "line") setChartType("pie");
  }, [lineAsPie, chartType]);

  useEffect(() => {
    if (lineAsPie) return;
    if (chartType === "pie") setChartType("classic");
  }, [lineAsPie, chartType]);

  useEffect(() => {
    const options = isOptionByBrandMode
      ? mediaOptions.map((option) => ({ key: option, label: option }))
      : metricDefs.map((metric) => ({ key: metric.key, label: metric.label }));
    if (!groupedMetricKey && options[0]?.key) {
      setGroupedMetricKey(options[0].key);
    } else if (groupedMetricKey && !options.some((option) => option.key === groupedMetricKey)) {
      setGroupedMetricKey(options[0]?.key || "");
    }
  }, [metricDefs, mediaOptions, isOptionByBrandMode, groupedMetricKey]);

  useEffect(() => {
    const options = isOptionByBrandMode
      ? mediaOptions.map((option) => ({ key: option, label: option }))
      : metricDefs.map((metric) => ({ key: metric.key, label: metric.label }));
    if (!lineMetricKey && options[0]?.key) {
      setLineMetricKey(options[0].key);
    } else if (lineMetricKey && !options.some((option) => option.key === lineMetricKey)) {
      setLineMetricKey(options[0]?.key || "");
    }
  }, [metricDefs, mediaOptions, isOptionByBrandMode, lineMetricKey]);

  const metricColorByKey = useMemo(
    () =>
      metricDefs.reduce(
        (acc, metric, index) => {
          acc[metric.key] = CHART_COLORS[index % CHART_COLORS.length];
          return acc;
        },
        {} as Record<string, string>,
      ),
    [metricDefs],
  );

  const groupedBrands = useMemo(() => takeItemsWithPinnedDashboardLabelsLast(sortedBrands, 8, (brand) => brand), [sortedBrands]);

  const groupedBarsData = useMemo(
    () =>
      months.map((month) => {
        const row: Record<string, number | string> = { month: formatMonthLabel(month) };
        groupedBrands.forEach((brand) => {
          row[brand] = isOptionByBrandMode
            ? groupedMetricKey
              ? getMediaCellNumericValue(groupedMetricKey, month, brand)
              : 0
            : groupedMetricKey
              ? getCellNumericValue(groupedMetricKey, month, brand)
              : 0;
        });
        return row;
      }),
    [
      months,
      groupedBrands,
      groupedMetricKey,
      isOptionByBrandMode,
      valueMode,
      monthMap,
      mediaCellMap,
      percentDecimals,
      showPercentSymbol,
    ],
  );

  const chartSelectorOptions = useMemo(
    () =>
      isOptionByBrandMode
        ? mediaOptions.map((option) => ({ key: option, label: option }))
        : metricDefs.map((metric) => ({ key: metric.key, label: metric.label })),
    [isOptionByBrandMode, mediaOptions, metricDefs],
  );
  const lineBrands = useMemo(() => takeItemsWithPinnedDashboardLabelsLast(sortedBrands, 8, (brand) => brand), [sortedBrands]);
  const topTwelveBrands = useMemo(() => takeItemsWithPinnedDashboardLabelsLast(sortedBrands, 12, (brand) => brand), [sortedBrands]);
  const lineSeriesMonths = useMemo(() => [...months], [months]);

  const lineData = useMemo(
    () =>
      lineBrands.map((brand) => {
        const row: Record<string, number | string> = { category: brand };
        lineSeriesMonths.forEach((month) => {
          row[month] = isOptionByBrandMode
            ? lineMetricKey
              ? getMediaCellNumericValue(lineMetricKey, month, brand)
              : 0
            : lineMetricKey
              ? getCellNumericValue(lineMetricKey, month, brand)
              : 0;
        });
        return row;
      }),
    [
      lineBrands,
      lineSeriesMonths,
      lineMetricKey,
      isOptionByBrandMode,
      valueMode,
      monthMap,
      mediaCellMap,
      percentDecimals,
      showPercentSymbol,
    ],
  );
  const pieDataByMonth = useMemo(() => {
    return months.map((month) => ({
      month,
      label: formatMonthLabel(month),
      data: topTwelveBrands
        .map((brand) => {
          const value = Number(
            isOptionByBrandMode
              ? lineMetricKey
                ? getMediaCellNumericValue(lineMetricKey, month, brand)
                : 0
              : lineMetricKey
                ? getCellNumericValue(lineMetricKey, month, brand)
                : 0,
          );
          return { name: brand, value };
        })
        .filter((item) => item.value > 0),
    }));
  }, [topTwelveBrands, months, isOptionByBrandMode, lineMetricKey, valueMode, monthMap, mediaCellMap]);
  const pieData = useMemo(() => {
    if (pieDataByMonth.length === 0) return [];
    return pieDataByMonth[0]?.data || [];
  }, [pieDataByMonth]);
  const pieColumns = Math.min(3, Math.max(1, pieDataByMonth.length));
  const pieColorByBrand = useMemo(() => {
    const map: Record<string, string> = {};
    topTwelveBrands.forEach((brand, idx) => {
      map[brand] = CHART_COLORS[idx % CHART_COLORS.length];
    });
    return map;
  }, [topTwelveBrands]);

  const classicCardWidth = isOptionByBrandMode ? Math.max(320, chartSelectorOptions.length * 58) : 270;
  const classicBarWidthClass = isOptionByBrandMode && chartSelectorOptions.length > 8 ? "w-6" : "w-7";

  const handleDownload = () => {
    const rows: (string | number)[][] = [];
    const totalCols = isOptionByBrandMode ? 1 + sortedBrands.length * months.length : 1 + metricDefs.length * months.length;

    const merges: Array<{ s: { r: number; c: number }; e: { r: number; c: number } }> = [];
    const headerRow1: (string | number)[] = [
      resolvedRowHeaderLabel,
    ];
    const headerRow2: (string | number)[] = [""];

    let colIndex = 1;
    if (isOptionByBrandMode) {
      sortedBrands.forEach((brand) => {
        headerRow1.push(brand);
        for (let i = 1; i < months.length; i += 1) headerRow1.push("");
        merges.push({
          s: { r: 0, c: colIndex },
          e: { r: 0, c: colIndex + months.length - 1 },
        });
        months.forEach((month) => headerRow2.push(formatMonthLabel(month)));
        colIndex += months.length;
      });
    } else {
      metricDefs.forEach((metric) => {
        headerRow1.push(metric.label);
        for (let i = 1; i < months.length; i += 1) headerRow1.push("");
        if (months.length > 1) {
          merges.push({
            s: { r: 0, c: colIndex },
            e: { r: 0, c: colIndex + months.length - 1 },
          });
        }
        months.forEach((month) => headerRow2.push(formatMonthLabel(month)));
        colIndex += months.length;
      });
    }

    rows.push(headerRow1);
    rows.push(headerRow2);

    const baseRow: (string | number)[] = ["Base n"];
    if (isOptionByBrandMode) {
      sortedBrands.forEach((brand) => {
        months.forEach((month) => {
          const optionForBase = mediaOptions[0] || "";
          baseRow.push(Number(mediaCellMap.get(`${optionForBase}__${brand}__${month}`)?.base || 0));
        });
      });
    } else {
      metricDefs.forEach((metric) => {
        months.forEach((month) => baseRow.push(Number(monthMap.get(month)?.bases?.[metric.key] || 0)));
      });
    }
    rows.push(baseRow);

    if (isOptionByBrandMode) {
      mediaOptions.forEach((option) => {
        const row: (string | number)[] = [option];
        sortedBrands.forEach((brand) => {
          months.forEach((month) => {
            const value = getMediaCellNumericValue(option, month, brand);
            row.push(valueMode === "count" ? Math.round(value) : Number(value.toFixed(percentDecimals)));
          });
        });
        rows.push(row);
      });
    } else {
      sortedBrands.forEach((brand) => {
        const row: (string | number)[] = [brand];
        metricDefs.forEach((metric) => {
          months.forEach((month) => {
            const value = getCellNumericValue(metric.key, month, brand);
            row.push(valueMode === "count" ? Math.round(value) : Number(value.toFixed(percentDecimals)));
          });
        });
        rows.push(row);
      });
    }

    const worksheet = XLSX.utils.aoa_to_sheet(rows);
    (worksheet as unknown as { ["!merges"]?: unknown })["!merges"] = merges;
    (worksheet as unknown as { ["!cols"]?: Array<{ wch: number }> })["!cols"] = Array.from(
      { length: totalCols },
      (_, idx) => ({ wch: idx === 0 ? 30 : 13 }),
    );

    const borderStyle = {
      top: { style: "thin", color: { rgb: "000000" } },
      bottom: { style: "thin", color: { rgb: "000000" } },
      left: { style: "thin", color: { rgb: "000000" } },
      right: { style: "thin", color: { rgb: "000000" } },
    };

    const applyStyle = (r: number, c: number, style: Record<string, unknown>) => {
      const cellRef = XLSX.utils.encode_cell({ r, c });
      const cell = (worksheet as Record<string, unknown>)[cellRef] as { s?: Record<string, unknown> } | undefined;
      if (!cell) return;
      cell.s = { ...(cell.s ?? {}), ...style };
    };

    const numFmtPercent = `${percentDecimals === 0 ? "0" : `0.${"0".repeat(percentDecimals)}`}${showPercentSymbol ? '\\%' : ""}`;
    const numFmtCount = "0";
    const numFmt = valueMode === "count" ? numFmtCount : numFmtPercent;

    for (let r = 0; r < rows.length; r += 1) {
      for (let c = 0; c < totalCols; c += 1) {
        applyStyle(r, c, { border: borderStyle });
      }
    }

    for (let c = 0; c < totalCols; c += 1) {
      applyStyle(0, c, {
        font: { bold: true },
        fill: { fgColor: { rgb: "E6E6E6" } },
        alignment: { horizontal: "center", vertical: "center" },
      });
      applyStyle(1, c, {
        font: { bold: true },
        fill: { fgColor: { rgb: "F2F2F2" } },
        alignment: { horizontal: "center", vertical: "center" },
      });
      applyStyle(2, c, {
        font: { bold: true },
        alignment: { horizontal: "center", vertical: "center" },
      });
    }

    // Base row should always be plain numeric counts (no % sign).
    for (let c = 1; c < totalCols; c += 1) {
      applyStyle(2, c, { numFmt: "0" });
    }
    // Data rows follow current mode/decimal export format.
    for (let r = 3; r < rows.length; r += 1) {
      for (let c = 1; c < totalCols; c += 1) {
        applyStyle(r, c, { numFmt });
      }
    }

    const workbook = XLSX.utils.book_new();
    const cleanSheetTitle = String(tableTitle || "Table")
      .replace(/[\\/*?:[\]]/g, "")
      .trim()
      .slice(0, 31) || "Table";
    XLSX.utils.book_append_sheet(workbook, worksheet, cleanSheetTitle);
    const cleanFileTitle =
      `${String(categoryTitle || "category")
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "_")
        .replace(/^_+|_+$/g, "")}_${String(tableTitle || "table")
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "_")
        .replace(/^_+|_+$/g, "")}`
        .replace(/_+/g, "_")
        .replace(/^_+|_+$/g, "") || "table";
    XLSX.writeFile(workbook, `${cleanFileTitle}_table.xlsx`);
  };

  const hasTrendPayload = metricDefs.length > 0 && months.length > 0;
  const hasRenderableTrendData = isOptionByBrandMode
    ? sortedBrands.length > 0 && mediaOptions.length > 0
    : sortedBrands.length > 0;
  const resolvedEmptyStateMessage = emptyStateMessage || "No trend data available for the selected month(s).";
  const solidAccentButtonClass = useGoldAccent
    ? "bg-[#C8A44D] text-slate-950 hover:bg-[#b89333]"
    : "bg-red-500 text-white hover:bg-red-600";
  const borderSolidAccentButtonClass = useGoldAccent
    ? "border-[#C8A44D] bg-[#C8A44D] text-slate-950 hover:bg-[#b89333]"
    : "border-red-500 bg-red-500 text-white hover:bg-red-600";
  const toggleThumbClass = useGoldAccent ? "bg-[#C8A44D]" : "bg-red-500";

  return (
    <article className="relative morphism-card rounded-2xl p-4">
      {loading ? <div className="absolute inset-0 z-20 rounded-2xl bg-white/12 backdrop-blur-[2px]" /> : null}
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <ActiveSelectionChipBar items={activeSelections} />
        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            onClick={() => setViewMode((prev) => (prev === "table" ? "chart" : "table"))}
            className="relative flex h-9 w-[72px] items-center justify-between rounded-full border border-slate-400/40 bg-white/25 p-1 transition-all duration-200 hover:-translate-y-0.5 hover:border-slate-500/60 hover:bg-white/35 hover:shadow-[0_6px_14px_rgba(15,23,42,0.15)]"
            title="Toggle charts or table view"
            aria-label="Toggle charts or table view"
          >
            <div
              className={`absolute h-7 w-7 rounded-full ${toggleThumbClass} transition-all duration-300 ease-out ${
                viewMode === "table" ? "left-[calc(100%-32px)]" : "left-1"
              }`}
            />
            <div
              className={`relative z-10 flex h-8 w-8 items-center justify-center rounded-full transition-colors duration-300 ${
                viewMode === "chart" ? "text-white" : "text-slate-700"
              }`}
              title="Charts"
            >
              <BarChart3 className="h-4 w-4" />
            </div>
            <div
              className={`relative z-10 flex h-8 w-8 items-center justify-center rounded-full transition-colors duration-300 ${
                viewMode === "table" ? "text-white" : "text-slate-700"
              }`}
              title="Tables"
            >
              <Table2 className="h-4 w-4" />
            </div>
          </button>
          <button
            type="button"
            onClick={() => setValueMode((prev) => (prev === "count" ? "percent" : "count"))}
            className={`flex h-9 w-9 items-center justify-center rounded-full border p-0 transition-all duration-200 hover:-translate-y-0.5 hover:shadow-[0_6px_14px_rgba(15,23,42,0.15)] ${
              valueMode === "count"
                ? borderSolidAccentButtonClass
                : "border-slate-400/40 bg-white/25 text-slate-700 hover:border-slate-500/60 hover:bg-white/35"
            }`}
            title={valueMode === "count" ? "Show percent" : "Show N count"}
            aria-label={valueMode === "count" ? "Show percent" : "Show N count"}
            disabled={!hasTrendPayload}
          >
            {valueMode === "count" ? <span className="text-xs font-bold">N</span> : <Percent className="h-4 w-4" />}
          </button>
          <button
            type="button"
            onClick={() => setPercentFormatStep((prev) => (prev + 1) % 6)}
            className="flex h-9 w-9 items-center justify-center rounded-full border border-slate-400/40 bg-white/25 p-0 text-slate-700 transition-all duration-200 hover:-translate-y-0.5 hover:border-slate-500/60 hover:bg-white/35 hover:shadow-[0_6px_14px_rgba(15,23,42,0.15)] disabled:opacity-40 disabled:hover:translate-y-0 disabled:hover:shadow-none"
            title={`Format: ${percentDecimals}dp ${showPercentSymbol ? "with %" : "without %"}`}
            aria-label="Adjust decimal places"
            disabled={valueMode === "count" || !hasTrendPayload}
          >
            <SlidersHorizontal className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={handleDownload}
            className="flex h-9 w-9 items-center justify-center rounded-full border border-slate-400/40 bg-white/25 p-0 text-slate-700 transition-all duration-200 hover:-translate-y-0.5 hover:border-slate-500/60 hover:bg-white/35 hover:shadow-[0_6px_14px_rgba(15,23,42,0.15)]"
            title="Export table"
            aria-label="Export table"
            disabled={!hasTrendPayload}
          >
            <Download className="h-4 w-4" />
          </button>
          {viewMode === "chart" ? (
            <div className="ml-2 flex items-center gap-1 rounded-full border border-slate-400/40 bg-white/25 p-1">
              {(classicOnly
                ? (["classic"] as const)
                : lineAsPie
                  ? (["classic", "grouped", "pie"] as const)
                  : (["classic", "grouped", "line"] as const)
              ).map((type) => (
                <button
                  key={type}
                  type="button"
                onClick={() => setChartType(type)}
                className={`rounded-full px-3 py-1 text-xs font-semibold transition-colors ${
                  chartType === type
                    ? solidAccentButtonClass
                    : "text-slate-700 hover:bg-white/40 hover:text-slate-900"
                }`}
              >
                  {type === "classic" ? "Classic" : type === "grouped" ? "Grouped Bar" : type === "pie" ? "Pie" : "Line"}
                </button>
              ))}
            </div>
          ) : null}
        </div>
      </div>

      {(!hasTrendPayload || !hasRenderableTrendData) && !loading ? (
        <div className="rounded-xl border-2 border-black/70 bg-white/20 p-4 text-sm text-slate-700">
          {resolvedEmptyStateMessage}
        </div>
      ) : viewMode === "table" ? (
        <div className="overflow-x-auto rounded-xl border-2 border-black/70 bg-white/20">
          {isOptionByBrandMode ? (
            <table className="min-w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-black/70 bg-white/35">
                    <th className={`${STICKY_TABLE_LEAD_HEADER_CLASS} px-4 py-2 text-left font-semibold text-slate-900`}>
                    {resolvedRowHeaderLabel}
                  </th>
                  {sortedBrands.map((brand) => (
                    <th
                      key={`brand-head-${brand}`}
                      colSpan={months.length}
                      className="border-r-2 border-black/70 px-3 py-2 text-center text-xs font-semibold uppercase tracking-wide text-slate-800"
                    >
                      {brand}
                    </th>
                  ))}
                </tr>
                <tr className="border-b border-black/70 bg-white/25">
                  <th className={`${STICKY_TABLE_LEAD_SUBHEADER_CLASS} px-4 py-2 text-left text-xs font-semibold text-slate-700`}>
                    &nbsp;
                  </th>
                  {sortedBrands.flatMap((brand) =>
                    months.map((month, monthIndex) => (
                      <th
                        key={`month-${brand}-${month}`}
                        className={`px-3 py-2 text-center text-xs font-semibold text-sky-700 ${
                          monthIndex === months.length - 1 ? "border-r-2 border-black/70" : "border-r border-black/60"
                        }`}
                      >
                        {formatMonthLabel(month)}
                      </th>
                    )),
                  )}
                </tr>
              </thead>
              <tbody>
                <tr className="border-b border-black/60 bg-white/20">
                  <td className={`${STICKY_TABLE_LEAD_CELL_CLASS} px-4 py-2 font-semibold text-slate-900`}>
                    Base n
                  </td>
                  {sortedBrands.flatMap((brand) =>
                    months.map((month, monthIndex) => {
                      const optionForBase = mediaOptions[0] || "";
                      const baseValue = optionForBase ? Number(mediaCellMap.get(`${optionForBase}__${brand}__${month}`)?.base || 0) : 0;
                      return (
                        <td
                          key={`media-base-${brand}-${month}`}
                          className={`px-3 py-2 text-center font-bold text-slate-900 ${
                            monthIndex === months.length - 1 ? "border-r-2 border-black/70" : "border-r border-black/60"
                          }`}
                        >
                          {baseValue.toLocaleString()}
                        </td>
                      );
                    }),
                  )}
                </tr>
                {mediaOptions.map((option) => (
                  <tr key={`media-option-${option}`} className="border-b border-black/60 last:border-b-0">
                    <td className={`${STICKY_TABLE_LEAD_CELL_CLASS} px-4 py-2 font-semibold text-slate-900`}>
                      {option}
                    </td>
                    {sortedBrands.flatMap((brand) =>
                      months.map((month, monthIndex) => {
                        const cell = mediaCellMap.get(`${option}__${brand}__${month}`);
                        const value = valueMode === "count" ? Number(cell?.count || 0) : Number(cell?.pct || 0);
                        return (
                          <td
                            key={`media-cell-${option}-${brand}-${month}`}
                            className={`px-3 py-2 text-center text-slate-800 ${
                              monthIndex === months.length - 1 ? "border-r-2 border-black/70" : "border-r border-black/60"
                            }`}
                          >
                            {formatCellValue(value)}
                          </td>
                        );
                      }),
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <table className="min-w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-black/70 bg-white/35">
                  <th className={`${STICKY_TABLE_LEAD_HEADER_CLASS} px-4 py-2 text-left font-semibold text-slate-900`}>
                    {resolvedRowHeaderLabel}
                  </th>
                  {metricDefs.map((metric) => (
                    <th
                      key={metric.key}
                      colSpan={months.length}
                      className="border-r-2 border-black/70 px-3 py-2 text-center text-xs font-semibold uppercase tracking-wide text-slate-800"
                    >
                      {metric.label}
                    </th>
                  ))}
                </tr>
                <tr className="border-b border-black/70 bg-white/25">
                  <th className={`${STICKY_TABLE_LEAD_SUBHEADER_CLASS} px-4 py-2 text-left text-xs font-semibold text-slate-700`}>
                    &nbsp;
                  </th>
                  {metricDefs.flatMap((metric) =>
                    months.map((month, monthIndex) => {
                      const isMetricBoundary = monthIndex === months.length - 1;
                      return (
                        <th
                          key={`${metric.key}-${month}`}
                          className={`px-3 py-2 text-center text-xs font-semibold text-sky-700 ${
                            isMetricBoundary ? "border-r-2 border-black/70" : "border-r border-black/60"
                          }`}
                        >
                          {formatMonthLabel(month)}
                        </th>
                      );
                    }),
                  )}
                </tr>
              </thead>
              <tbody>
                <tr className="border-b border-black/60 bg-white/20">
                  <td className={`${STICKY_TABLE_LEAD_CELL_CLASS} px-4 py-2 font-semibold text-slate-900`}>
                    Base n
                  </td>
                  {metricDefs.flatMap((metric) =>
                    months.map((month, monthIndex) => {
                      const baseValue = Number(monthMap.get(month)?.bases?.[metric.key] || 0);
                      const isMetricBoundary = monthIndex === months.length - 1;
                      return (
                        <td
                          key={`base-${metric.key}-${month}`}
                          className={`px-3 py-2 text-center font-bold text-slate-900 ${
                            isMetricBoundary ? "border-r-2 border-black/70" : "border-r border-black/60"
                          }`}
                        >
                          {baseValue.toLocaleString()}
                        </td>
                      );
                    }),
                  )}
                </tr>
                {sortedBrands.map((brand) => (
                  <tr key={brand} className="border-b border-black/60 last:border-b-0">
                    <td className={`${STICKY_TABLE_LEAD_CELL_CLASS} px-4 py-2 font-semibold text-slate-900`}>
                      {brand}
                    </td>
                    {metricDefs.flatMap((metric) =>
                      months.map((month, monthIndex) => {
                        const value = getCellNumericValue(metric.key, month, brand);
                        const isMetricBoundary = monthIndex === months.length - 1;
                        return (
                          <td
                            key={`${brand}-${metric.key}-${month}`}
                            className={`px-3 py-2 text-center text-slate-800 ${
                              isMetricBoundary ? "border-r-2 border-black/70" : "border-r border-black/60"
                            }`}
                          >
                            {formatCellValue(value)}
                          </td>
                        );
                      }),
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : (
        <div className="space-y-3">
          <div className="rounded-xl border-2 border-black/70 bg-white/20 p-3">
            <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-700">Legend</p>
            <div className="flex flex-wrap items-center gap-3">
              {(chartType === "line"
                ? lineSeriesMonths
                : chartType === "grouped"
                  ? groupedBrands
                  : chartType === "pie"
                    ? (pieDataByMonth[0]?.data || []).map((item) => item.name)
                    : isOptionByBrandMode
                      ? mediaOptions
                      : metricDefs
              ).map((entry, index) => {
                const key = typeof entry === "string" ? entry : entry.key;
                const label = typeof entry === "string" ? entry : entry.label;
                const color =
                  typeof entry === "string"
                    ? chartType === "grouped"
                      ? CHART_COLORS[index % CHART_COLORS.length]
                      : chartType === "line"
                        ? CHART_COLORS[index % CHART_COLORS.length]
                      : CHART_COLORS[index % CHART_COLORS.length]
                    : metricColorByKey[entry.key] || "#0ea5e9";
                const displayLabel = chartType === "line" && typeof entry === "string" ? formatMonthLabel(entry) : label;
                return (
                  <div key={`legend-${key}`} className="flex items-center gap-1.5 text-xs text-slate-800">
                    <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: color }} />
                    <span>{displayLabel}</span>
                  </div>
                );
              })}
            </div>
          </div>

          {chartType === "classic" ? (
            <div className="space-y-3">
              {months.map((month) => {
                const brandsForMonth = topTwelveBrands;
                const monthMax =
                  valueMode === "count"
                    ? Math.max(
                        1,
                        ...brandsForMonth.flatMap((brand) =>
                          metricDefs.map((metric) => Number(getCellNumericValue(metric.key, month, brand) || 0)),
                        ),
                      )
                    : 100;
                return (
                  <div key={`classic-${month}`} className="rounded-xl border-2 border-black/70 bg-white/20 p-3">
                    <div className="flex items-start gap-3">
                      <div className="min-w-[86px] rounded-xl bg-sky-900 px-3 py-4 text-center text-sm font-bold text-white">
                        {formatMonthLabel(month)}
                      </div>
                      <div className="flex-1 overflow-x-auto">
                        <div className="flex min-w-max gap-4">
                          {brandsForMonth.map((brand) => (
                            <div
                              key={`classic-${month}-${brand}`}
                              className="rounded-lg border border-white/35 bg-white/35 p-3"
                              style={{ width: `${classicCardWidth}px` }}
                            >
                              <div
                                className="mb-1 grid h-[210px] items-end gap-2.5"
                                style={{
                                  gridTemplateColumns: `repeat(${Math.max(isOptionByBrandMode ? mediaOptions.length : metricDefs.length, 1)}, minmax(0, 1fr))`,
                                }}
                              >
                                {(isOptionByBrandMode ? mediaOptions : metricDefs).map((entry, chartIdx) => {
                                  const key = typeof entry === "string" ? entry : entry.key;
                                  const value = Number(
                                    isOptionByBrandMode
                                      ? getMediaCellNumericValue(key, month, brand)
                                      : getCellNumericValue(key, month, brand),
                                  );
                                  const heightPx = Math.max(20, Math.min(176, (value / monthMax) * 176));
                                  return (
                                    <div key={`${month}-${brand}-${key}`} className="flex flex-col items-center gap-1">
                                      <span className="min-h-[30px] w-full whitespace-nowrap text-center text-[11px] leading-4 font-semibold text-slate-700">
                                        {formatChartLabelValue(value)}
                                      </span>
                                      <div
                                        className={`mx-auto ${classicBarWidthClass} rounded-t`}
                                        style={{
                                          height: `${heightPx}px`,
                                          backgroundColor: isOptionByBrandMode
                                            ? CHART_COLORS[chartIdx % CHART_COLORS.length]
                                            : metricColorByKey[key] || "#0ea5e9",
                                        }}
                                      />
                                    </div>
                                  );
                                })}
                              </div>
                              <p className="truncate text-center text-[11px] font-semibold text-slate-900">{brand}</p>
                              <p className="text-center text-[10px] text-slate-600">
                                (n=
                                {isOptionByBrandMode
                                  ? Number(mediaCellMap.get(`${mediaOptions[0] || ""}__${brand}__${month}`)?.base || 0).toLocaleString()
                                  : Number(monthMap.get(month)?.bases?.[metricDefs[0]?.key || ""] || 0).toLocaleString()}
                                )
                              </p>
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : null}

          {!classicOnly && chartType === "grouped" ? (
            <div className="rounded-xl border-2 border-black/70 bg-white/25 p-3">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <p className="min-w-0 flex-1 truncate text-xs font-semibold uppercase tracking-wide text-slate-700">
                  Grouped bars by month - {chartSelectorOptions.find((option) => option.key === groupedMetricKey)?.label || "Metric"}
                </p>
                {!hideChartSelectors ? (
                  <select
                    value={groupedMetricKey}
                    onChange={(event) => setGroupedMetricKey(event.target.value)}
                    className="w-60 shrink-0 rounded border border-slate-400/50 bg-white/60 px-2 py-1 text-xs font-semibold text-slate-800"
                  >
                    {chartSelectorOptions.map((option) => (
                      <option key={`grouped-metric-${option.key}`} value={option.key}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                ) : null}
              </div>
              <div className="h-[500px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={groupedBarsData} margin={{ top: 34, right: 12, left: 8, bottom: 28 }} barGap={5} barCategoryGap="20%">
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(15, 23, 42, 0.22)" />
                    <XAxis dataKey="month" tick={{ fontSize: 12, fontWeight: 700, fill: "#0f172a" }} />
                    <YAxis tick={{ fontSize: 11 }} />
                    <Legend wrapperStyle={{ fontSize: "11px" }} />
                    <Tooltip
                      formatter={(value: number, name: string) => [
                        `${valueMode === "count" ? Math.round(value) : Number(value).toFixed(percentDecimals)}${valueMode === "count" ? "" : showPercentSymbol ? "%" : ""}`,
                        `${resolvedRowHeaderLabel}: ${name}`,
                      ]}
                      labelFormatter={(label) => `Month: ${label}`}
                    />
                    {groupedBrands.map((brand, index) => (
                      <Bar
                        key={`grouped-brand-${brand}`}
                        dataKey={brand}
                        name={brand}
                        fill={CHART_COLORS[index % CHART_COLORS.length]}
                        maxBarSize={56}
                        radius={[4, 4, 0, 0]}
                      >
                        <LabelList
                          dataKey={brand}
                          position="top"
                          content={(props) => {
                            const x = Number(props.x || 0);
                            const y = Number(props.y || 0);
                            const width = Number(props.width || 0);
                            const value = Number(props.value || 0);
                            if (width < 24) return null;
                            const labelText = formatChartLabelValue(value);
                            if (labelText.length > 5 && width < 30) return null;
                            const staggerOffset = index % 2 === 0 ? 4 : 14;
                            const labelY = y < 18 ? y + 12 : y - staggerOffset;
                            return (
                              <text
                                x={x + width / 2}
                                y={labelY}
                                fill="#334155"
                                fontSize={11}
                                fontWeight={600}
                                textAnchor="middle"
                              >
                                {labelText}
                              </text>
                            );
                          }}
                        />
                      </Bar>
                    ))}
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          ) : null}

          {!classicOnly && chartType === "line" ? (
            <div className="rounded-xl border-2 border-black/70 bg-white/25 p-3">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <p className="min-w-0 flex-1 truncate text-xs font-semibold uppercase tracking-wide text-slate-700">
                  {resolvedRowHeaderLabel} trends by month
                </p>
                {!hideChartSelectors ? (
                  <select
                    value={lineMetricKey}
                    onChange={(event) => setLineMetricKey(event.target.value)}
                    className="w-60 shrink-0 rounded border border-slate-400/50 bg-white/60 px-2 py-1 text-xs font-semibold text-slate-800"
                  >
                    {chartSelectorOptions.map((option) => (
                      <option key={`line-metric-${option.key}`} value={option.key}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                ) : null}
              </div>
              <div className="h-[390px]">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={lineData} margin={{ top: 10, right: 12, left: 8, bottom: 12 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(15, 23, 42, 0.22)" />
                    <XAxis dataKey="category" tick={{ fontSize: 11 }} />
                    <YAxis tick={{ fontSize: 11 }} />
                    <Legend wrapperStyle={{ fontSize: "11px" }} />
                    <Tooltip
                      labelFormatter={(label) => `${resolvedRowHeaderLabel}: ${label}`}
                      formatter={(value: number, name: string) => [
                        `${valueMode === "count" ? Math.round(value) : Number(value).toFixed(percentDecimals)}${valueMode === "count" ? "" : showPercentSymbol ? "%" : ""}`,
                        `${name}`,
                      ]}
                    />
                    {lineSeriesMonths.map((month, index) => (
                      <Line
                        key={`line-${month}`}
                        type="monotone"
                        dataKey={month}
                        name={formatMonthLabel(month)}
                        stroke={CHART_COLORS[index % CHART_COLORS.length]}
                        strokeWidth={2.5}
                        dot={{ r: 3.5 }}
                        activeDot={{ r: 5 }}
                      />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
          ) : null}

          {!classicOnly && chartType === "pie" ? (
            <div className="rounded-xl border-2 border-black/70 bg-white/25 p-3">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <p className="min-w-0 flex-1 truncate text-xs font-semibold uppercase tracking-wide text-slate-700">
                  {resolvedRowHeaderLabel} share
                </p>
                {!hideChartSelectors ? (
                  <select
                    value={lineMetricKey}
                    onChange={(event) => setLineMetricKey(event.target.value)}
                    className="w-60 shrink-0 rounded border border-slate-400/50 bg-white/60 px-2 py-1 text-xs font-semibold text-slate-800"
                  >
                    {chartSelectorOptions.map((option) => (
                      <option key={`pie-metric-${option.key}`} value={option.key}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                ) : null}
              </div>
              {pieDataByMonth.length > 1 ? (
                <div
                  className="grid gap-3"
                  style={{ gridTemplateColumns: `repeat(${pieColumns}, minmax(0, 1fr))` }}
                >
                  {pieDataByMonth.map((monthBucket) => (
                    <div key={`pie-month-${monthBucket.month}`} className="rounded-lg border border-white/35 bg-white/20 p-2.5">
                      <p className="mb-1.5 text-center text-xs font-semibold text-slate-800">{monthBucket.label}</p>
                      <div className="h-[280px]">
                        <ResponsiveContainer width="100%" height="100%">
                          <PieChart>
                            <Tooltip
                              formatter={(value: number, name: string) => [
                                `${valueMode === "count" ? Math.round(value) : Number(value).toFixed(percentDecimals)}${valueMode === "count" ? "" : showPercentSymbol ? "%" : ""}`,
                                name,
                              ]}
                            />
                            <Pie
                              data={monthBucket.data}
                              dataKey="value"
                              nameKey="name"
                              innerRadius={44}
                              outerRadius={96}
                              paddingAngle={2}
                              labelLine={false}
                              label={renderPieValueLabel}
                            >
                              {monthBucket.data.map((entry) => (
                                <Cell key={`pie-cell-${monthBucket.month}-${entry.name}`} fill={pieColorByBrand[entry.name] || "#0ea5e9"} />
                              ))}
                            </Pie>
                          </PieChart>
                        </ResponsiveContainer>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="h-[390px]">
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Tooltip
                        formatter={(value: number, name: string) => [
                          `${valueMode === "count" ? Math.round(value) : Number(value).toFixed(percentDecimals)}${valueMode === "count" ? "" : showPercentSymbol ? "%" : ""}`,
                          name,
                        ]}
                      />
                      <Pie
                        data={pieData}
                        dataKey="value"
                        nameKey="name"
                        innerRadius={54}
                        outerRadius={120}
                        paddingAngle={2}
                        labelLine={false}
                        label={renderPieValueLabel}
                      >
                        {pieData.map((entry) => (
                          <Cell key={`pie-cell-${entry.name}`} fill={pieColorByBrand[entry.name] || "#0ea5e9"} />
                        ))}
                      </Pie>
                    </PieChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>
          ) : null}
        </div>
      )}
      {loading ? (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center rounded-2xl bg-white/35 backdrop-blur-[1px]">
          <div className="rounded-lg border border-white/60 bg-white/70 px-3 py-1.5 text-xs font-semibold text-slate-700">
            Updating chart/table...
          </div>
        </div>
      ) : null}
    </article>
  );
}

function FloatingLogosBackground({ slug }: { slug: string }) {
  // const partnerLogo = getCategoryBrandLogo(slug);
  return (
    <div className="pointer-events-none fixed inset-0 z-0 overflow-hidden">
      <div className="absolute inset-0 bg-[#d2d7df]" />
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(60% 45% at 15% 20%, rgba(255, 168, 76, 0.18), transparent 60%), radial-gradient(55% 40% at 85% 25%, rgba(64, 195, 255, 0.18), transparent 60%), radial-gradient(50% 40% at 70% 85%, rgba(76, 219, 164, 0.16), transparent 60%)",
        }}
      />
      <div className="absolute top-1/2 left-1/2 flex -translate-x-1/2 -translate-y-1/2 items-center gap-16">
        <img src="/infinity-logo.png" alt="" className="h-auto w-[34vw] max-w-2xl animate-float opacity-[0.14]" />
        {/* <img src={partnerLogo.src} alt="" className="h-auto w-[30vw] max-w-xl animate-float-delayed opacity-[0.13]" /> */}
      </div>
      <div className="absolute inset-0 bg-gradient-to-br from-primary/6 via-transparent to-accent/6" />
    </div>
  );
}

function QuestionCard({ item }: { item: PageQuestion }) {
  return (
    <article className="morphism-card rounded-2xl p-5">
      <h3 className="text-base font-semibold text-slate-900">{item.question}</h3>
      <p className="mt-1 text-sm text-slate-700">{item.questionLabel}</p>
      <div className="mt-4 space-y-2">
        {item.answers.map((answer) => (
          <div key={`${item.question}-${answer.answer}`} className="rounded-2xl border border-white/30 bg-white/20 px-4 py-3">
            <div className="mb-1 flex items-center justify-between text-sm">
              <span className="truncate pr-3 text-slate-800">{answer.answer}</span>
              <span className="font-semibold text-sky-700">{answer.pct}%</span>
            </div>
            <div className="mt-2 h-2.5 overflow-hidden rounded-full bg-muted/50">
              <div className="h-full bg-sky-500" style={{ width: `${Math.min(100, answer.pct)}%` }} />
            </div>
          </div>
        ))}
      </div>
    </article>
  );
}

function DemographicListCard({ title, values, categoryTitle }: { title: string; values: DemoBucket[]; categoryTitle?: string }) {
  const iconByField: Record<string, JSX.Element> = {
    Region: <MapPin className="h-4 w-4 text-sky-600" />,
    D3: <Wallet className="h-4 w-4 text-sky-600" />,
    Income: <Wallet className="h-4 w-4 text-sky-600" />,
    Gender: <Users className="h-4 w-4 text-sky-600" />,
    Age: <Users className="h-4 w-4 text-sky-600" />,
    SEC: <Shield className="h-4 w-4 text-sky-600" />,
    Week: <Calendar className="h-4 w-4 text-sky-600" />,
  };
  const displayedValues = takeItemsWithPinnedDashboardLabelsLast(values, 8, (item) => item.value);

  return (
    <article className="morphism-card rounded-2xl p-5">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {iconByField[title] || <Clock3 className="h-4 w-4 text-sky-600" />}
          <h3 className="text-sm font-semibold uppercase tracking-wide text-sky-700">{title}</h3>
        </div>
        <button
          type="button"
          onClick={() => downloadOverviewCardData(title, displayedValues, categoryTitle)}
          className="flex h-9 w-9 items-center justify-center rounded-full border border-slate-400/40 bg-white/25 p-0 text-slate-700 transition-all duration-200 hover:-translate-y-0.5 hover:border-slate-500/60 hover:bg-white/35 hover:shadow-[0_6px_14px_rgba(15,23,42,0.15)] disabled:opacity-40 disabled:hover:translate-y-0 disabled:hover:shadow-none"
          title={`Download ${title}`}
          aria-label={`Download ${title}`}
          disabled={displayedValues.length === 0}
        >
          <Download className="h-4 w-4" />
        </button>
      </div>
      <div className="w-full space-y-2 rounded-2xl border border-white/30 bg-white/20 p-3">
        {displayedValues.map((item, idx) => (
          <div key={`${title}-${item.value}-${idx}`} className="rounded-xl border border-white/25 bg-white/35 px-3 py-2">
            <div className="flex items-center justify-between text-sm">
              <span className="font-semibold text-slate-900">{item.value}</span>
              <span className="text-slate-700">
                {item.count.toLocaleString()} ({item.pct.toFixed(1)}%)
              </span>
            </div>
            <div className="mt-2 h-2.5 overflow-hidden rounded-full bg-slate-200/70">
              <div
                className="h-full rounded-full"
                style={{
                  width: `${Math.min(100, item.pct)}%`,
                  backgroundColor: CHART_COLORS[idx % CHART_COLORS.length],
                }}
              />
            </div>
          </div>
        ))}
      </div>
    </article>
  );
}

function DemographicDonutCard({ title, values, categoryTitle }: { title: string; values: DemoBucket[]; categoryTitle?: string }) {
  const displayedValues = takeItemsWithPinnedDashboardLabelsLast(values, 6, (item) => item.value);
  const data = displayedValues.map((item) => ({
    name: item.value,
    pct: Number(item.pct.toFixed(2)),
    count: item.count,
  }));

  const renderPieLabel = ({
    cx,
    cy,
    midAngle,
    outerRadius,
    percent,
    name,
  }: {
    cx?: number;
    cy?: number;
    midAngle?: number;
    outerRadius?: number;
    percent?: number;
    name?: string;
  }) => {
    if (
      cx === undefined ||
      cy === undefined ||
      midAngle === undefined ||
      outerRadius === undefined ||
      percent === undefined ||
      !name
    ) {
      return null;
    }

    const RADIAN = Math.PI / 180;
    const radius = outerRadius + 18;
    const x = cx + radius * Math.cos(-midAngle * RADIAN);
    const y = cy + radius * Math.sin(-midAngle * RADIAN);

    return (
      <text
        x={x}
        y={y}
        fill="#1e293b"
        fontSize={11}
        fontWeight={600}
        textAnchor={x > cx ? "start" : "end"}
        dominantBaseline="central"
      >
        {`${name} ${(percent * 100).toFixed(0)}%`}
      </text>
    );
  };

  return (
    <article className="morphism-card rounded-2xl p-5">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-sky-700">{title}</h3>
        <button
          type="button"
          onClick={() => downloadOverviewCardData(title, displayedValues, categoryTitle)}
          className="flex h-9 w-9 items-center justify-center rounded-full border border-slate-400/40 bg-white/25 p-0 text-slate-700 transition-all duration-200 hover:-translate-y-0.5 hover:border-slate-500/60 hover:bg-white/35 hover:shadow-[0_6px_14px_rgba(15,23,42,0.15)] disabled:opacity-40 disabled:hover:translate-y-0 disabled:hover:shadow-none"
          title={`Download ${title}`}
          aria-label={`Download ${title}`}
          disabled={displayedValues.length === 0}
        >
          <Download className="h-4 w-4" />
        </button>
      </div>
      <div className="h-56 w-full rounded-2xl border border-white/30 bg-white/20 p-3">
        <div className="flex h-full flex-col gap-3 md:flex-row md:items-center">
          <ul className="space-y-1 md:w-40">
            {data.map((item, idx) => (
              <li key={`${title}-legend-${item.name}`} className="flex items-center gap-2 text-xs">
                <span className="h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: CHART_COLORS[idx % CHART_COLORS.length] }} />
                <span className="font-semibold text-slate-800">{item.name}</span>
              </li>
            ))}
          </ul>
          <div className="h-40 flex-1 md:h-full">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Tooltip formatter={(value: number) => [`${Number(value).toFixed(1)}%`, "Share"]} />
                <Pie
                  data={data}
                  dataKey="pct"
                  nameKey="name"
                  innerRadius={42}
                  outerRadius={72}
                  paddingAngle={2}
                  strokeWidth={2}
                  labelLine={false}
                  label={renderPieLabel}
                >
                  {data.map((_, idx) => (
                    <Cell key={`${title}-slice-${idx}`} fill={CHART_COLORS[idx % CHART_COLORS.length]} stroke="rgba(255,255,255,0.9)" />
                  ))}
                </Pie>
              </PieChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>
    </article>
  );
}

function buildVerbatimWordCloudLayout(terms: VerbatimWordCloudTerm[]) {
  const viewWidth = 900;
  const viewHeight = 480;
  const centerX = viewWidth / 2;
  const centerY = viewHeight / 2;
  const palette = ["#0f172a", "#0369a1", "#0f766e", "#7c2d12", "#7c3aed", "#be123c", "#166534", "#1d4ed8"];
  const sortedTerms = [...terms]
    .map((term) => ({ text: String(term.text || "").trim(), value: Number(term.value || 0) }))
    .filter((term) => term.text && term.value > 0)
    .sort((left, right) => {
      const diff = right.value - left.value;
      if (diff !== 0) return diff;
      return left.text.localeCompare(right.text, undefined, { sensitivity: "base" });
    })
    .slice(0, 50);

  if (!sortedTerms.length) {
    return { viewWidth, viewHeight, placements: [] as Array<Record<string, number | string>> };
  }

  const max = Math.max(...sortedTerms.map((term) => term.value), 1);
  const min = Math.min(...sortedTerms.map((term) => term.value), max);
  const cloudRadiusX = 300;
  const cloudRadiusY = 218;
  const placedBoxes: Array<{ left: number; top: number; right: number; bottom: number }> = [];
  const placements: Array<{
    text: string;
    value: number;
    x: number;
    y: number;
    fontSize: number;
    color: string;
  }> = [];

  const overlaps = (candidate: { left: number; top: number; right: number; bottom: number }) =>
    placedBoxes.some(
      (box) =>
        !(candidate.right < box.left || candidate.left > box.right || candidate.bottom < box.top || candidate.top > box.bottom),
    );

  sortedTerms.forEach((term, index) => {
    const ratio = max === min ? 1 : Math.pow((term.value - min) / (max - min), 0.42);
    const baseFontSize = 22 + ratio * 60;
    let currentFontSize = baseFontSize;
    let placed = false;

    while (!placed && currentFontSize >= 14) {
      const boxWidth = Math.max(currentFontSize * 2.6, term.text.length * currentFontSize * 0.63);
      const boxHeight = currentFontSize * 1.26;

      for (let step = 0; step < 2800; step += 1) {
        const angle = step * 2.399963229728653;
        const radius = 3 + 5.1 * Math.sqrt(step);
        const x = centerX + Math.cos(angle) * radius;
        const y = centerY + Math.sin(angle) * radius;
        const normX = (x - centerX) / cloudRadiusX;
        const normY = (y - centerY) / cloudRadiusY;
        const candidate = {
          left: x - boxWidth / 2 - 5,
          right: x + boxWidth / 2 + 5,
          top: y - boxHeight / 2 - 5,
          bottom: y + boxHeight / 2 + 5,
        };

        if (normX * normX + normY * normY > 1.01) continue;
        if (candidate.left < 20 || candidate.right > viewWidth - 20 || candidate.top < 20 || candidate.bottom > viewHeight - 20) {
          continue;
        }
        if (overlaps(candidate)) continue;

        placements.push({
          text: term.text,
          value: term.value,
          x,
          y,
          fontSize: currentFontSize,
          color: palette[index % palette.length],
        });
        placedBoxes.push(candidate);
        placed = true;
        break;
      }

      if (!placed) {
        currentFontSize -= 2;
      }
    }
  });

  return { viewWidth, viewHeight, placements };
}

function VerbatimWordCloud({ terms }: { terms: VerbatimWordCloudTerm[] }) {
  if (!terms.length) {
    return <p className="text-sm text-slate-600">No word-cloud terms available for this selection.</p>;
  }

  const { viewWidth, viewHeight, placements } = buildVerbatimWordCloudLayout(terms);

  return (
    <div className="overflow-hidden rounded-[24px] border border-white/20 bg-[radial-gradient(circle_at_center,rgba(255,255,255,0.55),rgba(255,255,255,0.22)_60%,rgba(255,255,255,0.08))]">
      <svg viewBox={`0 0 ${viewWidth} ${viewHeight}`} className="h-[480px] w-full" role="img" aria-label="Verbatim word cloud">
        {placements.map((item, index) => (
          <text
            key={`${item.text}-${index}`}
            x={Number(item.x)}
            y={Number(item.y)}
            textAnchor="middle"
            dominantBaseline="middle"
            style={{
              fontSize: `${Number(item.fontSize)}px`,
              fill: String(item.color),
              fontWeight: 700,
              letterSpacing: "-0.01em",
            } as CSSProperties}
          >
            {item.text}
            <title>{`${item.text}: ${Number(item.value).toLocaleString()}`}</title>
          </text>
        ))}
      </svg>
    </div>
  );
}

function VerbatimTopicsGrid({ topics }: { topics: VerbatimTopic[] }) {
  if (!topics.length) {
    return <p className="text-sm text-slate-600">No topics could be derived for the current selection.</p>;
  }

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {topics.map((topic) => (
        <article
          key={topic.id}
          className="rounded-[24px] border border-white/30 bg-white/20 p-4 shadow-[0_12px_28px_rgba(15,23,42,0.08)]"
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="text-base font-semibold text-slate-900">{topic.label}</h3>
              <p className="mt-1 text-xs font-medium uppercase tracking-[0.12em] text-slate-500">
                {topic.responseCount.toLocaleString()} responses • {topic.share.toFixed(1)}%
              </p>
            </div>
            <div className="flex flex-wrap justify-end gap-2">
              {topic.keywords.map((keyword) => (
                <span
                  key={`${topic.id}-${keyword}`}
                  className="rounded-full border border-sky-400/35 bg-sky-100/60 px-3 py-1 text-xs font-semibold text-sky-800"
                >
                  {keyword}
                </span>
              ))}
            </div>
          </div>
          <div className="mt-4 space-y-2">
            {topic.samples.map((sample, index) => (
              <blockquote
                key={`${topic.id}-sample-${index}`}
                className="rounded-2xl border border-white/25 bg-white/30 px-3 py-2 text-sm text-slate-700"
              >
                “{sample}”
              </blockquote>
            ))}
          </div>
        </article>
      ))}
    </div>
  );
}

function VerbatimTopicSummaryTable({ topics }: { topics: VerbatimTopic[] }) {
  if (!topics.length) {
    return <p className="text-sm text-slate-600">No topics could be derived for the current selection.</p>;
  }

  const getSentimentClassName = (sentiment: string) => {
    const normalized = String(sentiment || "").toLowerCase();
    if (normalized === "positive") {
      return "border-emerald-300/50 bg-emerald-100/70 text-emerald-800";
    }
    if (normalized === "negative") {
      return "border-rose-300/50 bg-rose-100/70 text-rose-800";
    }
    return "border-slate-300/50 bg-slate-100/70 text-slate-700";
  };

  return (
    <div className="overflow-x-auto rounded-[20px] border border-white/25 bg-white/18">
      <table className="min-w-full border-collapse text-left">
        <thead>
          <tr className="border-b border-white/20 bg-white/18 text-xs font-semibold uppercase tracking-[0.12em] text-slate-600">
            <th className="px-4 py-3">Topic</th>
            <th className="px-4 py-3">Responses</th>
            <th className="px-4 py-3">Share</th>
            <th className="px-4 py-3">Sentiment</th>
            <th className="px-4 py-3">Keywords</th>
          </tr>
        </thead>
        <tbody>
          {topics.slice(0, 6).map((topic) => (
            <tr key={topic.id} className="border-b border-white/15 last:border-b-0">
              <td className="px-4 py-3 text-sm font-semibold text-slate-900">{topic.label}</td>
              <td className="px-4 py-3 text-sm text-slate-700">{topic.responseCount.toLocaleString()}</td>
              <td className="px-4 py-3 text-sm text-slate-700">{topic.share.toFixed(1)}%</td>
              <td className="px-4 py-3 text-sm text-slate-700">
                <span
                  className={`inline-flex rounded-full border px-2.5 py-1 text-xs font-semibold ${getSentimentClassName(topic.sentiment)}`}
                >
                  {topic.sentiment || "Neutral"}
                </span>
              </td>
              <td className="px-4 py-3 text-sm text-sky-800">{topic.keywords.join(", ") || "None"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function Dashboard() {
  const { slug: rawSlug = "" } = useParams();
  const slug = rawSlug.trim();
  const location = useLocation();
  const navigate = useNavigate();
  const { user } = useAuth();
  const dashboardBasePath = location.pathname.startsWith("/admin/dashboard/") ? "/admin/dashboard" : "/dashboard";
  const category = useMemo(() => categories.find((item) => item.slug === slug), [slug]);
  const hasValidCategorySlug = Boolean(slug && category);
  const isMalt = isMaltCategorySlug(slug);
  // const partnerLogo = useMemo(() => getCategoryBrandLogo(slug), [slug]);
  const isAdminDashboardRoute = location.pathname.startsWith("/admin/dashboard/");
  const isAdminUser = user?.role === "admin";
  const titleTextClass = isMalt ? "text-[#C8A44D]" : "text-red-600";
  const solidAccentButtonClass = isMalt
    ? "border-[#C8A44D] bg-[#C8A44D] text-slate-950 hover:bg-[#b89333]"
    : "border-red-500 bg-red-500 text-white hover:bg-red-600";
  const textAccentActionClass = isMalt ? "text-[#8c6a20] hover:text-[#6d5118]" : "text-red-600 hover:text-red-700";
  const activeSelectionButtonClass = isMalt
    ? "border border-[#C8A44D] bg-[#C8A44D] text-slate-950"
    : "border border-red-500 bg-red-500 text-white";
  const accentCheckboxClass = isMalt ? "accent-[#C8A44D]" : "accent-red-500";

  const [pages, setPages] = useState<PageDef[]>([]);
  const [pageData, setPageData] = useState<PageQuestion[]>([]);
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [syncStatus, setSyncStatus] = useState<SyncStatusResponse | null>(null);
  const sawActiveSyncRef = useRef(false);
  const [monthOptions, setMonthOptions] = useState<string[]>([]);
  const [monthSelectionMode, setMonthSelectionMode] = useState<MonthSelectionMode>("single");
  const [awarenessViewMode, setAwarenessViewMode] = useState<AwarenessViewMode>("brand_awareness");
  const [purchaseBehaviourMode, setPurchaseBehaviourMode] = useState<PurchaseBehaviourMode>("purchase_frequency");
  const [campaignCheckMode, setCampaignCheckMode] = useState<CampaignCheckMode>("ad_recall");
  const [flexMode, setFlexMode] = useState<FlexViewMode>("qfs");
  const [selectedFlexQuestionByMode, setSelectedFlexQuestionByMode] = useState<Record<FlexViewMode, FlexQuestionKey>>({
    qfs: "qfs1",
    qfsb: "qfsb1",
    qfw: "qfw1",
  });
  const previousMonthSelectionModeRef = useRef<MonthSelectionMode>("single");
  const [selectedMonthGroupIds, setSelectedMonthGroupIds] = useState<string[]>([]);
  const [awarenessTrendData, setAwarenessTrendData] = useState<AwarenessMonthTrend[]>([]);
  const [mediaSourceRows, setMediaSourceRows] = useState<AwarenessSummaryRow[]>([]);
  const [awarenessBrandOrder, setAwarenessBrandOrder] = useState<string[]>([]);
  const [brandImageryOptionUniverse, setBrandImageryOptionUniverse] = useState<string[]>([]);
  const [purchaseMetricDefs, setPurchaseMetricDefs] = useState<AwarenessMetricDef[]>([]);
  const [campaignMetricDefs, setCampaignMetricDefs] = useState<AwarenessMetricDef[]>([]);
  const [selectedMonths, setSelectedMonths] = useState<string[]>([]);
  const [awarenessFilters, setAwarenessFilters] = useState<AwarenessFilterState>({
    region: [],
    income: [],
    gender: [],
    age: [],
    sec: [],
    week: [],
  });
  const [awarenessFilterOptions, setAwarenessFilterOptions] = useState<AwarenessFilterOptions>({
    region: [],
    income: [],
    gender: [],
    age: [],
    sec: [],
    week: [],
  });
  const [loadingPages, setLoadingPages] = useState(true);
  const [loadingFilters, setLoadingFilters] = useState(true);
  const [loadingData, setLoadingData] = useState(false);
  const [loadingOverview, setLoadingOverview] = useState(false);
  const [loadingAwarenessTrend, setLoadingAwarenessTrend] = useState(false);
  const [customTableModalOpen, setCustomTableModalOpen] = useState(false);
  const [lastCustomTableGeneratedAt, setLastCustomTableGeneratedAt] = useState("");
  const [lastCustomTableSummary, setLastCustomTableSummary] = useState<{
    top: number;
    side: number;
    analyses: number;
  } | null>(null);
  const [lastCustomTablePayload, setLastCustomTablePayload] = useState<CustomTableSelectionPayload | null>(null);
  const [customTableResult, setCustomTableResult] = useState<CustomTableResponse | null>(null);
  const [loadingCustomTable, setLoadingCustomTable] = useState(false);
  const [customTableError, setCustomTableError] = useState("");
  const [customTableRefreshNonce, setCustomTableRefreshNonce] = useState(0);
  const verbatimQuestionOptions = useMemo(() => getVerbatimQuestionOptions(slug), [slug]);
  const [selectedVerbatimQuestionId, setSelectedVerbatimQuestionId] = useState("");
  const [verbatimInsights, setVerbatimInsights] = useState<VerbatimInsightsResponse | null>(null);
  const [loadingVerbatimInsights, setLoadingVerbatimInsights] = useState(false);
  const [verbatimError, setVerbatimError] = useState("");
  const [selectedVerbatimGroupId, setSelectedVerbatimGroupId] = useState("");
  const lastCustomTableRequestKeyRef = useRef("");
  const customTableRequestAbortRef = useRef<AbortController | null>(null);
  const customTableRequestSequenceRef = useRef(0);
  const selectedMonthGroupIdsRef = useRef<string[]>([]);
  const [error, setError] = useState("");
  const [exportModalType, setExportModalType] = useState<DataTableExportType | null>(null);
  const [exportScope, setExportScope] = useState<DataTableExportScope>("current");
  const [exportingDataTables, setExportingDataTables] = useState(false);
  const [exportProgress, setExportProgress] = useState(0);
  const [exportError, setExportError] = useState("");
  const [exportAvailability, setExportAvailability] = useState<Partial<Record<DataTableExportType, boolean>>>({});

  const updateSelectedMonthGroupIds = (nextValue: string[] | ((prev: string[]) => string[])) => {
    setSelectedMonthGroupIds((prev) => {
      const next = typeof nextValue === "function" ? nextValue(prev) : nextValue;
      selectedMonthGroupIdsRef.current = next;
      return next;
    });
  };

  const sidebarColor = getDashboardSidebarColor();
  const canShowCampaignCheck = CAMPAIGN_CHECK_ALLOWED_SLUGS.has(slug);
  const hasCampaignCheckSubpages = canShowCampaignCheck && slug === SNACKS_CAMPAIGN_CHECK_SLUG;
  const canShowFlavourFlex = FLAVOUR_FLEX_ALLOWED_SLUGS.has(slug);
  const canShowVerbatimTopics = verbatimQuestionOptions.length > 0;
  const pathParts = location.pathname.split("/").filter(Boolean);
  const pageRouteIdx = pathParts.findIndex((part) => part === "page");
  const rawSelectedPageId = pageRouteIdx >= 0 ? pathParts[pageRouteIdx + 1] || "" : "";
  const canonicalSelectedPageId =
    rawSelectedPageId === LEGACY_BRAND_AWARENESS_SUBPAGE_ID || rawSelectedPageId === LEGACY_AD_AWARENESS_SUBPAGE_ID
      ? AWARENESS_SUBPAGE_ID
      : rawSelectedPageId === AWARENESS_PAGE_ID
      ? AWARENESS_SUBPAGE_ID
      : rawSelectedPageId === FLAVOUR_FLEX_PAGE_ID
        ? canShowFlavourFlex
          ? FLAVOUR_SUBPAGE_ID
          : ""
        : rawSelectedPageId === CAMPAIGN_CHECK_PAGE_ID && hasCampaignCheckSubpages
          ? RADIO_JINGLE_SUBPAGE_ID
        : rawSelectedPageId;
  const isFlavourFlexRoute = canonicalSelectedPageId.startsWith(FLAVOUR_FLEX_SUBPAGE_PREFIX);
  const isCampaignCheckSubpageRoute = canonicalSelectedPageId.startsWith(CAMPAIGN_CHECK_SUBPAGE_PREFIX);
  const selectedPageId =
    EXCLUDED_PAGE_IDS.has(canonicalSelectedPageId) ||
    (!canShowFlavourFlex && isFlavourFlexRoute) ||
    (!canShowCampaignCheck && (canonicalSelectedPageId === CAMPAIGN_CHECK_PAGE_ID || isCampaignCheckSubpageRoute)) ||
    (!hasCampaignCheckSubpages && isCampaignCheckSubpageRoute)
      ? ""
      : canonicalSelectedPageId;
  const selectedPageIdRef = useRef(selectedPageId);
  const effectiveSelectedPageId = selectedPageId.startsWith(AWARENESS_SUBPAGE_PREFIX)
    ? AWARENESS_PAGE_ID
    : selectedPageId.startsWith(FLAVOUR_FLEX_SUBPAGE_PREFIX)
      ? FLAVOUR_FLEX_PAGE_ID
      : selectedPageId.startsWith(CAMPAIGN_CHECK_SUBPAGE_PREFIX)
        ? CAMPAIGN_CHECK_PAGE_ID
      : selectedPageId;
  const isAwarenessTrendPage =
    selectedPageId === AWARENESS_SUBPAGE_ID || selectedPageId === MEDIA_SOURCE_SUBPAGE_ID || selectedPageId === USAGE_SUBPAGE_ID;
  const isPrimaryAwarenessTrendPage = selectedPageId === AWARENESS_SUBPAGE_ID;
  const isPurchaseBehaviourTrendPage = selectedPageId === PURCHASE_BEHAVIOUR_PAGE_ID;
  const isCampaignCheckTrendPage = selectedPageId === CAMPAIGN_CHECK_PAGE_ID && canShowCampaignCheck && !hasCampaignCheckSubpages;
  const isBrandImageryTrendPage = selectedPageId === BRAND_IMAGERY_PAGE_ID;
  const isFlavourTrendPage = selectedPageId === FLAVOUR_SUBPAGE_ID && canShowFlavourFlex;
  const isFlexTrendPage = selectedPageId === FLEX_SUBPAGE_ID && canShowFlavourFlex;
  const isTrendPage =
    isAwarenessTrendPage ||
    isPurchaseBehaviourTrendPage ||
    isBrandImageryTrendPage ||
    isCampaignCheckTrendPage ||
    isFlavourTrendPage ||
    isFlexTrendPage;
  const isTables = pathParts[pathParts.length - 1] === "tables";
  const isCustomTable = pathParts[pathParts.length - 1] === CUSTOM_TABLE_ROUTE_SEGMENT;
  const isExportReport = pathParts[pathParts.length - 1] === EXPORT_REPORT_ROUTE_SEGMENT;
  const isVerbatimsTopics = pathParts[pathParts.length - 1] === VERBATIMS_TOPICS_ROUTE_SEGMENT;
  const isOverview = !selectedPageId && !isTables && !isCustomTable && !isExportReport && !isVerbatimsTopics;
  const activeFlexQuestionKey = selectedFlexQuestionByMode[flexMode];
  const activeFlexQuestionOption =
    FLEX_MODE_CONFIG[flexMode].questionOptions.find((option) => option.key === activeFlexQuestionKey) ||
    FLEX_MODE_CONFIG[flexMode].questionOptions[0];
  const selectedVerbatimQuestionOption =
    verbatimQuestionOptions.find((option) => option.id === selectedVerbatimQuestionId) || verbatimQuestionOptions[0] || null;

  const selectedPage = pages.find((page) => page.id === effectiveSelectedPageId);
  const pageTitle = isCustomTable
    ? "Custom Table"
    : isExportReport
    ? "Data Table Exports"
    : isVerbatimsTopics
    ? "Verbatims & Topics"
    : isTables
    ? "Data Tables"
    : isOverview
    ? "Demographics"
    : AWARENESS_SUBPAGE_TITLES[selectedPageId] ||
      FLAVOUR_FLEX_SUBPAGE_TITLES[selectedPageId] ||
      CAMPAIGN_CHECK_SUBPAGE_TITLES[selectedPageId] ||
      FIXED_PAGE_TITLES[selectedPageId] ||
      selectedPage?.title ||
      "Overview";

  useEffect(() => {
    if (!isExportReport) return;
    let active = true;
    const controller = new AbortController();
    fetch(`${API_BASE}/api/exports/status`, {
      signal: controller.signal,
      cache: "no-store",
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((payload: ExportStatusResponse | null) => {
        if (!active || !payload?.types) return;
        setExportAvailability({
          interim: payload.types.interim?.active !== false,
          full: payload.types.full?.active !== false,
          rolling: payload.types.rolling?.active !== false,
          quarter: payload.types.quarter?.active !== false,
        });
      })
      .catch(() => {});
    return () => {
      active = false;
      controller.abort();
    };
  }, [isExportReport]);

  const campaignCheckMediaSrc = useMemo(() => {
    if (!isCampaignCheckTrendPage) return "";
    if (slug === "toothpaste") return TOOTHPASTE_CAMPAIGN_MEDIA_SRC;
    if (slug === "bleach" || slug === "toilet-cleaner") return HYPO_CAMPAIGN_MEDIA_SRC;
    return "";
  }, [isCampaignCheckTrendPage, slug]);

  useEffect(() => {
    selectedPageIdRef.current = selectedPageId;
  }, [selectedPageId]);

  useEffect(() => {
    let active = true;
    let timer: number | null = null;
    let controller: AbortController | null = null;

    const scheduleNextStatusCheck = (delayMs: number) => {
      if (!active) return;
      timer = window.setTimeout(loadSyncStatus, delayMs);
    };

    const loadSyncStatus = async () => {
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 10_000);
      try {
        const response = await fetch(`${API_BASE}/api/sync/status`, {
          signal: controller.signal,
          cache: "no-store",
        });
        if (!response.ok) {
          throw new Error(`Sync status request failed (${response.status})`);
        }
        const status = (await response.json()) as SyncStatusResponse;
        if (!active) return;
        if (status.running) {
          sawActiveSyncRef.current = true;
        } else if (sawActiveSyncRef.current) {
          window.location.reload();
          return;
        }
        setSyncStatus(status);
        scheduleNextStatusCheck(status.running ? 10_000 : 60_000);
      } catch (_err) {
        // Dashboard data remains usable when sync status is temporarily unavailable.
        scheduleNextStatusCheck(60_000);
      } finally {
        window.clearTimeout(timeout);
        controller = null;
      }
    };

    loadSyncStatus();
    return () => {
      active = false;
      if (timer !== null) window.clearTimeout(timer);
      controller?.abort();
    };
  }, []);

  useEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [location.pathname]);

  useEffect(() => {
    if (!isCustomTable) return;
    setCustomTableModalOpen(true);
  }, [isCustomTable, location.pathname]);

  useEffect(() => {
    setAwarenessFilters({
      region: [],
      income: [],
      gender: [],
      age: [],
      sec: [],
      week: [],
    });
    setMonthSelectionMode("single");
    previousMonthSelectionModeRef.current = "single";
    updateSelectedMonthGroupIds([]);
  }, [location.pathname]);

  useEffect(() => {
    setCustomTableResult(null);
    setCustomTableError("");
    setLastCustomTableGeneratedAt("");
    setLastCustomTableSummary(null);
    setLastCustomTablePayload(null);
    lastCustomTableRequestKeyRef.current = "";
    customTableRequestAbortRef.current?.abort();
    customTableRequestAbortRef.current = null;
  }, [slug]);

  useEffect(() => {
    return () => {
      customTableRequestAbortRef.current?.abort();
      customTableRequestAbortRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!isPurchaseBehaviourTrendPage) return;
    setPurchaseBehaviourMode("purchase_frequency");
  }, [isPurchaseBehaviourTrendPage, location.pathname]);
  useEffect(() => {
    if (!isPrimaryAwarenessTrendPage) return;
    setAwarenessViewMode("brand_awareness");
  }, [isPrimaryAwarenessTrendPage, location.pathname]);
  useEffect(() => {
    if (!isCampaignCheckTrendPage) return;
    setCampaignCheckMode("ad_recall");
  }, [isCampaignCheckTrendPage, location.pathname]);
  useEffect(() => {
    if (!isFlexTrendPage) return;
    setFlexMode("qfs");
  }, [isFlexTrendPage, location.pathname]);
  useEffect(() => {
    setSelectedFlexQuestionByMode({
      qfs: "qfs1",
      qfsb: "qfsb1",
      qfw: "qfw1",
    });
  }, [location.pathname]);

  useEffect(() => {
    if (!verbatimQuestionOptions.length) {
      setSelectedVerbatimQuestionId("");
      return;
    }
    setSelectedVerbatimQuestionId((prev) =>
      verbatimQuestionOptions.some((option) => option.id === prev) ? prev : verbatimQuestionOptions[0].id,
    );
  }, [verbatimQuestionOptions]);

  useEffect(() => {
    const groups = verbatimInsights?.groups || [];
    if (!groups.length) {
      setSelectedVerbatimGroupId("");
      return;
    }
    setSelectedVerbatimGroupId((prev) => (groups.some((group) => group.id === prev) ? prev : groups[0].id));
  }, [verbatimInsights]);

  useEffect(() => {
    if (!hasValidCategorySlug || !isBrandImageryTrendPage) {
      setBrandImageryOptionUniverse([]);
      return;
    }

    let cancelled = false;
    const controller = new AbortController();

    fetchJson<{ questions?: Array<{ label?: string; question?: string; questionLabel?: string; questionCodes?: string[] }> }>(
      `${API_BASE}/api/section-questions`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          slug,
          pageId: BRAND_IMAGERY_PAGE_ID,
        }),
      },
    )
      .then((payload) => {
        if (cancelled) return;
        const seen = new Set<string>();
        const options = (payload.questions || [])
          .map((item) =>
            inferQbiStatementFromQuestion(
              String(item.questionCodes?.[0] || item.question || ""),
              String(item.label || item.questionLabel || item.question || ""),
            ),
          )
          .map((value) => String(value || "").trim())
          .filter((value) => {
            if (!value || seen.has(value)) return false;
            seen.add(value);
            return true;
          });
        setBrandImageryOptionUniverse(options);
      })
      .catch((err: Error) => {
        if (isAbortError(err) || cancelled) return;
        setBrandImageryOptionUniverse([]);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [slug, hasValidCategorySlug, isBrandImageryTrendPage]);

  useEffect(() => {
    if (!hasValidCategorySlug) {
      setPages([]);
      setLoadingPages(false);
      return;
    }
    let cancelled = false;
    const controller = new AbortController();
    setLoadingPages(true);
    setError("");

    fetchJson<{ pages: PageDef[] }>(`${API_BASE}/api/pages/${slug}`, { signal: controller.signal })
      .then((data) => {
        if (!cancelled) {
          setPages(
            (data.pages || []).filter((page) => {
              if (EXCLUDED_PAGE_IDS.has(page.id)) return false;
              if (page.id === CAMPAIGN_CHECK_PAGE_ID && !canShowCampaignCheck) return false;
              if (page.id === FLAVOUR_FLEX_PAGE_ID && !canShowFlavourFlex) return false;
              return true;
            }),
          );
        }
      })
      .catch((err: Error) => {
        if (isAbortError(err)) return;
        if (!cancelled) setError(err.message || "Failed to load page definitions");
      })
      .finally(() => {
        if (!cancelled) setLoadingPages(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [slug, hasValidCategorySlug, canShowCampaignCheck, canShowFlavourFlex]);

  useEffect(() => {
    if (!hasValidCategorySlug) {
      setMonthOptions([]);
      setAwarenessFilterOptions({ region: [], income: [], gender: [], age: [], sec: [], week: [] });
      setLoadingFilters(false);
      return;
    }
    let cancelled = false;
    const controller = new AbortController();
    setLoadingFilters(true);
    fetchJson<{ filters?: Record<string, string[]> }>(`${API_BASE}/api/filters/${slug}`, { signal: controller.signal })
      .then((data) => {
        if (cancelled) return;
        const months = Array.isArray(data?.filters?.month) ? [...data.filters.month] : [];
        months.sort((a, b) => (a > b ? -1 : a < b ? 1 : 0));
        setMonthOptions(months);
        setAwarenessFilterOptions({
          region: Array.isArray(data?.filters?.Region) ? data.filters.Region : [],
          income: Array.isArray(data?.filters?.D3) ? data.filters.D3 : [],
          gender: Array.isArray(data?.filters?.Gender) ? data.filters.Gender : [],
          age: Array.isArray(data?.filters?.Age) ? data.filters.Age : [],
          sec: Array.isArray(data?.filters?.SEC) ? data.filters.SEC : [],
          week: Array.isArray(data?.filters?.Week) ? data.filters.Week : [],
        });
      })
      .catch((err: Error) => {
        if (isAbortError(err)) return;
        if (!cancelled) {
          setMonthOptions([]);
          setAwarenessFilterOptions({ region: [], income: [], gender: [], age: [], sec: [], week: [] });
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingFilters(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [slug, hasValidCategorySlug]);

  useEffect(() => {
    if (!hasValidCategorySlug || !effectiveSelectedPageId || isTrendPage) {
      setPageData([]);
      setLoadingData(false);
      return;
    }

    let cancelled = false;
    const controller = new AbortController();

    const t = window.setTimeout(() => {
      setLoadingData(true);
      setError("");

      fetchJson<{ questions: PageQuestion[] }>(`${API_BASE}/api/page-data`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug, pageId: effectiveSelectedPageId, limitQuestions: 10, limitAnswers: 6 }),
        signal: controller.signal,
      })
        .then((data) => {
          if (!cancelled) setPageData(filterPageQuestionsForSelectedPage(slug, selectedPageIdRef.current, data.questions || []));
        })
        .catch((err: Error) => {
          if (isAbortError(err)) return;
          if (!cancelled) setError(err.message || "Failed to load page data");
        })
        .finally(() => {
          if (!cancelled) setLoadingData(false);
        });
    }, DASHBOARD_FETCH_DEBOUNCE_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(t);
      controller.abort();
    };
  }, [slug, effectiveSelectedPageId, isTrendPage, hasValidCategorySlug]);

  useEffect(() => {
    if (!isOverview) return;
    if (!hasValidCategorySlug) {
      setOverview(null);
      setLoadingOverview(false);
      return;
    }
    if (loadingFilters) return;
    const monthsForOverview = selectedMonths.length > 0 ? [...selectedMonths] : monthOptions[0] ? [monthOptions[0]] : [];
    if (monthsForOverview.length === 0) return;
    let cancelled = false;
    const controller = new AbortController();
    const t = window.setTimeout(() => {
      setLoadingOverview(true);
      setError("");

      fetchJson<OverviewResponse>(`${API_BASE}/api/overview-demographics`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug, months: monthsForOverview }),
        signal: controller.signal,
      })
        .then((data) => {
          if (!cancelled) setOverview(data);
        })
        .catch((err: Error) => {
          if (isAbortError(err)) return;
          if (!cancelled) setError(err.message || "Failed to load overview demographics");
        })
        .finally(() => {
          if (!cancelled) setLoadingOverview(false);
        });
    }, DASHBOARD_FETCH_DEBOUNCE_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(t);
      controller.abort();
    };
  }, [slug, isOverview, selectedMonths, monthOptions, loadingFilters, hasValidCategorySlug]);

  useEffect(() => {
    if (!hasValidCategorySlug || !isTrendPage) {
      setAwarenessTrendData([]);
      setMediaSourceRows([]);
      setAwarenessBrandOrder([]);
      setPurchaseMetricDefs([]);
      setCampaignMetricDefs([]);
      setLoadingAwarenessTrend(false);
      return;
    }
    if (loadingFilters) {
      return;
    }
    const trendLatestMonthKey = getLatestMonthKey(monthOptions);
    const groupsByMode: MonthButtonGroup[] = buildMonthButtonGroupsForMode(monthSelectionMode, monthOptions, trendLatestMonthKey);
    const groupsById = new Map<string, MonthButtonGroup>(
      groupsByMode.map((group): [string, MonthButtonGroup] => [group.id, group]),
    );
    const fallbackGroup = groupsByMode.find((group) => !group.disabled) || groupsByMode[0] || null;
    const resolvedGroups =
      selectedMonthGroupIds
        .map((id) => groupsById.get(id))
        .filter((group): group is MonthButtonGroup => Boolean(group && !group.disabled));
    const periodGroups: MonthButtonGroup[] = resolvedGroups.length > 0 ? resolvedGroups : fallbackGroup ? [fallbackGroup] : [];
    const monthsForTrend = Array.from(new Set(periodGroups.flatMap((group) => group.months))).sort((a, b) => (a > b ? -1 : a < b ? 1 : 0));
    if (!monthsForTrend.length) {
      setAwarenessTrendData([]);
      setAwarenessBrandOrder([]);
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    const t = window.setTimeout(() => {
      setLoadingAwarenessTrend(true);
      setError("");

      const trendLoader = isPurchaseBehaviourTrendPage
        ? fetchPurchaseBehaviourSummary(slug, monthsForTrend, awarenessFilters, purchaseBehaviourMode, controller.signal)
        : isCampaignCheckTrendPage
          ? fetchCampaignCheckSummary(slug, monthsForTrend, awarenessFilters, campaignCheckMode, controller.signal)
        : isBrandImageryTrendPage
          ? fetchBrandImagerySummary(slug, monthsForTrend, awarenessFilters, controller.signal)
        : isFlavourTrendPage
          ? fetchFlavourSummary(slug, monthsForTrend, awarenessFilters, controller.signal)
        : isFlexTrendPage
          ? fetchFlexSummary(slug, monthsForTrend, awarenessFilters, flexMode, activeFlexQuestionKey, controller.signal)
        : fetchAwarenessSummary(slug, monthsForTrend, awarenessFilters, selectedPageId, controller.signal, awarenessViewMode).then((data) => ({
            rows: data.rows,
            metricDefs: getAwarenessMetricDefs(selectedPageId, awarenessViewMode),
          }));

      trendLoader
        .then((response) => {
          if (cancelled) return;
          const metricDefs =
            isPurchaseBehaviourTrendPage || isCampaignCheckTrendPage || isFlexTrendPage || isFlavourTrendPage
              ? response.metricDefs
              : getAwarenessMetricDefs(selectedPageId, awarenessViewMode);
          setPurchaseMetricDefs(isPurchaseBehaviourTrendPage ? metricDefs : []);
          setCampaignMetricDefs(isCampaignCheckTrendPage ? metricDefs : []);
          const allowedMetrics = new Set(metricDefs.map((metric) => metric.key));
          const canonicalBrandOrder = Array.from(
            new Map(
              (response.rows || [])
                .filter((row) => row.brand && row.brand !== "(No response)")
                .sort((left, right) => {
                  const leftOrder = Number(left.brand_order ?? Number.MAX_SAFE_INTEGER);
                  const rightOrder = Number(right.brand_order ?? Number.MAX_SAFE_INTEGER);
                  if (leftOrder !== rightOrder) return leftOrder - rightOrder;
                  return String(left.brand).localeCompare(String(right.brand), undefined, {
                    numeric: true,
                    sensitivity: "base",
                  });
                })
                .map((row) => [row.brand, row.brand]),
            ).values(),
          );
          setAwarenessBrandOrder(canonicalBrandOrder);
          const byMonth = new Map<string, AwarenessMonthTrend>();
          const periodMetricMonthBase = new Map<string, number>();
          const periodMediaMentionByBrandOption = new Map<string, Map<string, Map<string, number>>>();
          const periodMediaBrandMonthBase = new Map<string, Map<string, Map<string, number>>>();
          const monthToPeriods = new Map<string, string[]>();

          periodGroups.forEach((period) => {
            const metrics: Record<string, Record<string, number>> = {};
            const counts: Record<string, Record<string, number>> = {};
            const bases: Record<string, number> = {};
            metricDefs.forEach((metric) => {
              metrics[metric.key] = {};
              counts[metric.key] = {};
              bases[metric.key] = 0;
            });
            byMonth.set(period.label, { month: period.label, metrics, counts, bases });
            period.months.forEach((month) => {
              const list = monthToPeriods.get(month) || [];
              list.push(period.label);
              monthToPeriods.set(month, list);
            });
          });

          (response.rows || []).forEach((row) => {
            const month = String(row.month || "");
            const metric = String(row.metric || "").toLowerCase();
            const brand = String(row.brand || "").trim();
            if (!month || !allowedMetrics.has(metric) || !brand || brand === "(No response)") return;
            const mention = Number(row.mention_n || 0);
            const base = Number(row.base_n || 0);
            const option = String(row.option || "").trim();
            const periodLabels = monthToPeriods.get(month);
            if (!periodLabels || periodLabels.length === 0) return;

            periodLabels.forEach((periodLabel) => {
              const bucket = byMonth.get(periodLabel);
              if (!bucket) return;
              const baseKey = `${periodLabel}__${metric}__${month}`;
              periodMetricMonthBase.set(baseKey, Math.max(Number(periodMetricMonthBase.get(baseKey) || 0), base));
              bucket.counts[metric][brand] = Number(bucket.counts[metric][brand] || 0) + mention;

              if ((metric === "media_source" || metric === "brand_imagery") && option) {
                let mediaByBrand = periodMediaMentionByBrandOption.get(periodLabel);
                if (!mediaByBrand) {
                  mediaByBrand = new Map<string, Map<string, number>>();
                  periodMediaMentionByBrandOption.set(periodLabel, mediaByBrand);
                }
                let optionsByBrand = mediaByBrand.get(brand);
                if (!optionsByBrand) {
                  optionsByBrand = new Map<string, number>();
                  mediaByBrand.set(brand, optionsByBrand);
                }
                optionsByBrand.set(option, Number(optionsByBrand.get(option) || 0) + mention);

                let baseByBrand = periodMediaBrandMonthBase.get(periodLabel);
                if (!baseByBrand) {
                  baseByBrand = new Map<string, Map<string, number>>();
                  periodMediaBrandMonthBase.set(periodLabel, baseByBrand);
                }
                let baseByMonth = baseByBrand.get(brand);
                if (!baseByMonth) {
                  baseByMonth = new Map<string, number>();
                  baseByBrand.set(brand, baseByMonth);
                }
                baseByMonth.set(month, Math.max(Number(baseByMonth.get(month) || 0), base));
              }
            });
          });

          const mediaRowsAgg: AwarenessSummaryRow[] = [];
          periodGroups.forEach((period) => {
            const periodLabel = period.label;
            const bucket = byMonth.get(periodLabel);
            if (!bucket) return;
            metricDefs.forEach((metricDef) => {
              const metricKey = metricDef.key;
              const periodBase = period.months.reduce((sum, month) => sum + Number(periodMetricMonthBase.get(`${periodLabel}__${metricKey}__${month}`) || 0), 0);
              bucket.bases[metricKey] = periodBase;

              Object.entries(bucket.counts[metricKey] || {}).forEach(([brandName, mention]) => {
                const mentionCount = Number(mention || 0);
                bucket.metrics[metricKey][brandName] = periodBase > 0 ? Number(((mentionCount / periodBase) * 100).toFixed(1)) : 0;
              });

              if (!isPurchaseBehaviourTrendPage && (metricKey === "media_source" || metricKey === "brand_imagery")) {
                const mediaByBrand = periodMediaMentionByBrandOption.get(periodLabel);
                const baseByBrand = periodMediaBrandMonthBase.get(periodLabel);
                if (!mediaByBrand || !baseByBrand) return;
                mediaByBrand.forEach((optionsByBrand, brandName) => {
                  const baseByMonth = baseByBrand.get(brandName);
                  const mediaBase = period.months.reduce((sum, month) => sum + Number(baseByMonth?.get(month) || 0), 0);
                  optionsByBrand.forEach((mention, optionValue) => {
                    mediaRowsAgg.push({
                      month: periodLabel,
                      brand: brandName,
                      option: optionValue,
                      metric: "media_source",
                      mention_n: mention,
                      base_n: mediaBase,
                      pct: mediaBase > 0 ? Number(((mention / mediaBase) * 100).toFixed(1)) : 0,
                    });
                  });
                });
              }
            });

            if (
              metricDefs.some((metric) => metric.key === "total_awareness")
              && Object.keys(bucket.counts.total_awareness || {}).length === 0
            ) {
              const totalBase = Math.max(
                Number(bucket.bases.brand_tom || 0),
                Number(bucket.bases.brand_spont || 0),
                Number(bucket.bases.aided || 0),
              );
              bucket.bases.total_awareness = totalBase;
              const totalBrands = new Set<string>([
                ...Object.keys(bucket.counts.brand_tom || {}),
                ...Object.keys(bucket.counts.brand_spont || {}),
                ...Object.keys(bucket.counts.aided || {}),
              ]);
              totalBrands.forEach((brandName) => {
                const c1 = Number(bucket.counts.brand_tom?.[brandName] || 0);
                const c2 = Number(bucket.counts.brand_spont?.[brandName] || 0);
                const c3 = Number(bucket.counts.aided?.[brandName] || 0);
                const p1 = Number(bucket.metrics.brand_tom?.[brandName] || 0);
                const p2 = Number(bucket.metrics.brand_spont?.[brandName] || 0);
                const p3 = Number(bucket.metrics.aided?.[brandName] || 0);
                bucket.counts.total_awareness[brandName] = c1 + c2 + c3;
                bucket.metrics.total_awareness[brandName] = Number((p1 + p2 + p3).toFixed(1));
              });
            }

            if (
              metricDefs.some((metric) => metric.key === "total_ad_awareness")
              && Object.keys(bucket.counts.total_ad_awareness || {}).length === 0
            ) {
              const totalBase = Math.max(
                Number(bucket.bases.ad_tom || 0),
                Number(bucket.bases.ad_spont || 0),
                Number(bucket.bases.aided_ad || 0),
              );
              bucket.bases.total_ad_awareness = totalBase;
              const totalBrands = new Set<string>([
                ...Object.keys(bucket.counts.ad_tom || {}),
                ...Object.keys(bucket.counts.ad_spont || {}),
                ...Object.keys(bucket.counts.aided_ad || {}),
              ]);
              totalBrands.forEach((brandName) => {
                const c1 = Number(bucket.counts.ad_tom?.[brandName] || 0);
                const c2 = Number(bucket.counts.ad_spont?.[brandName] || 0);
                const c3 = Number(bucket.counts.aided_ad?.[brandName] || 0);
                const p1 = Number(bucket.metrics.ad_tom?.[brandName] || 0);
                const p2 = Number(bucket.metrics.ad_spont?.[brandName] || 0);
                const p3 = Number(bucket.metrics.aided_ad?.[brandName] || 0);
                bucket.counts.total_ad_awareness[brandName] = c1 + c2 + c3;
                bucket.metrics.total_ad_awareness[brandName] = Number((p1 + p2 + p3).toFixed(1));
              });
            }
          });

          setMediaSourceRows(mediaRowsAgg.length > 0 ? mediaRowsAgg : response.rows || []);
          const sorted = periodGroups
            .map((period) => byMonth.get(period.label))
            .filter(Boolean) as AwarenessMonthTrend[];
          setAwarenessTrendData(sorted);
        })
        .catch((err: Error) => {
          if (isAbortError(err)) return;
          if (!cancelled) setError(err.message || "Failed to load awareness trend data");
        })
        .finally(() => {
          if (!cancelled) setLoadingAwarenessTrend(false);
        });
    }, DASHBOARD_FETCH_DEBOUNCE_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(t);
      controller.abort();
    };
  }, [
    hasValidCategorySlug,
    slug,
    selectedPageId,
    awarenessFilters,
    isTrendPage,
    isPurchaseBehaviourTrendPage,
    isCampaignCheckTrendPage,
    isBrandImageryTrendPage,
    isFlavourTrendPage,
    isFlexTrendPage,
    purchaseBehaviourMode,
    campaignCheckMode,
    flexMode,
    activeFlexQuestionKey,
    awarenessViewMode,
    selectedMonthGroupIds,
    monthSelectionMode,
    monthOptions,
    loadingFilters,
  ]);

  const toggleMonthGroup = (group: MonthButtonGroup) => {
    if (group.disabled) return;
    updateSelectedMonthGroupIds((prev) => {
      const baseline = prev.length > 0 ? prev : defaultMonthGroup ? [defaultMonthGroup.id] : [];
      if (baseline.includes(group.id)) {
        const next = baseline.filter((id) => id !== group.id);
        return next.length > 0 ? next : defaultMonthGroup ? [defaultMonthGroup.id] : prev;
      }
      return [...baseline, group.id];
    });
    queueCustomTableRefresh();
  };
  const clearSelectedMonths = () => {
    updateSelectedMonthGroupIds(defaultMonthGroup ? [defaultMonthGroup.id] : []);
    queueCustomTableRefresh();
  };
  const toggleAwarenessFilter = (key: keyof AwarenessFilterState, value: string) => {
    setAwarenessFilters((prev) => ({
      ...prev,
      [key]: prev[key].includes(value) ? prev[key].filter((item) => item !== value) : [...prev[key], value],
    }));
  };
  const clearAwarenessFilters = () => {
    setAwarenessFilters({ region: [], income: [], gender: [], age: [], sec: [], week: [] });
  };
  const clearAwarenessFilter = (key: keyof AwarenessFilterState) => {
    setAwarenessFilters((prev) => ({ ...prev, [key]: [] }));
  };
  const hasAwarenessFilterSelection = Object.values(awarenessFilters).some((values) => values.length > 0);
  const customTableSections = useMemo(() => {
    const sectionById = new Map<string, string>();
    const addSection = (id: string, title: string) => {
      if (!id || sectionById.has(id)) return;
      sectionById.set(id, title);
    };

    // Keep hidden pages excluded except Demographics which is explicitly required here.
    addSection("screener-demographics", "Demographics");
    pages.forEach((page) => addSection(page.id, page.title));
    // Keep parity with dashboard sidebar which always exposes these entries.
    addSection(PURCHASE_BEHAVIOUR_PAGE_ID, "Purchase Behaviour");
    addSection(BRAND_IMAGERY_PAGE_ID, "Brand Imagery");
    if (canShowCampaignCheck) {
      addSection(CAMPAIGN_CHECK_PAGE_ID, "Campaign Check");
    }

    return Array.from(sectionById.entries()).map(([id, title]) => ({ id, title }));
  }, [pages, canShowCampaignCheck]);

  const pageNavigation = useMemo(() => {
    const nav: Array<{ key: string; title: string; path: string }> = [{ key: "__overview", title: "Demographics", path: `${dashboardBasePath}/${slug}` }];
    const awarenessParent = pages.find((page) => page.id === AWARENESS_PAGE_ID);
    const purchaseBehaviourPage = pages.find((page) => page.id === PURCHASE_BEHAVIOUR_PAGE_ID);
    const brandImageryPage = pages.find((page) => page.id === BRAND_IMAGERY_PAGE_ID);
    const flavourFlexPage = pages.find((page) => page.id === FLAVOUR_FLEX_PAGE_ID);
    const campaignCheckPage = pages.find((page) => page.id === CAMPAIGN_CHECK_PAGE_ID);
    if (awarenessParent) {
      nav.push({ key: AWARENESS_SUBPAGE_ID, title: "Awareness", path: `${dashboardBasePath}/${slug}/page/${AWARENESS_SUBPAGE_ID}` });
      nav.push({ key: MEDIA_SOURCE_SUBPAGE_ID, title: "Media Source", path: `${dashboardBasePath}/${slug}/page/${MEDIA_SOURCE_SUBPAGE_ID}` });
      nav.push({ key: USAGE_SUBPAGE_ID, title: "Usage", path: `${dashboardBasePath}/${slug}/page/${USAGE_SUBPAGE_ID}` });
      if (purchaseBehaviourPage) {
        nav.push({ key: purchaseBehaviourPage.id, title: "Purchase Behaviour", path: `${dashboardBasePath}/${slug}/page/${purchaseBehaviourPage.id}` });
      }
      if (brandImageryPage) {
        nav.push({ key: brandImageryPage.id, title: "Brand Imagery", path: `${dashboardBasePath}/${slug}/page/${brandImageryPage.id}` });
      }
      if (flavourFlexPage) {
        nav.push({ key: FLAVOUR_SUBPAGE_ID, title: "Flavour", path: `${dashboardBasePath}/${slug}/page/${FLAVOUR_SUBPAGE_ID}` });
        nav.push({ key: FLEX_SUBPAGE_ID, title: "Flex", path: `${dashboardBasePath}/${slug}/page/${FLEX_SUBPAGE_ID}` });
      }
      if (campaignCheckPage && canShowCampaignCheck) {
        if (hasCampaignCheckSubpages) {
          SNACKS_CAMPAIGN_CHECK_SUBPAGES.forEach((subpage) => {
            nav.push({ key: subpage.id, title: subpage.title, path: `${dashboardBasePath}/${slug}/page/${subpage.id}` });
          });
        } else {
          nav.push({ key: campaignCheckPage.id, title: "Campaign Check", path: `${dashboardBasePath}/${slug}/page/${campaignCheckPage.id}` });
        }
      }
    }
    pages
      .filter(
        (page) =>
          page.id !== AWARENESS_PAGE_ID &&
          page.id !== PURCHASE_BEHAVIOUR_PAGE_ID &&
          page.id !== BRAND_IMAGERY_PAGE_ID &&
          page.id !== FLAVOUR_FLEX_PAGE_ID &&
          page.id !== CAMPAIGN_CHECK_PAGE_ID &&
          !EXCLUDED_PAGE_IDS.has(page.id),
      )
      .forEach((page) => nav.push({ key: page.id, title: page.title, path: `${dashboardBasePath}/${slug}/page/${page.id}` }));
    if (canShowVerbatimTopics) {
      nav.push({ key: "__verbatims_topics", title: "Verbatims & Topics", path: `${dashboardBasePath}/${slug}/${VERBATIMS_TOPICS_ROUTE_SEGMENT}` });
    }
    nav.push({ key: "__custom_table", title: "Custom Table", path: `${dashboardBasePath}/${slug}/${CUSTOM_TABLE_ROUTE_SEGMENT}` });
    nav.push({ key: "__export_report", title: "Data Table Exports", path: `${dashboardBasePath}/${slug}/${EXPORT_REPORT_ROUTE_SEGMENT}` });
    return nav;
  }, [pages, slug, dashboardBasePath, canShowCampaignCheck, canShowVerbatimTopics, hasCampaignCheckSubpages]);
  const currentNavKey = isOverview
    ? "__overview"
    : isCustomTable
    ? "__custom_table"
    : isExportReport
    ? "__export_report"
    : isVerbatimsTopics
    ? "__verbatims_topics"
    : isTables
    ? "__tables"
    : selectedPageId;
  const currentNavIndex = pageNavigation.findIndex((item) => item.key === currentNavKey);
  const previousNav = currentNavIndex > 0 ? pageNavigation[currentNavIndex - 1] : null;
  const nextNav = currentNavIndex >= 0 && currentNavIndex < pageNavigation.length - 1 ? pageNavigation[currentNavIndex + 1] : null;
  const isBlockedPageRoute =
    rawSelectedPageId === CAMPAIGN_CHECK_PAGE_ID
      ? !canShowCampaignCheck
      : rawSelectedPageId.startsWith(CAMPAIGN_CHECK_SUBPAGE_PREFIX)
        ? !hasCampaignCheckSubpages
      : rawSelectedPageId === FLAVOUR_FLEX_PAGE_ID || rawSelectedPageId.startsWith(FLAVOUR_FLEX_SUBPAGE_PREFIX)
        ? !canShowFlavourFlex
        : isVerbatimsTopics
          ? !canShowVerbatimTopics
        : false;
  const lastCustomTableGeneratedLabel = useMemo(() => {
    if (!lastCustomTableGeneratedAt) return "";
    const date = new Date(lastCustomTableGeneratedAt);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString();
  }, [lastCustomTableGeneratedAt]);
  const hasCustomTableExportableResult = useMemo(() => {
    const normalized = normalizeCustomTableResponse(customTableResult);
    return Boolean(normalized && Array.isArray(normalized.tables) && normalized.tables.length > 0);
  }, [customTableResult]);
  const runCustomTableGeneration = async (
    payload: CustomTableSelectionPayload,
    months: string[],
    options: { clearExisting?: boolean; periodGroups?: MonthButtonGroup[] } = {},
  ) => {
    if (!hasValidCategorySlug) {
      throw new Error("Category slug is missing");
    }
    const periodGroups = Array.isArray(options.periodGroups) ? options.periodGroups : [];
    const requestKey = buildCustomTableRequestKey(payload, months, periodGroups);
    customTableRequestAbortRef.current?.abort();
    const controller = new AbortController();
    customTableRequestAbortRef.current = controller;
    const requestSequence = ++customTableRequestSequenceRef.current;

    if (options.clearExisting) {
      setCustomTableResult(null);
    }
    setLoadingCustomTable(true);
    setCustomTableError("");
    try {
      const response = await fetchJson<CustomTableResponse>(`${API_BASE}/api/custom-table`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          slug,
          topQuestions: payload.selectedQuestionsByBreak.top,
          sideQuestions: payload.selectedQuestionsByBreak.side,
          displayMode: payload.selectedDisplayModeId,
          analysisOptions: payload.selectedAnalysis,
          formatOptions: payload.formatOptions,
          filters: {},
          months,
          monthGroups: periodGroups.map((group) => ({
            id: group.id,
            label: group.label,
            months: group.months,
          })),
        }),
      });
      if (!Array.isArray(response?.tables)) {
        throw new Error("Custom table endpoint returned an unexpected response shape.");
      }

      if (controller.signal.aborted || requestSequence !== customTableRequestSequenceRef.current) {
        return null;
      }

      const normalizedResponse = normalizeCustomTableResponse({
        ...response,
        displayMode: payload.selectedDisplayModeId,
        analysisOptions: payload.selectedAnalysis,
        formatOptions: payload.formatOptions,
      });
      setCustomTableResult(normalizedResponse);
      setLastCustomTableGeneratedAt(normalizedResponse?.generatedAt || response.generatedAt || payload.generatedAt);
      setLastCustomTablePayload(payload);
      lastCustomTableRequestKeyRef.current = requestKey;
      return normalizedResponse;
    } catch (err) {
      if (isAbortError(err)) return null;
      throw err;
    } finally {
      if (requestSequence === customTableRequestSequenceRef.current) {
        customTableRequestAbortRef.current = null;
        setLoadingCustomTable(false);
      }
    }
  };

  const getCustomTableGenerationMonths = (payload: CustomTableSelectionPayload, periodGroups = getSelectedCustomTablePeriodGroups()) => {
    void payload;
    return Array.from(new Set(periodGroups.flatMap((group) => group.months))).sort((a, b) => (a > b ? -1 : a < b ? 1 : 0));
  };

  const queueCustomTableRefresh = () => {
    if (!isCustomTable || !lastCustomTablePayload) return;
    lastCustomTableRequestKeyRef.current = "";
    setCustomTableError("");
    setCustomTableRefreshNonce((value) => value + 1);
  };

  const handleCustomTableGenerate = async (payload: CustomTableSelectionPayload) => {
    const countFor = (breakId: "top" | "side") => {
      const bySection = payload.selectedQuestionIdsByBreak[breakId] || {};
      return Object.values(bySection).reduce((sum, ids) => sum + (Array.isArray(ids) ? ids.length : 0), 0);
    };

    try {
      const periodGroups = getSelectedCustomTablePeriodGroups();
      const generated = await runCustomTableGeneration(payload, getCustomTableGenerationMonths(payload, periodGroups), {
        clearExisting: true,
        periodGroups,
      });
      if (!generated) return;
      setLastCustomTableSummary({
        top: countFor("top"),
        side: countFor("side"),
        analyses: payload.selectedAnalysis.length,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to generate custom table";
      setCustomTableError(message);
      throw err;
    }
  };
  const handleCustomTableExport = () => {
    if (!hasCustomTableExportableResult || !category) return;
    downloadCustomTableWorkbook(customTableResult, category.category);
  };
  const openDataTableExportModal = (type: DataTableExportType) => {
    setExportModalType(type);
    setExportScope("current");
    setExportProgress(0);
    setExportError("");
  };
  const closeDataTableExportModal = () => {
    if (exportingDataTables) return;
    setExportModalType(null);
    setExportProgress(0);
    setExportError("");
  };
  const handleDataTableExport = async () => {
    if (!exportModalType || !slug) return;
    setExportingDataTables(true);
    setExportError("");
    setExportProgress(1);
    const progressTimer = window.setInterval(() => {
      setExportProgress((value) => (value >= 95 ? value : Math.min(95, value + Math.max(1, Math.round((96 - value) / 9)))));
    }, 180);

    try {
      const response = await fetch(`${API_BASE}/api/exports/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          type: exportModalType,
          scope: isAdminUser ? exportScope : "current",
          slug,
          requestedBy: isAdminUser ? "admin" : "category",
        }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok || !payload?.ok) {
        throw new Error(payload?.error || `Export failed (${response.status})`);
      }
      setExportProgress(100);
      const downloadUrl = `${API_BASE}${payload.downloadUrl}`;
      const link = document.createElement("a");
      link.href = downloadUrl;
      link.download = payload.filename || "";
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => {
        setExportingDataTables(false);
        setExportModalType(null);
        setExportProgress(0);
      }, 450);
    } catch (err) {
      setExportError(err instanceof Error ? err.message : "Failed to export data tables.");
      setExportProgress(0);
      setExportingDataTables(false);
    } finally {
      window.clearInterval(progressTimer);
    }
  };

  const executiveSummary = useMemo(() => {
    if (!overview) return null;
    const regionTop = overview.distributions.Region?.[0];
    const genderTop = overview.distributions.Gender?.[0];
    const secTop = overview.distributions.SEC?.[0];
    const ageTop = overview.distributions.Age?.[0];
    const monthsInScope = selectedMonths.length > 0 ? selectedMonths : monthOptions[0] ? [monthOptions[0]] : [];
    const summaryLatestMonthKey = getLatestMonthKey(monthOptions);
    const monthScope = monthsInScope.length > 0 ? monthsInScope.map((month) => formatMonthLabel(month, summaryLatestMonthKey)).join(", ") : "all available months";
    const pct = (value?: number) => (Number.isFinite(value) ? `${Number(value).toFixed(1)}%` : "N/A");

    return {
      monthScope,
      totalRespondents: overview.totalRespondents.toLocaleString(),
      regionTop: { value: regionTop?.value || "N/A", pct: pct(regionTop?.pct) },
      genderTop: { value: genderTop?.value || "N/A", pct: pct(genderTop?.pct) },
      ageTop: { value: ageTop?.value || "N/A", pct: pct(ageTop?.pct) },
      secTop: { value: secTop?.value || "N/A", pct: pct(secTop?.pct) },
    };
  }, [overview, selectedMonths, monthOptions]);

  const showMonthFilter = isOverview || isTrendPage || isCustomTable || isVerbatimsTopics;
  const showAwarenessFilters = isTrendPage || isVerbatimsTopics;
  const hasHeaderModeButtons =
    isPrimaryAwarenessTrendPage || isPurchaseBehaviourTrendPage || isCampaignCheckTrendPage || isFlexTrendPage;
  const monthFilterOptions = useMemo(() => {
    const source = isOverview ? overview?.monthsAvailable || [] : monthOptions;
    return [...source].sort((a, b) => (a > b ? -1 : a < b ? 1 : 0));
  }, [isOverview, overview, monthOptions]);
  const latestMonthKey = useMemo(() => getLatestMonthKey(monthFilterOptions), [monthFilterOptions]);
  const monthButtonGroups = useMemo<MonthButtonGroup[]>(() => {
    return buildMonthButtonGroupsForMode(monthSelectionMode, monthFilterOptions, latestMonthKey);
  }, [monthFilterOptions, latestMonthKey, monthSelectionMode]);
  const defaultMonthGroup = useMemo(
    () => monthButtonGroups.find((group) => !group.disabled) || monthButtonGroups[0] || null,
    [monthButtonGroups],
  );
  const selectedMonthGroups = useMemo(
    () => monthButtonGroups.filter((group) => selectedMonthGroupIds.includes(group.id) && !group.disabled),
    [monthButtonGroups, selectedMonthGroupIds],
  );
  const effectiveSelectedMonths = useMemo(
    () =>
      selectedMonthGroups.length > 0
        ? Array.from(new Set(selectedMonthGroups.flatMap((group) => group.months))).sort((a, b) => (a > b ? -1 : a < b ? 1 : 0))
        : defaultMonthGroup
          ? [...defaultMonthGroup.months].sort((a, b) => (a > b ? -1 : a < b ? 1 : 0))
          : [],
    [selectedMonthGroups, defaultMonthGroup],
  );
  const trendMonths = useMemo(
    () =>
      selectedMonthGroups.length > 0
        ? selectedMonthGroups.map((group) => group.label)
        : defaultMonthGroup
          ? [defaultMonthGroup.label]
          : [],
    [selectedMonthGroups, defaultMonthGroup],
  );
  const trendPeriodGroups = useMemo(
    () =>
      selectedMonthGroups.length > 0
        ? selectedMonthGroups
        : defaultMonthGroup
          ? [defaultMonthGroup]
          : [],
    [selectedMonthGroups, defaultMonthGroup],
  );

  useEffect(() => {
    selectedMonthGroupIdsRef.current = selectedMonthGroupIds;
  }, [selectedMonthGroupIds]);

  const getSelectedCustomTablePeriodGroups = (
    selectedIdsSource = selectedMonthGroupIds,
    groupsSource = monthButtonGroups,
    fallbackGroup = defaultMonthGroup,
  ) => {
    const selectedIds = new Set(selectedIdsSource);
    const groups = groupsSource.filter((group) => selectedIds.has(group.id) && !group.disabled);
    if (groups.length > 0) return groups;
    return fallbackGroup ? [fallbackGroup] : [];
  };

  useEffect(() => {
    if (!hasValidCategorySlug || !isVerbatimsTopics || !selectedVerbatimQuestionOption) {
      setVerbatimInsights(null);
      setLoadingVerbatimInsights(false);
      setVerbatimError("");
      return;
    }
    if (loadingFilters) return;
    if (!effectiveSelectedMonths.length) {
      setVerbatimInsights(null);
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    const t = window.setTimeout(() => {
      setLoadingVerbatimInsights(true);
      setVerbatimError("");

      fetchJson<VerbatimInsightsResponse>(`${API_BASE}/api/verbatim-topics`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          slug,
          questionCode: selectedVerbatimQuestionOption.questionCode,
          groupByBrand: Boolean(selectedVerbatimQuestionOption.groupByBrand),
          filters: awarenessFilters,
          months: effectiveSelectedMonths,
        }),
      })
        .then((data) => {
          if (!cancelled) setVerbatimInsights(data);
        })
        .catch((err: Error) => {
          if (isAbortError(err)) return;
          if (!cancelled) {
            setVerbatimInsights(null);
            setVerbatimError(err.message || "Failed to load verbatim insights");
          }
        })
        .finally(() => {
          if (!cancelled) setLoadingVerbatimInsights(false);
        });
    }, DASHBOARD_FETCH_DEBOUNCE_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(t);
      controller.abort();
    };
  }, [
    hasValidCategorySlug,
    isVerbatimsTopics,
    selectedVerbatimQuestionOption,
    slug,
    awarenessFilters,
    effectiveSelectedMonths,
    loadingFilters,
  ]);

  useEffect(() => {
    if (!showMonthFilter) return;
    const validIds = new Set(monthButtonGroups.filter((group) => !group.disabled).map((group) => group.id));
    updateSelectedMonthGroupIds((prev) => {
      previousMonthSelectionModeRef.current = monthSelectionMode;
      const cleaned = prev.filter((id) => validIds.has(id));
      if (cleaned.length > 0) return cleaned;
      return defaultMonthGroup ? [defaultMonthGroup.id] : [];
    });
  }, [showMonthFilter, monthSelectionMode, monthButtonGroups, defaultMonthGroup]);

  useEffect(() => {
    const months = selectedMonthGroups.length > 0
      ? Array.from(new Set(selectedMonthGroups.flatMap((group) => group.months))).sort((a, b) => (a > b ? -1 : a < b ? 1 : 0))
      : defaultMonthGroup
        ? [...defaultMonthGroup.months].sort((a, b) => (a > b ? -1 : a < b ? 1 : 0))
        : [];
    setSelectedMonths((prev) => (areStringArraysEqual(prev, months) ? prev : months));
  }, [selectedMonthGroups, defaultMonthGroup]);

  useEffect(() => {
    if (!hasValidCategorySlug || !isCustomTable || !lastCustomTablePayload) return;
    const periodGroups = getSelectedCustomTablePeriodGroups(
      selectedMonthGroupIds,
      monthButtonGroups,
      defaultMonthGroup,
    );
    const generationMonths = getCustomTableGenerationMonths(lastCustomTablePayload, periodGroups);
    const nextKey = buildCustomTableRequestKey(lastCustomTablePayload, generationMonths, periodGroups);
    if (nextKey === lastCustomTableRequestKeyRef.current) return;

    runCustomTableGeneration(lastCustomTablePayload, generationMonths, { periodGroups, clearExisting: true }).catch((err) => {
      const message = err instanceof Error ? err.message : "Failed to refresh custom table";
      setCustomTableError(message);
    });
  }, [
    isCustomTable,
    lastCustomTablePayload,
    effectiveSelectedMonths,
    trendPeriodGroups,
    monthButtonGroups,
    defaultMonthGroup,
    monthSelectionMode,
    selectedMonthGroupIds,
    customTableRefreshNonce,
    slug,
    hasValidCategorySlug,
  ]);

  const awarenessMetricDefs = isPurchaseBehaviourTrendPage
    ? purchaseMetricDefs
    : isCampaignCheckTrendPage
      ? campaignMetricDefs
      : isFlexTrendPage
        ? activeFlexQuestionOption
          ? FLEX_MODE_CONFIG[flexMode].metricDefs.filter((metric) => metric.key === activeFlexQuestionOption.key)
          : FLEX_MODE_CONFIG[flexMode].metricDefs
        : isFlavourTrendPage
          ? FLAVOUR_METRIC_DEFS
      : getAwarenessMetricDefs(selectedPageId, awarenessViewMode);
  const trendTableTitle = isFlexTrendPage
    ? `${pageTitle} - ${activeFlexQuestionOption?.buttonLabel || FLEX_MODE_CONFIG[flexMode].buttonLabel}`
    : pageTitle;
  const trendRowHeaderLabel = isFlavourTrendPage
    ? "Flavour"
    : isFlexTrendPage
      ? "Response Option"
      : undefined;
  const trendEmptyStateMessage = isFlexTrendPage
    ? `${activeFlexQuestionOption?.buttonLabel || "This question"} has no available data in the current dataset for the selected month(s).`
    : undefined;
  const selectedVerbatimGroup =
    verbatimInsights?.groups.find((group) => group.id === selectedVerbatimGroupId) || verbatimInsights?.groups[0] || null;
  const selectedVerbatimQuestionLabel =
    selectedVerbatimQuestionOption?.label ||
    verbatimInsights?.questionLabel ||
    "Verbatims";
  const awarenessBrandUniverse = useMemo(() => {
    const set = new Set<string>();
    awarenessBrandOrder.forEach((brand) => {
      if (brand && brand !== "(No response)") set.add(brand);
    });
    awarenessTrendData.forEach((month) => {
      awarenessMetricDefs.forEach((metric) => {
        Object.keys(month.metrics[metric.key] || {}).forEach((brand) => {
          if (brand && brand !== "(No response)") set.add(brand);
        });
      });
    });
    return Array.from(set);
  }, [awarenessBrandOrder, awarenessTrendData, awarenessMetricDefs]);
  const awarenessFilterGroups: Array<{ key: keyof AwarenessFilterState; label: string; options: string[] }> = [
    { key: "region", label: "Region", options: awarenessFilterOptions.region },
    { key: "income", label: "Income", options: awarenessFilterOptions.income },
    { key: "gender", label: "Gender", options: awarenessFilterOptions.gender },
    { key: "age", label: "Age", options: awarenessFilterOptions.age },
    { key: "sec", label: "SEC", options: awarenessFilterOptions.sec },
    { key: "week", label: "Week", options: awarenessFilterOptions.week },
  ];
  const activeTableSelections = useMemo<ActiveSelectionChip[]>(() => {
    const chips: ActiveSelectionChip[] = [];

    if (showMonthFilter && effectiveSelectedMonths.length > 0) {
      chips.unshift({
        key: "months",
        label: "Months",
        value: effectiveSelectedMonths
          .map((month) => formatMonthLabel(month, latestMonthKey))
          .join(", "),
      });
    }

    if (showAwarenessFilters) {
      awarenessFilterGroups.forEach((group) => {
        const values = awarenessFilters[group.key] || [];
        if (values.length === 0) return;
        chips.push({
          key: `filter-${group.key}`,
          label: group.label,
          value: summarizeSelectionValues(values, 2),
        });
      });
    }

    return chips;
  }, [
    showMonthFilter,
    effectiveSelectedMonths,
    latestMonthKey,
    showAwarenessFilters,
    awarenessFilterGroups,
    awarenessFilters,
  ]);

  if (!category) return <Navigate to="/" replace />;
  if (isAdminDashboardRoute && isAdminRestrictedCategory(slug)) {
    return <Navigate to="/admin/categories" replace />;
  }
  if (hasCampaignCheckSubpages && rawSelectedPageId === CAMPAIGN_CHECK_PAGE_ID) {
    return <Navigate to={`${dashboardBasePath}/${slug}/page/${RADIO_JINGLE_SUBPAGE_ID}`} replace />;
  }
  if (isBlockedPageRoute) {
    return <Navigate to={`${dashboardBasePath}/${slug}`} replace />;
  }
  if (isTables) return <Navigate to={`${dashboardBasePath}/${slug}`} replace />;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8, scale: 0.995 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -6, scale: 0.99 }}
      transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
      style={sidebarColor ? ({ "--sidebar-accent": sidebarColor } as CSSProperties) : undefined}
      className="min-h-screen bg-[#d2d7df] text-slate-900"
    >
      <SidebarProvider defaultOpen>
        <DashboardSidebar slug={slug} pages={pages} />
        <SidebarInset className="relative bg-[#d2d7df]">
          <FloatingLogosBackground slug={slug} />
          <header className="z-20 border-b border-white/30 bg-white/18 px-3 py-3 backdrop-blur-md sm:px-4 sm:py-4 md:sticky md:top-0 md:px-6 md:py-5">
            <div className="flex flex-col gap-3 sm:gap-4 lg:flex-row lg:items-start lg:justify-between">
              <div className="min-w-0 flex-1 lg:self-center">
                <div className="flex min-w-0 items-start gap-2.5 sm:gap-3">
                  <SidebarTrigger className="mt-1 md:hidden" />
                  <div className="flex min-w-0 items-start gap-2.5 sm:gap-3">
                    {/* <img
                      src={partnerLogo.src}
                      alt={partnerLogo.alt}
                      className="h-10 w-auto max-w-[120px] rounded-md object-contain sm:h-12 sm:max-w-[140px] md:h-14 md:max-w-none"
                    /> */}
                    <div className="min-w-0">
                      <p className="text-xs uppercase tracking-wide text-slate-600 sm:text-sm">{category.category}</p>
                      <h1 className={`break-words text-[clamp(1.8rem,7vw,2.75rem)] font-bold leading-tight ${titleTextClass}`}>{pageTitle}</h1>
                      {syncStatus?.running || syncStatus?.lastSuccessAt ? (
                        <p className="mt-1 flex items-center gap-1.5 text-[11px] font-medium text-slate-600 sm:text-xs">
                          <Clock3 className="h-3.5 w-3.5" />
                          {syncStatus.running
                            ? "SurveyCTO update in progress"
                            : `Data updated ${formatSyncTimestamp(syncStatus.lastSuccessAt || "")}`}
                        </p>
                      ) : null}
                    </div>
                  </div>
                </div>
              </div>

              {showMonthFilter ? (
                <div className="flex w-full min-w-0 flex-col gap-2 lg:flex-1 lg:flex-row lg:items-stretch">
                  <div className="morphism-card w-full rounded-2xl p-2.5 lg:min-h-[122px] lg:w-[170px]">
                    <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-600">Grouping</p>
                    <div className="space-y-1.5">
                      {[
                        { id: "single", label: "Monthly Report" },
                        { id: "quarter", label: "Quarterly Report" },
                        { id: "rolling", label: "Rolling Report" },
                      ].map((modeOption) => {
                        const checked = monthSelectionMode === modeOption.id;
                        return (
                          <label
                            key={modeOption.id}
                            className="flex cursor-pointer items-center gap-2 whitespace-nowrap text-xs font-semibold text-slate-700"
                          >
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={() => {
                                const nextMode = modeOption.id as MonthSelectionMode;
                                const nextGroups = buildMonthButtonGroupsForMode(nextMode, monthFilterOptions, latestMonthKey);
                                const nextGroupIds = selectMonthGroupIdsForMonths(nextGroups, effectiveSelectedMonths);
                                const nextDefaultGroup = nextGroups.find((group) => !group.disabled) || nextGroups[0] || null;
                                const selectedMonthSet = new Set(effectiveSelectedMonths);
                                const overlappingGroupIds = nextGroups
                                  .filter((group) => !group.disabled && group.months.some((month) => selectedMonthSet.has(month)))
                                  .map((group) => group.id);
                                setMonthSelectionMode(nextMode);
                                updateSelectedMonthGroupIds(
                                  nextGroupIds.length > 0
                                    ? nextGroupIds
                                    : overlappingGroupIds.length > 0
                                      ? overlappingGroupIds
                                      : nextDefaultGroup
                                        ? [nextDefaultGroup.id]
                                        : [],
                                );
                                queueCustomTableRefresh();
                              }}
                              className={`h-3.5 w-3.5 rounded border border-slate-400/60 ${accentCheckboxClass}`}
                            />
                            <span>{modeOption.label}</span>
                          </label>
                        );
                      })}
                    </div>
                  </div>

                  <div className="morphism-card w-full min-w-0 rounded-2xl p-2.5 lg:min-h-[122px] lg:flex-1">
                    <div className="mb-2 flex flex-wrap items-center justify-between gap-1.5">
                      <p className="text-xs font-semibold uppercase tracking-wide text-slate-600">Month Filter</p>
                      {selectedMonthGroupIds.length > 0 ? (
                        <button
                          type="button"
                          onClick={clearSelectedMonths}
                          className={`text-xs font-semibold transition-colors ${textAccentActionClass}`}
                        >
                          Clear selection(s)
                        </button>
                      ) : null}
                    </div>
                    <div className="overflow-x-auto">
                      <div
                        className="grid min-w-max gap-1.5 lg:min-w-0"
                        style={{
                          gridTemplateColumns: `repeat(${Math.max(1, monthButtonGroups.length || 1)}, minmax(${
                            monthSelectionMode === "rolling"
                              ? 104
                              : monthSelectionMode === "quarter"
                                ? 84
                                : monthButtonGroups.length > 9
                                  ? 72
                                  : 82
                          }px, 1fr))`,
                        }}
                      >
                      {monthButtonGroups.map((group) => {
                        const active =
                          selectedMonthGroupIds.includes(group.id) ||
                          (selectedMonthGroupIds.length === 0 && defaultMonthGroup?.id === group.id);
                        return (
                          <button
                            type="button"
                            key={group.id}
                            onClick={() => toggleMonthGroup(group)}
                            disabled={group.disabled}
                            title={group.subLabel ? `${group.label} (${group.subLabel})` : group.label}
                            className={`rounded px-1.5 py-1.5 text-[11px] font-semibold ${
                              group.disabled
                                ? "cursor-not-allowed border border-slate-300/70 bg-slate-200/50 text-slate-400"
                                : active
                                  ? activeSelectionButtonClass
                                  : "toggle-morphism text-slate-800 hover:text-slate-950"
                            } flex min-h-[54px] flex-col items-center justify-center`}
                          >
                            <div className="w-full whitespace-nowrap text-center leading-tight">
                              {group.label}
                            </div>
                            {group.subLabel ? <div className="mt-0.5 text-[10px] font-medium opacity-95">{group.subLabel}</div> : null}
                          </button>
                        );
                      })}
                      </div>
                    </div>
                  </div>
                </div>
              ) : null}
            </div>

            {showAwarenessFilters ? (
              <div className="mt-3 morphism-card rounded-2xl p-2">
                <div className="mb-2 flex items-center justify-between">
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-600">Filters</p>
                  {hasAwarenessFilterSelection ? (
                    <button
                      type="button"
                      onClick={clearAwarenessFilters}
                      className={`text-xs font-semibold transition-colors ${textAccentActionClass}`}
                    >
                      Clear selection(s)
                    </button>
                  ) : null}
                </div>
                <div className="grid gap-2 md:grid-cols-3 xl:grid-cols-6">
                  {awarenessFilterGroups.map((group) => (
                    <div key={group.key} className="rounded-xl border border-white/25 bg-white/20 p-1.5">
                      <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-700">{group.label}</p>
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <button
                            type="button"
                            className="flex w-full items-center justify-between rounded border border-white/40 bg-white/20 px-2 py-1 text-left text-[11px] font-semibold text-slate-800 backdrop-blur-sm"
                          >
                            <span>
                              {awarenessFilters[group.key].length > 0
                                ? `${awarenessFilters[group.key].length} selected`
                                : `Select ${group.label}`}
                            </span>
                            <ChevronDown className="h-3.5 w-3.5 text-slate-700" />
                          </button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent
                          align="start"
                          className="w-44 max-h-72 overflow-y-auto border border-white/45 bg-white/70 text-slate-900 shadow-md backdrop-blur-md"
                        >
                          <DropdownMenuLabel className="text-[11px] uppercase tracking-wide text-slate-700">
                            {group.label}
                          </DropdownMenuLabel>
                          <DropdownMenuSeparator />
                          {group.options.map((option) => (
                            <DropdownMenuCheckboxItem
                              key={`${group.key}-${option}`}
                              checked={awarenessFilters[group.key].includes(option)}
                              onSelect={(event) => event.preventDefault()}
                              onCheckedChange={() => toggleAwarenessFilter(group.key, option)}
                              className="text-xs text-slate-800 focus:bg-slate-100 focus:text-slate-900 data-[state=checked]:font-semibold"
                            >
                              {option}
                            </DropdownMenuCheckboxItem>
                          ))}
                          <DropdownMenuSeparator />
                          <button
                            type="button"
                            onClick={() => clearAwarenessFilter(group.key)}
                            className={`w-full px-2 py-1 text-left text-xs font-semibold ${textAccentActionClass}`}
                          >
                            Clear {group.label}
                          </button>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            {isOverview ? (
              <div className="mt-3 animate-fade-in border-t border-white/35 pt-3 text-center">
                <h2 className="text-base font-semibold text-slate-900">Executive Summary</h2>
                <p className="mx-auto mt-1 max-w-6xl text-sm leading-relaxed text-slate-700">
                  {loadingOverview || !executiveSummary ? (
                    "Building narrative summary..."
                  ) : (
                    <>
                      This survey captures the voices of{" "}
                      <span className="font-semibold text-slate-900">{executiveSummary.totalRespondents}</span> respondents
                      across <span className="font-semibold text-slate-900">{executiveSummary.monthScope}</span>, giving a clear snapshot
                      of who currently engages with <span className="font-semibold text-slate-900">{category.category}</span>. The
                      strongest geographic footprint comes from{" "}
                      <span className="font-semibold text-slate-900">{executiveSummary.regionTop.value}</span> (
                      <span className="font-semibold text-slate-900">{executiveSummary.regionTop.pct}</span>). The audience leans{" "}
                      <span className="font-semibold text-slate-900">{executiveSummary.genderTop.value}</span> (
                      <span className="font-semibold text-slate-900">{executiveSummary.genderTop.pct}</span>) and peaks at{" "}
                      <span className="font-semibold text-slate-900">{executiveSummary.ageTop.value}</span> (
                      <span className="font-semibold text-slate-900">{executiveSummary.ageTop.pct}</span>). Social economic presence is led
                      by <span className="font-semibold text-slate-900">{executiveSummary.secTop.value}</span> (
                      <span className="font-semibold text-slate-900">{executiveSummary.secTop.pct}</span>), framing the value and messaging
                      signals most relevant to this wave.
                    </>
                  )}
                </p>
              </div>
            ) : null}

            {isPurchaseBehaviourTrendPage ? (
              <div className="relative z-20 mt-2 flex translate-y-1/2 justify-center">
                <div className="flex flex-wrap items-center justify-center gap-2">
                  <button
                    type="button"
                    onClick={() => setPurchaseBehaviourMode("purchase_frequency")}
                    className={`rounded-2xl border px-4 py-2 text-xs font-semibold transition-colors ${
                      purchaseBehaviourMode === "purchase_frequency"
                        ? solidAccentButtonClass
                        : "border-white/45 bg-white/28 text-slate-800 hover:bg-white/38"
                    }`}
                  >
                    Purchase Frequency
                  </button>
                  <button
                    type="button"
                    onClick={() => setPurchaseBehaviourMode("pack_size_frequency")}
                    className={`rounded-2xl border px-4 py-2 text-xs font-semibold transition-colors ${
                      purchaseBehaviourMode === "pack_size_frequency"
                        ? solidAccentButtonClass
                        : "border-white/45 bg-white/28 text-slate-800 hover:bg-white/38"
                    }`}
                  >
                    Pack Size Frequency
                  </button>
                </div>
              </div>
            ) : null}
            {isPrimaryAwarenessTrendPage ? (
              <div className="relative z-20 mt-2 flex translate-y-1/2 justify-center">
                <div className="flex flex-wrap items-center justify-center gap-2">
                  <button
                    type="button"
                    onClick={() => setAwarenessViewMode("brand_awareness")}
                    className={`rounded-2xl border px-4 py-2 text-xs font-semibold transition-colors ${
                      awarenessViewMode === "brand_awareness"
                        ? solidAccentButtonClass
                        : "border-white/45 bg-white/28 text-slate-800 hover:bg-white/38"
                    }`}
                  >
                    Brand Awareness
                  </button>
                  <button
                    type="button"
                    onClick={() => setAwarenessViewMode("ad_awareness")}
                    className={`rounded-2xl border px-4 py-2 text-xs font-semibold transition-colors ${
                      awarenessViewMode === "ad_awareness"
                        ? solidAccentButtonClass
                        : "border-white/45 bg-white/28 text-slate-800 hover:bg-white/38"
                    }`}
                  >
                    AD Awareness
                  </button>
                </div>
              </div>
            ) : null}
            {isCampaignCheckTrendPage ? (
              <div className="relative z-20 mt-2 flex translate-y-1/2 justify-center">
                <div className="flex flex-wrap items-center justify-center gap-2">
                  <button
                    type="button"
                    onClick={() => setCampaignCheckMode("ad_recall")}
                    className={`rounded-2xl border px-4 py-2 text-xs font-semibold transition-colors ${
                      campaignCheckMode === "ad_recall"
                        ? solidAccentButtonClass
                        : "border-white/45 bg-white/28 text-slate-800 hover:bg-white/38"
                    }`}
                  >
                    Ad Recall
                  </button>
                  <button
                    type="button"
                    onClick={() => setCampaignCheckMode("ad_recall_media_source")}
                    className={`rounded-2xl border px-4 py-2 text-xs font-semibold transition-colors ${
                      campaignCheckMode === "ad_recall_media_source"
                        ? solidAccentButtonClass
                        : "border-white/45 bg-white/28 text-slate-800 hover:bg-white/38"
                    }`}
                  >
                    Ad Recall Media Source
                  </button>
                </div>
              </div>
            ) : null}
            {isFlexTrendPage ? (
              <div className="relative z-20 mt-2 flex translate-y-1/2 justify-center">
                <div className="flex flex-wrap items-center justify-center gap-2">
                  {(Object.entries(FLEX_MODE_CONFIG) as Array<[FlexViewMode, (typeof FLEX_MODE_CONFIG)[FlexViewMode]]>).map(
                    ([modeKey, config]) => (
                      <button
                        key={modeKey}
                        type="button"
                        onClick={() => setFlexMode(modeKey)}
                        className={`rounded-2xl border px-4 py-2 text-xs font-semibold transition-colors ${
                          flexMode === modeKey
                            ? solidAccentButtonClass
                            : "border-white/45 bg-white/28 text-slate-800 hover:bg-white/38"
                        }`}
                      >
                        {config.buttonLabel}
                      </button>
                    ),
                  )}
                </div>
              </div>
            ) : null}
          </header>

          <main className={`relative z-10 p-3 sm:p-6 lg:p-8 ${hasHeaderModeButtons ? "pt-12 sm:pt-14" : ""}`}>
            <div className="mx-auto max-w-7xl space-y-6">
            {error ? <div className="rounded-lg border border-rose-300/40 bg-rose-100/70 p-4 text-rose-800">{error}</div> : null}

            {isOverview ? (
              <section className="grid grid-cols-1 gap-4 sm:gap-6 lg:grid-cols-2">
                {!loadingOverview ? (
                  <>
                    <DemographicListCard title="Region" values={overview?.distributions.Region || []} categoryTitle={category.category} />
                    <DemographicListCard title="Income" values={overview?.distributions.D3 || []} categoryTitle={category.category} />
                    <DemographicDonutCard title="Gender" values={overview?.distributions.Gender || []} categoryTitle={category.category} />
                    <DemographicDonutCard title="Age" values={overview?.distributions.Age || []} categoryTitle={category.category} />
                    <DemographicListCard title="SEC" values={overview?.distributions.SEC || []} categoryTitle={category.category} />
                    <DemographicListCard title="Week" values={overview?.distributions.Week || []} categoryTitle={category.category} />
                  </>
                ) : null}
              </section>
            ) : null}

            {isFlexTrendPage ? (
              <div className="flex justify-center">
                <div className="flex flex-wrap items-center justify-center gap-2">
                  {FLEX_MODE_CONFIG[flexMode].questionOptions.map((option) => (
                    <button
                      key={option.key}
                      type="button"
                      onClick={() =>
                        setSelectedFlexQuestionByMode((prev) => ({
                          ...prev,
                          [flexMode]: option.key,
                        }))
                      }
                      className={`rounded-2xl border px-4 py-2 text-xs font-semibold transition-colors ${
                        activeFlexQuestionOption?.key === option.key
                          ? solidAccentButtonClass
                          : "border-white/45 bg-white/28 text-slate-800 hover:bg-white/38"
                      }`}
                    >
                      {option.buttonLabel}
                    </button>
                  ))}
                </div>
              </div>
            ) : null}

            {isFlexTrendPage && flexMode === "qfs" ? (
              <section className="mx-auto max-w-4xl">
                <div className="overflow-hidden rounded-[24px] border border-white/35 bg-white/20 p-3 shadow-[0_16px_40px_rgba(15,23,42,0.12)] backdrop-blur-sm sm:p-4">
                  <p className="mb-3 text-center text-sm font-semibold text-slate-800">Indomie School Sampling Stimuli Ad</p>
                  <img
                    src={FLEX_QFS1_MEDIA_SRC}
                    alt="N_QFS1 supporting media"
                    className="h-auto max-h-[340px] w-full rounded-[18px] object-contain"
                    loading="lazy"
                  />
                </div>
              </section>
            ) : null}

            {isFlexTrendPage && flexMode === "qfsb" ? (
              <section className="mx-auto max-w-4xl">
                <div className="overflow-hidden rounded-[24px] border border-white/35 bg-white/20 p-3 shadow-[0_16px_40px_rgba(15,23,42,0.12)] backdrop-blur-sm sm:p-4">
                  <p className="mb-3 text-center text-sm font-semibold text-slate-800">Minimie Pick Ur Vibe Ad</p>
                  <img
                    src={FLEX_QFSB_MEDIA_SRC}
                    alt="QFSB supporting media"
                    className="h-auto max-h-[340px] w-full rounded-[18px] object-contain"
                    loading="lazy"
                  />
                </div>
              </section>
            ) : null}

            {isFlexTrendPage && flexMode === "qfw" ? (
              <section className="mx-auto max-w-4xl">
                <div className="overflow-hidden rounded-[24px] border border-white/35 bg-white/20 p-3 shadow-[0_16px_40px_rgba(15,23,42,0.12)] backdrop-blur-sm sm:p-4">
                  <p className="mb-3 text-center text-sm font-semibold text-slate-800">Minimie Naija Pepper Flavor</p>
                  <video
                    src={FLEX_QFW_MEDIA_SRC}
                    controls
                    preload="metadata"
                    playsInline
                    className="h-auto max-h-[340px] w-full rounded-[18px] bg-slate-900 object-contain"
                  >
                    Your browser does not support the video tag.
                  </video>
                </div>
              </section>
            ) : null}

            {isCampaignCheckTrendPage && campaignCheckMediaSrc ? (
              <section className="mx-auto max-w-4xl">
                <div className="overflow-hidden rounded-[24px] border border-white/35 bg-white/20 p-3 shadow-[0_16px_40px_rgba(15,23,42,0.12)] backdrop-blur-sm sm:p-4">
                  <video
                    src={campaignCheckMediaSrc}
                    controls
                    preload="metadata"
                    playsInline
                    className="h-auto max-h-[340px] w-full rounded-[18px] bg-slate-900 object-contain"
                  >
                    Your browser does not support the video tag.
                  </video>
                </div>
              </section>
            ) : null}

            {isVerbatimsTopics ? (
              <section className="space-y-5">
                <div className="flex flex-wrap items-center justify-center gap-2">
                  {verbatimQuestionOptions.map((option) => (
                    <button
                      key={option.id}
                      type="button"
                      onClick={() => setSelectedVerbatimQuestionId(option.id)}
                      className={`rounded-2xl border px-4 py-2 text-xs font-semibold transition-colors ${
                        selectedVerbatimQuestionOption?.id === option.id
                          ? solidAccentButtonClass
                          : "border-white/45 bg-white/28 text-slate-800 hover:bg-white/38"
                      }`}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>

                <article className="morphism-card rounded-2xl p-5">
                  <div className="flex flex-wrap items-start justify-between gap-4">
                    <div className="space-y-1">
                      <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Verbatim question</p>
                      <h2 className="text-lg font-semibold text-slate-900">
                        {selectedVerbatimQuestionLabel}
                      </h2>
                      <p className="text-sm text-slate-700">
                        {(verbatimInsights?.totalResponses || 0).toLocaleString()} responses in current scope.
                        {selectedVerbatimQuestionOption?.groupByBrand ? " Grouped by main brand." : ""}
                      </p>
                    </div>
                    <ActiveSelectionChipBar items={activeTableSelections} />
                  </div>

                  {selectedVerbatimQuestionOption?.groupByBrand && (verbatimInsights?.groups || []).length > 1 ? (
                    <div className="mt-4 flex flex-wrap gap-2">
                      {(verbatimInsights?.groups || []).map((group) => (
                        <button
                          key={group.id}
                          type="button"
                          onClick={() => setSelectedVerbatimGroupId(group.id)}
                          className={`rounded-full border px-4 py-2 text-xs font-semibold transition-colors ${
                            selectedVerbatimGroup?.id === group.id
                              ? solidAccentButtonClass
                              : "border-white/45 bg-white/28 text-slate-800 hover:bg-white/38"
                          }`}
                        >
                          {group.label} ({group.responseCount.toLocaleString()})
                        </button>
                      ))}
                    </div>
                  ) : null}
                </article>

                <article className="relative morphism-card rounded-2xl p-5">
                  {loadingVerbatimInsights && !selectedVerbatimGroup ? (
                    <div className="space-y-4">
                      <div className="h-5 w-48 rounded bg-white/25" />
                      <div className="grid gap-4 xl:grid-cols-[1.05fr,0.95fr]">
                        <div className="h-48 rounded-[24px] border border-white/25 bg-white/20" />
                        <div className="h-48 rounded-[24px] border border-white/25 bg-white/20" />
                      </div>
                    </div>
                  ) : verbatimError ? (
                    <div className="rounded-2xl border border-rose-300/45 bg-rose-100/70 p-4 text-sm text-rose-800">
                      {verbatimError}
                    </div>
                  ) : !selectedVerbatimGroup ? (
                    <div className="rounded-2xl border border-white/30 bg-white/20 p-4 text-sm text-slate-700">
                      No verbatim responses were found for this question in the current scope.
                    </div>
                  ) : (
                    <div className="space-y-5">
                      <div className="grid gap-5 xl:grid-cols-[1.1fr,0.9fr]">
                        <section className="rounded-[24px] border border-white/30 bg-white/20 p-4">
                          <div className="mb-4 flex items-center justify-between gap-3">
                            <div>
                              <p className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">Word Cloud</p>
                              <h3 className="text-base font-semibold text-slate-900">{selectedVerbatimGroup.label}</h3>
                            </div>
                            <p className="text-xs font-medium uppercase tracking-[0.12em] text-slate-500">
                              {selectedVerbatimGroup.responseCount.toLocaleString()} responses
                            </p>
                          </div>
                          <VerbatimWordCloud terms={selectedVerbatimGroup.wordCloud} />
                        </section>

                        <section className="rounded-[24px] border border-white/30 bg-white/20 p-4">
                          <p className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">Topic Summary</p>
                          <div className="mt-4">
                            <VerbatimTopicSummaryTable topics={selectedVerbatimGroup.topics || []} />
                          </div>
                        </section>
                      </div>

                      <section className="space-y-3">
                        <div>
                          <p className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">Topics</p>
                          <h3 className="mt-1 text-base font-semibold text-slate-900">
                            Deterministic topic groups from current verbatims
                          </h3>
                        </div>
                        <VerbatimTopicsGrid topics={selectedVerbatimGroup.topics} />
                      </section>
                    </div>
                  )}

                  {loadingVerbatimInsights && selectedVerbatimGroup ? (
                    <div className="pointer-events-none absolute inset-0 flex items-center justify-center rounded-2xl bg-white/30 backdrop-blur-[1px]">
                      <div className="rounded-lg border border-white/60 bg-white/75 px-3 py-1.5 text-xs font-semibold text-slate-700">
                        Updating word cloud and topics...
                      </div>
                    </div>
                  ) : null}
                </article>
              </section>
            ) : null}

            {isTrendPage ? (
              <section>
                <AwarenessTrendTable
                  key={`${slug}-${selectedPageId}`}
                  metricDefs={awarenessMetricDefs}
                  months={trendMonths}
                  data={awarenessTrendData}
                  brandUniverse={awarenessBrandUniverse}
                  optionUniverse={isBrandImageryTrendPage ? brandImageryOptionUniverse : []}
                  loading={loadingAwarenessTrend || loadingFilters}
                  mediaRows={mediaSourceRows}
                  tableTitle={trendTableTitle}
                  categoryTitle={category.category}
                  lineAsPie={isCampaignCheckTrendPage && campaignCheckMode === "ad_recall"}
                  hideChartSelectors={isCampaignCheckTrendPage}
                  activeSelections={activeTableSelections}
                  useGoldAccent={isMalt}
                  rowHeaderLabel={trendRowHeaderLabel}
                  emptyStateMessage={trendEmptyStateMessage}
                />
              </section>
            ) : null}

            {selectedPageId && !isTrendPage ? (
              <section className="grid gap-4 xl:grid-cols-2">
                {loadingData
                  ? Array.from({ length: 6 }).map((_, idx) => (
                      <div key={idx} className="morphism-card rounded-2xl p-5">
                        <div className="h-4 w-40 rounded bg-white/20" />
                        <div className="mt-3 h-3 w-full rounded bg-white/10" />
                      </div>
                    ))
                  : pageData.map((item) => <QuestionCard key={item.question} item={item} />)}
              </section>
            ) : null}

            {isTables ? (
              <article className="morphism-card rounded-2xl p-5">
                <h2 className="text-lg font-semibold text-slate-900">Data Tables</h2>
                <p className="mt-2 text-sm text-slate-700">
                  Table endpoint is available at <code className="text-sky-700">POST /api/table</code>. This page is ready for table UI
                  wiring against DuckDB.
                </p>
              </article>
            ) : null}

            {isCustomTable ? (
              <section className="space-y-4">
                <div className="flex justify-end">
                  <div className="flex flex-wrap items-center justify-end gap-2">
                    <button
                      type="button"
                      onClick={() => setCustomTableModalOpen(true)}
                      className={`rounded-xl border px-4 py-2 text-sm font-semibold transition-colors ${solidAccentButtonClass}`}
                    >
                      Custom Table Builder
                    </button>
                    <button
                      type="button"
                      onClick={handleCustomTableExport}
                      className="rounded-xl border border-slate-400/40 bg-white/25 px-4 py-2 text-sm font-semibold text-slate-800 transition-all duration-200 hover:-translate-y-0.5 hover:border-slate-500/60 hover:bg-white/35 hover:shadow-[0_6px_14px_rgba(15,23,42,0.15)] disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:translate-y-0 disabled:hover:shadow-none"
                      disabled={!hasCustomTableExportableResult}
                    >
                      Custom Table Export
                    </button>
                  </div>
                </div>

                <CustomTableResultsView
                  result={customTableResult}
                  loading={loadingCustomTable}
                  error={customTableError}
                  activeSelections={activeTableSelections}
                />
              </section>
            ) : null}

            {isCustomTable ? (
              <CustomTableBuilderModal
                slug={slug}
                sections={customTableSections}
                monthOptions={monthFilterOptions}
                open={customTableModalOpen}
                onOpenChange={setCustomTableModalOpen}
                generating={loadingCustomTable}
                initialSelection={lastCustomTablePayload}
                onGenerate={handleCustomTableGenerate}
              />
            ) : null}

            {isExportReport ? (
              <section className="space-y-4">
                <div className="grid gap-4 md:grid-cols-2">
                  {DATA_TABLE_EXPORT_TYPES.map((item) => {
                    const isAvailable = exportAvailability[item.type] !== false;
                    return (
                      <article key={item.type} className="morphism-card rounded-2xl p-5">
                        <div className="flex min-h-[112px] flex-col justify-between gap-5">
                          <h2 className="text-lg font-semibold text-slate-900">{item.title}</h2>
                          <button
                            type="button"
                            onClick={() => openDataTableExportModal(item.type)}
                            disabled={!isAvailable}
                            title={!isAvailable ? "This export is inactive for the current month scope." : undefined}
                            className={`inline-flex w-fit items-center gap-2 rounded-xl border px-4 py-2 text-sm font-semibold transition-colors ${solidAccentButtonClass} disabled:cursor-not-allowed disabled:opacity-45`}
                          >
                            <Download className="h-4 w-4" />
                            Download
                          </button>
                        </div>
                      </article>
                    );
                  })}
                </div>

                {exportModalType ? (
                  <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 px-4 py-6">
                    <div className="w-full max-w-md rounded-2xl border border-white/30 bg-white p-5 shadow-2xl">
                      <div className="flex items-start justify-between gap-4">
                        <div>
                          <h2 className="text-lg font-semibold text-slate-950">
                            {DATA_TABLE_EXPORT_TYPES.find((item) => item.type === exportModalType)?.title}
                          </h2>
                          <p className="mt-1 text-sm text-slate-600">
                            {isAdminUser
                              ? "Choose the category scope for this data table export."
                              : `Export data tables for ${category?.category || "this category"}.`}
                          </p>
                        </div>
                        <button
                          type="button"
                          onClick={closeDataTableExportModal}
                          disabled={exportingDataTables}
                          className="rounded-lg border border-slate-300 px-2 py-1 text-sm font-semibold text-slate-600 transition-colors hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          Close
                        </button>
                      </div>

                      {isAdminUser ? (
                        <div className="mt-5 space-y-3">
                          <label className="flex cursor-pointer items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm font-semibold text-slate-800">
                            <input
                              type="radio"
                              name="data-table-export-scope"
                              value="current"
                              checked={exportScope === "current"}
                              onChange={() => setExportScope("current")}
                              className={accentCheckboxClass}
                              disabled={exportingDataTables}
                            />
                            Current category only
                          </label>
                          <label className="flex cursor-pointer items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm font-semibold text-slate-800">
                            <input
                              type="radio"
                              name="data-table-export-scope"
                              value="all"
                              checked={exportScope === "all"}
                              onChange={() => setExportScope("all")}
                              className={accentCheckboxClass}
                              disabled={exportingDataTables}
                            />
                            All categories
                          </label>
                        </div>
                      ) : null}

                      {exportingDataTables ? (
                        <div className="mt-5">
                          <div className="mb-2 flex items-center justify-between text-xs font-semibold uppercase tracking-wide text-slate-500">
                            <span>Exporting</span>
                            <span>{exportProgress}%</span>
                          </div>
                          <div className="h-3 overflow-hidden rounded-full bg-slate-200">
                            <div
                              className={`h-full rounded-full transition-all duration-200 ${isMalt ? "bg-[#C8A44D]" : "bg-red-500"}`}
                              style={{ width: `${exportProgress}%` }}
                            />
                          </div>
                        </div>
                      ) : null}

                      {exportError ? (
                        <div className="mt-5 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-sm font-semibold text-red-700">
                          {exportError}
                        </div>
                      ) : null}

                      <div className="mt-6 flex justify-end gap-2">
                        <button
                          type="button"
                          onClick={closeDataTableExportModal}
                          disabled={exportingDataTables}
                          className="rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-700 transition-colors hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          Cancel
                        </button>
                        <button
                          type="button"
                          onClick={handleDataTableExport}
                          disabled={exportingDataTables}
                          className={`rounded-xl border px-4 py-2 text-sm font-semibold transition-colors ${solidAccentButtonClass} disabled:cursor-not-allowed disabled:opacity-50`}
                        >
                          Continue to Export
                        </button>
                      </div>
                    </div>
                  </div>
                ) : null}
              </section>
            ) : null}

            {!isOverview && !selectedPageId && !isTables && !isCustomTable && !isExportReport && !isVerbatimsTopics ? (
              <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {loadingPages
                  ? Array.from({ length: 6 }).map((_, idx) => (
                      <div key={idx} className="morphism-card rounded-2xl p-5">
                        <div className="h-4 w-36 rounded bg-white/20" />
                        <div className="mt-3 h-3 w-full rounded bg-white/10" />
                      </div>
                    ))
                  : pages.map((page) => (
                      <article key={page.id} className="morphism-card rounded-2xl p-5">
                        <h3 className="text-lg font-semibold text-slate-900">{page.title}</h3>
                        <p className="mt-2 text-sm text-slate-700">{page.description}</p>
                        <p className="mt-3 text-sm text-sky-700">{page.questionCount} questionnaire items</p>
                      </article>
                    ))}
              </section>
            ) : null}

            <section className="flex items-center justify-between pt-2">
              <button
                type="button"
                onClick={() => previousNav && navigate(previousNav.path)}
                disabled={!previousNav}
                className="rounded-xl border border-slate-400/45 bg-white/25 px-4 py-2 text-sm font-semibold text-slate-700 transition-colors hover:border-sky-400 hover:bg-sky-200/50 hover:text-sky-800 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {previousNav ? `Back: ${previousNav.title}` : "Back"}
              </button>
              <button
                type="button"
                onClick={() => nextNav && navigate(nextNav.path)}
                disabled={!nextNav}
                className={`rounded-xl border px-4 py-2 text-sm font-semibold transition-colors ${solidAccentButtonClass} disabled:cursor-not-allowed disabled:opacity-40`}
              >
                {nextNav ? `Next: ${nextNav.title}` : "Next"}
              </button>
            </section>
            </div>
          </main>
        </SidebarInset>
      </SidebarProvider>
    </motion.div>
  );
}
