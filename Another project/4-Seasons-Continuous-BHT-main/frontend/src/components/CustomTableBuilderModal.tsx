import { useEffect, useMemo, useRef, useState } from "react";
import { Loader2, Wand2 } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { isMaltCategorySlug } from "@/data/categories";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type BreakType = "top" | "side";
type AnalysisOption = "significance" | "chi_square";
type DisplayModeId = "row_pct" | "counts" | "column_pct" | "total_pct";

type SectionDef = {
  id: string;
  title: string;
};

type PageQuestionLight = {
  question: string;
  questionLabel: string;
  month?: string;
};

type GroupedQuestion = {
  id: string;
  label: string;
  grouped: boolean;
  questionCodes: string[];
  sourceQuestionLabels?: string[];
  months?: string[];
};

type MonthValueSpec = {
  id: string;
  label: string;
  months: string[];
};

type CatalogQuestion = {
  question: string;
  questionLabel: string;
  questionCodes: string[];
  sourceQuestionLabels?: string[];
};

type PageDataResponse = {
  questions?: Array<PageQuestionLight | GroupedQuestion>;
};

type FiltersResponse = {
  filters?: Record<string, string[]>;
};

export type CustomTableFormatOptions = {
  showPercentSign: boolean;
  useDecimalPlaces: boolean;
  decimalPlaces: number;
};

export type CustomTableSelectionPayload = {
  generatedAt: string;
  selectedDisplayModeId: DisplayModeId;
  selectedAnalysis: AnalysisOption[];
  formatOptions: CustomTableFormatOptions;
  selectedQuestionIdsByBreak: Record<BreakType, Record<string, string[]>>;
  selectedQuestionsByBreak: Record<BreakType, Array<{
    id: string;
    label: string;
    sectionId: string;
    sectionTitle: string;
    questionCodes: string[];
    sourceQuestionLabels?: string[];
    months?: string[];
    monthValues?: MonthValueSpec[];
  }>>;
};

const DEFAULT_API_BASE =
  typeof window !== "undefined" && ["localhost", "127.0.0.1"].includes(window.location.hostname)
    ? "http://localhost:4000"
    : "";
const API_BASE = (import.meta.env.VITE_API_BASE_URL || DEFAULT_API_BASE).replace(/\/+$/, "");

const BREAK_BUTTONS: Array<{ id: BreakType; label: string; hint: string }> = [
  { id: "top", label: "Top Break", hint: "Columns grouping" },
  { id: "side", label: "Side Break", hint: "Rows grouping" },
];

const ANALYSIS_OPTIONS: Array<{ id: AnalysisOption; label: string }> = [
  { id: "significance", label: "Significance Test" },
  { id: "chi_square", label: "Chi-Square Test" },
];

const DISPLAY_MODES: Array<{ id: DisplayModeId; title: string; description: string }> = [
  {
    id: "column_pct",
    title: "Column %",
    description: "Percentage within each top-break category",
  },
  {
    id: "counts",
    title: "Counts",
    description: "Raw respondent counts",
  },
  {
    id: "row_pct",
    title: "Row %",
    description: "Percentage within each side-break category",
  },
  {
    id: "total_pct",
    title: "Total %",
    description: "Share of all included interviews",
  },
];
const DEFAULT_DISPLAY_MODE_ID: DisplayModeId = "column_pct";

const DECIMAL_PLACE_OPTIONS = [1, 2, 3, 4];
const DEFAULT_FORMAT_OPTIONS: CustomTableFormatOptions = {
  showPercentSign: true,
  useDecimalPlaces: false,
  decimalPlaces: 1,
};

const EMPTY_BY_BREAK: Record<BreakType, Record<string, string[]>> = {
  top: {},
  side: {},
};

const EMPTY_SECTION_BY_BREAK: Record<BreakType, string> = {
  top: "",
  side: "",
};

const BREAK_LABEL: Record<BreakType, string> = {
  top: "Top Break",
  side: "Side Break",
};

const OVERALL_SECTION_ID = "overall";
const OVERALL_QUESTION_ID = "__overall__";
const OVERALL_QUESTION_CODE = "__overall__";
const OVERALL_SECTION: SectionDef = {
  id: OVERALL_SECTION_ID,
  title: "Overall",
};
const OVERALL_QUESTION: GroupedQuestion = {
  id: OVERALL_QUESTION_ID,
  label: "Overall",
  grouped: false,
  questionCodes: [OVERALL_QUESTION_CODE],
  sourceQuestionLabels: ["Overall"],
};

const OVERVIEW_QUESTION_FIELDS: Array<{ key: string; label: string }> = [
  { key: "Region", label: "Region" },
  { key: "D3", label: "Income" },
  { key: "Gender", label: "Gender" },
  { key: "Age", label: "Age" },
  { key: "SEC", label: "SEC" },
  { key: "Week", label: "Week" },
];
const CLASSIFIED_AD_CHANNELS_QUESTION_CODE = "__classified_ad_channels__";
const CLASSIFIED_AD_CHANNELS_QUESTION: GroupedQuestion = {
  id: "awareness_usage__classified_ad_channels",
  label: "Classified Ad Channels",
  grouped: false,
  questionCodes: [CLASSIFIED_AD_CHANNELS_QUESTION_CODE],
  sourceQuestionLabels: ["Classified Ad Channels"],
};

function normalizeText(value: string) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .trim();
}

function extractSurveyCode(questionCode: string, questionLabel: string) {
  const code = normalizeText(questionCode);
  const label = normalizeText(questionLabel);
  const codeParts = code.split("_");
  const codeMatch = codeParts.find((part) => /^(?:BAU\d+[A-Z]?|PB\d+[A-Z]?|QC\d+[A-Z]?|FQ\d+[A-Z]?|A\d+|QBI)$/i.test(part));
  if (codeMatch) return codeMatch.toUpperCase();

  const labelMatch = label.match(/\b[A-Z]+_(BAU\d+[A-Z]?|PB\d+[A-Z]?|QC\d+[A-Z]?|A\d+)\b/i);
  if (labelMatch?.[1]) return labelMatch[1].toUpperCase();

  if (/\bQBI\b/i.test(label)) return "QBI";
  return "";
}

function extractGroupSurveyCode(questionCodes: string[], questionLabel: string) {
  const candidates = [...questionCodes, questionLabel];
  for (const candidate of candidates) {
    const surveyCode = extractSurveyCode(candidate, questionLabel);
    if (surveyCode) return surveyCode.toUpperCase();
  }
  return "";
}

function shouldGroupBySurveyCode(sectionId: string, surveyCode: string) {
  if (!surveyCode) return false;
  if (sectionId === "brand-imagery" || sectionId === "screener-demographics") return false;
  return /^(?:BAU\d+[A-Z]?|PB\d+[A-Z]?|QC\d+[A-Z]?|FQ\d+[A-Z]?|A\d+)$/i.test(surveyCode);
}

function stripLeadingQuestionCode(label: string, surveyCode: string) {
  let cleaned = normalizeText(label);
  const escapedSurveyCode = surveyCode.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const patterns = [
    new RegExp(`^\\d+\\.\\s*[A-Za-z0-9]+_${escapedSurveyCode}(?:_\\d+)*\\.?\\s*`, "i"),
    new RegExp(`^\\d+\\.\\s*${escapedSurveyCode}(?:_\\d+)*\\.?\\s*`, "i"),
    new RegExp(`^[A-Za-z0-9]+_${escapedSurveyCode}(?:_\\d+)*\\.?\\s*`, "i"),
    new RegExp(`^${escapedSurveyCode}(?:_\\d+)*\\.?\\s*`, "i"),
  ];

  patterns.forEach((pattern) => {
    cleaned = cleaned.replace(pattern, "").trim();
  });

  return cleaned.replace(/^\d+\.\s*/, "").trim();
}

function getTopLevelParenRanges(value: string) {
  const ranges: Array<{ start: number; end: number }> = [];
  let depth = 0;
  let start = -1;

  for (let i = 0; i < value.length; i += 1) {
    const char = value[i];
    if (char === "(") {
      if (depth === 0) start = i;
      depth += 1;
      continue;
    }
    if (char === ")" && depth > 0) {
      depth -= 1;
      if (depth === 0 && start >= 0) {
        ranges.push({ start, end: i });
        start = -1;
      }
    }
  }

  return ranges;
}

function stripTrailingQuestionVariant(label: string) {
  let cleaned = normalizeText(label);
  cleaned = cleaned.replace(/\s*\[[^\]]*other\s*\(specify\)\]\s*$/i, "").trim();
  const topLevelParenRanges = getTopLevelParenRanges(cleaned);
  const trailingRange = topLevelParenRanges[topLevelParenRanges.length - 1];
  if (trailingRange && trailingRange.end === cleaned.length - 1) {
    cleaned = cleaned.slice(0, trailingRange.start).trim();
  }
  cleaned = cleaned.replace(/\s*\[[^\]]+\]\s*$/i, "").trim();
  return cleaned;
}

function cleanQuestionLabel(sectionId: string, questionCode: string, questionLabel: string) {
  const label = normalizeText(questionLabel || questionCode);
  const surveyCode = extractSurveyCode(questionCode, label);

  if (sectionId === "brand-imagery") {
    const statement = label.match(/^\(([^)]+)\)/)?.[1]?.trim();
    if (statement) return `QBI. ${statement}`;
  }

  let cleaned = label
    .replace(/\{0\}/g, "the brand")
    .replace(/\{1\}/g, "")
    .replace(/\s+/g, " ")
    .trim();
  const topLevelParenRanges = getTopLevelParenRanges(cleaned);
  if (topLevelParenRanges[0]?.start === 0) {
    cleaned = cleaned.slice(topLevelParenRanges[0].end + 1).trim();
  }
  cleaned = stripLeadingQuestionCode(cleaned, surveyCode);
  cleaned = stripTrailingQuestionVariant(cleaned);
  cleaned = cleaned.replace(/^\d+\.\s*/, "").trim();

  if (!cleaned && surveyCode) return surveyCode;
  return cleaned;
}

function formatGroupedQuestionLabel(sectionId: string, questionCodes: string[], questionLabel: string) {
  const surveyCode = extractGroupSurveyCode(questionCodes, questionLabel);
  const cleaned = cleanQuestionLabel(sectionId, questionCodes[0] || "", questionLabel);

  if (!cleaned && surveyCode) return surveyCode;
  if (!shouldGroupBySurveyCode(sectionId, surveyCode)) return cleaned;

  const escapedSurveyCode = surveyCode.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const withoutPrefix = cleaned.replace(new RegExp(`^${escapedSurveyCode}\\.?\\s*`, "i"), "").trim();
  return withoutPrefix ? `${surveyCode}. ${withoutPrefix}` : surveyCode;
}

function extractBrandImageryAttributeLabel(questionCode: string, questionLabel: string) {
  const cleaned = formatGroupedQuestionLabel("brand-imagery", [questionCode], questionLabel)
    .replace(/^QBI\.?\s*/i, "")
    .trim();
  return cleaned;
}

function getSurveyCodeSortParts(surveyCode: string) {
  const normalized = normalizeText(surveyCode).toUpperCase();
  if (!normalized) return null;
  if (normalized === "QBI") {
    return {
      familyRank: 4,
      questionNumber: 0,
      suffixRank: 0,
      normalized,
    };
  }

  const match = normalized.match(/^(BAU|PB|QC|FQ|A)(\d+)([A-Z]?)$/i);
  if (!match) return null;

  const family = match[1].toUpperCase();
  const familyRankMap: Record<string, number> = {
    BAU: 1,
    PB: 2,
    QC: 3,
    FQ: 3,
    A: 4,
  };

  return {
    familyRank: familyRankMap[family] ?? 99,
    questionNumber: Number(match[2] || 0),
    suffixRank: match[3] ? match[3].toUpperCase().charCodeAt(0) - 64 : 0,
    normalized,
  };
}

function compareQuestionOrder(
  left: { order: number; questionCodes: string[]; label: string },
  right: { order: number; questionCodes: string[]; label: string },
) {
  const leftCode = extractGroupSurveyCode(left.questionCodes, left.label);
  const rightCode = extractGroupSurveyCode(right.questionCodes, right.label);
  const leftParts = getSurveyCodeSortParts(leftCode);
  const rightParts = getSurveyCodeSortParts(rightCode);

  if (leftParts && rightParts) {
    if (leftParts.familyRank !== rightParts.familyRank) return leftParts.familyRank - rightParts.familyRank;
    if (leftParts.questionNumber !== rightParts.questionNumber) return leftParts.questionNumber - rightParts.questionNumber;
    if (leftParts.suffixRank !== rightParts.suffixRank) return leftParts.suffixRank - rightParts.suffixRank;
  } else if (leftParts || rightParts) {
    return leftParts ? -1 : 1;
  }

  if (left.order !== right.order) return left.order - right.order;
  return left.label.localeCompare(right.label, undefined, { numeric: true, sensitivity: "base" });
}

function isPurchaseBehaviorQuestion(questionCodes: string[], questionLabel: string) {
  const surveyCode = extractGroupSurveyCode(questionCodes, questionLabel);
  return /^(?:BAU9B?|BAU10)$/i.test(surveyCode);
}

function shouldExcludeFromAwareness(questionCodes: string[], questionLabel: string) {
  const surveyCode = extractGroupSurveyCode(questionCodes, questionLabel);
  return isPurchaseBehaviorQuestion(questionCodes, questionLabel) || /^QFS1$/i.test(surveyCode);
}

function shouldExcludeFromCustomTableBreakPicker(questionCodes: string[], questionLabel: string) {
  const surveyCode = extractGroupSurveyCode(questionCodes, questionLabel);
  if (/^BAU6D$/i.test(surveyCode)) return true;
  if (/^QFW2$/i.test(surveyCode) || /^QFS2$/i.test(surveyCode)) return true;
  const label = normalizeText(questionLabel);
  if (/\bQFW2\b/i.test(label) && /key message/i.test(label)) return true;
  if (/\bQFS2\b/i.test(label) && /key message/i.test(label)) return true;
  return false;
}

function shouldExcludeFromCustomTableQuestions(questionCodes: string[], questionLabel: string) {
  const surveyCode = extractGroupSurveyCode(questionCodes, questionLabel);
  return /^BAU6D$/i.test(surveyCode);
}

function selectionIdForLabel(sectionId: string, label: string) {
  const slug = normalizeText(label)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return `${sectionId}__${slug || "question"}`;
}

function questionRegistryKey(sectionId: string, questionId: string) {
  return `${sectionId}::${questionId}`;
}

function selectionToGroupedQuestion(
  selection: {
    id: string;
    label: string;
    questionCodes: string[];
    sourceQuestionLabels?: string[];
    months?: string[];
  },
): GroupedQuestion {
  return {
    id: selection.id,
    label: selection.label,
    grouped: Array.isArray(selection.questionCodes) && selection.questionCodes.length > 1,
    questionCodes: Array.isArray(selection.questionCodes) ? [...selection.questionCodes] : [],
    sourceQuestionLabels: Array.isArray(selection.sourceQuestionLabels) ? [...selection.sourceQuestionLabels] : undefined,
    months: Array.isArray(selection.months) ? [...selection.months] : undefined,
  };
}

function toggleInList(current: string[], value: string): string[] {
  if (current.includes(value)) return current.filter((item) => item !== value);
  return [...current, value];
}

function toGroupedQuestions(rows: PageQuestionLight[], sectionId: string): GroupedQuestion[] {
  const catalogRows: CatalogQuestion[] = rows.map((row) => ({
    question: normalizeText(row.question),
    questionLabel: normalizeText(row.questionLabel || row.question),
    questionCodes: [normalizeText(row.question)],
    sourceQuestionLabels: [],
  }));
  return regroupCatalogQuestions(catalogRows, sectionId);
}

function regroupCatalogQuestions(rows: CatalogQuestion[], sectionId: string): GroupedQuestion[] {
  if (sectionId === "brand-imagery") {
    const normalizedRows = rows
      .map((row) => ({
        question: normalizeText(row.question),
        questionLabel: normalizeText(row.questionLabel || row.question),
        questionCodes:
          Array.isArray(row.questionCodes) && row.questionCodes.length > 0
            ? row.questionCodes.map((code) => normalizeText(code)).filter(Boolean)
            : [normalizeText(row.question)],
        sourceQuestionLabels: Array.isArray(row.sourceQuestionLabels)
          ? row.sourceQuestionLabels.map((label) => normalizeText(label)).filter(Boolean)
          : [],
      }))
      .filter((row) => row.question);
    const questionCodes = Array.from(
      new Set(
        normalizedRows.flatMap((row) => (Array.isArray(row.questionCodes) && row.questionCodes.length > 0 ? row.questionCodes : [row.question])),
      ),
    );
    if (!questionCodes.length) return [];
    const seenAttributes = new Set<string>();
    const attributes = normalizedRows
      .map((row) => extractBrandImageryAttributeLabel(row.question, row.questionLabel))
      .filter((attribute) => {
        if (!attribute || seenAttributes.has(attribute)) return false;
        seenAttributes.add(attribute);
        return true;
      });
    const label = attributes.length > 0
      ? `Brand Imagery (${attributes.join(", ")})`
      : "Brand Imagery (Attributes)";

    return [{
      id: selectionIdForLabel(sectionId, "Brand Imagery Group"),
      label,
      grouped: true,
      questionCodes,
    }];
  }

  const grouped = new Map<string, { order: number; items: CatalogQuestion[] }>();

  rows.forEach((row, index) => {
    const questionCode = normalizeText(row.question);
    if (!questionCode) return;

      const questionCodes =
        Array.isArray(row.questionCodes) && row.questionCodes.length > 0 ? row.questionCodes.map((code) => normalizeText(code)) : [questionCode];
      const sourceQuestionLabels = Array.isArray(row.sourceQuestionLabels)
        ? row.sourceQuestionLabels.map((label) => normalizeText(label)).filter(Boolean)
        : [];
      const label = formatGroupedQuestionLabel(sectionId, questionCodes, row.questionLabel || row.question);
    const surveyCode = extractGroupSurveyCode(questionCodes, row.questionLabel || row.question);
    const key = shouldGroupBySurveyCode(sectionId, surveyCode)
      ? `${sectionId}::code::${surveyCode}`
      : `${sectionId}::label::${label.toLowerCase()}`;
    if (!grouped.has(key)) {
      grouped.set(key, { order: index, items: [] });
    }
      grouped.get(key)?.items.push({
        question: questionCode,
        questionLabel: normalizeText(row.questionLabel || row.question),
        questionCodes,
        sourceQuestionLabels,
      });
  });

  return Array.from(grouped.entries())
    .map(([key, group]) => {
      const first = group.items[0];
      const questionCodes = Array.from(
        new Set(
          group.items.flatMap((item) => (Array.isArray(item.questionCodes) && item.questionCodes.length > 0 ? item.questionCodes : [item.question])),
        ),
      );
      const sourceQuestionLabels = questionCodes.map((questionCode) => {
        const match = group.items.find((item) =>
          (Array.isArray(item.questionCodes) && item.questionCodes.length > 0 ? item.questionCodes : [item.question])
            .map((code) => normalizeText(code))
            .includes(normalizeText(questionCode)),
        );
        const matchedCodes = Array.isArray(match?.questionCodes) && match.questionCodes.length > 0 ? match.questionCodes : [match?.question || ""];
        const matchedIndex = matchedCodes.map((code) => normalizeText(code)).indexOf(normalizeText(questionCode));
        return normalizeText(match?.sourceQuestionLabels?.[matchedIndex] || match?.questionLabel || questionCode);
      });
      const isGrouped = group.items.length > 1 || questionCodes.length > 1;
      const normalizedLabel = formatGroupedQuestionLabel(sectionId, questionCodes, first.questionLabel || first.question);
      return {
        order: group.order,
        id: isGrouped ? selectionIdForLabel(sectionId, normalizedLabel) : first.question,
        label: normalizeText(normalizedLabel) || first.question,
        grouped: isGrouped,
        questionCodes,
        sourceQuestionLabels,
      };
    })
    .filter((question) => !shouldExcludeFromCustomTableQuestions(question.questionCodes, question.label))
    .sort(compareQuestionOrder)
    .map(({ order: _order, ...question }) => question);
}

async function parseResponseError(response: Response): Promise<string> {
  const bodyText = await response.text();
  if (!bodyText) return `Request failed (${response.status})`;
  if (bodyText.trim().startsWith("<!DOCTYPE")) return `Request failed (${response.status})`;
  try {
    const parsed = JSON.parse(bodyText) as { error?: string };
    if (parsed?.error) return parsed.error;
  } catch (_err) {}
  return bodyText.slice(0, 160);
}

async function fetchSectionQuestions(slug: string, sectionId: string, signal: AbortSignal, months: string[] = []): Promise<GroupedQuestion[]> {
  if (sectionId === "screener-demographics") {
    const response = await fetch(`${API_BASE}/api/filters/${slug}`, {
      method: "GET",
      signal,
    });
    if (!response.ok) {
      const message = await parseResponseError(response);
      throw new Error(message);
    }
    const payload = (await response.json()) as FiltersResponse;
    const filters = payload.filters || {};
    return OVERVIEW_QUESTION_FIELDS.filter((field) => Array.isArray(filters[field.key]))
      .map((field) => ({
        id: `overview_${field.key.toLowerCase()}`,
        label: field.label,
        grouped: false,
        questionCodes: [field.key],
      }));
  }

  if (sectionId === "purchase-behavior") {
    const response = await fetch(`${API_BASE}/api/section-questions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        slug,
        pageId: "awareness-usage",
        months,
      }),
      signal,
    });
    if (!response.ok) {
      const message = await parseResponseError(response);
      throw new Error(message);
    }
    const payload = (await response.json()) as PageDataResponse;
    const catalogRows = Array.isArray(payload.questions)
      ? (payload.questions as GroupedQuestion[])
          .filter((item) => isPurchaseBehaviorQuestion(item.questionCodes, item.label))
          .map((item) => ({
            question: item.questionCodes[0] || item.id,
            questionLabel: item.label,
            questionCodes: item.questionCodes,
            sourceQuestionLabels: item.sourceQuestionLabels || [],
          }))
      : [];
    return regroupCatalogQuestions(catalogRows, sectionId);
  }

  if (sectionId === "campaign-check") {
    const response = await fetch(`${API_BASE}/api/page-data-monthly`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        slug,
        pageId: sectionId,
        months,
        questionPattern: "A1|A2|A5|do you remember seeing this advert recently",
        limitQuestions: 2000,
        limitAnswers: 1,
      }),
      signal,
    });
    if (!response.ok) {
      const message = await parseResponseError(response);
      throw new Error(message);
    }
    const payload = (await response.json()) as PageDataResponse;
    return toGroupedQuestions((payload.questions as PageQuestionLight[]) || [], sectionId);
  }

  const response = await fetch(
    `${API_BASE}/api/section-questions`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(
        {
          slug,
          pageId: sectionId,
          months,
        },
      ),
      signal,
    },
  );

  if (!response.ok) {
    const message = await parseResponseError(response);
    throw new Error(message);
  }

  const payload = (await response.json()) as PageDataResponse;
  if (!Array.isArray(payload.questions)) return [];
  let catalogRows: CatalogQuestion[];
  if (!payload.questions.every((item) => "grouped" in item && "questionCodes" in item)) {
    catalogRows = (payload.questions as PageQuestionLight[]).map((item) => ({
      question: normalizeText(item.question),
      questionLabel: normalizeText(item.questionLabel || item.question),
      questionCodes: [normalizeText(item.question)],
      sourceQuestionLabels: [],
    }));
  } else {
    catalogRows = (payload.questions as GroupedQuestion[]).map((item) => ({
      question: item.questionCodes[0] || item.id,
      questionLabel: item.label,
      questionCodes: item.questionCodes,
      sourceQuestionLabels: item.sourceQuestionLabels || [],
    }));
  }

  if (sectionId === "awareness-usage") {
    catalogRows = catalogRows.filter((item) => !shouldExcludeFromAwareness(item.questionCodes, item.questionLabel));
  }

  const groupedQuestions = regroupCatalogQuestions(catalogRows, sectionId);
  if (sectionId === "awareness-usage" && !groupedQuestions.some((question) => question.id === CLASSIFIED_AD_CHANNELS_QUESTION.id)) {
    return [...groupedQuestions, CLASSIFIED_AD_CHANNELS_QUESTION];
  }
  return groupedQuestions;
}

type Props = {
  slug: string;
  sections: SectionDef[];
  monthOptions?: string[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  generating?: boolean;
  initialSelection?: CustomTableSelectionPayload | null;
  onGenerate: (payload: CustomTableSelectionPayload) => void | Promise<void>;
};

export function CustomTableBuilderModal({
  slug,
  sections,
  monthOptions = [],
  open,
  onOpenChange,
  onGenerate,
  generating = false,
  initialSelection = null,
}: Props) {
  const isMalt = isMaltCategorySlug(slug);
  const activeBreakButtonClass = isMalt
    ? "border-[#C8A44D] bg-[linear-gradient(135deg,#d4b15a_0%,#b89333_100%)] text-slate-950 shadow-[0_10px_24px_rgba(200,164,77,0.35)]"
    : "border-red-500 bg-[linear-gradient(135deg,#ef4444_0%,#dc2626_100%)] text-white shadow-[0_10px_24px_rgba(220,38,38,0.35)]";
  const activeBreakHintClass = isMalt ? "text-[#fff7de]" : "text-red-100";
  const activeSectionClass = isMalt ? "border-[#d9be7c] bg-[#fbf6e7]" : "border-red-300 bg-red-50";
  const activeDisplayModeClass = isMalt
    ? "border-[#C8A44D] bg-[#fbf6e7] shadow-[0_8px_18px_rgba(200,164,77,0.16)]"
    : "border-red-400 bg-red-50 shadow-[0_8px_18px_rgba(220,38,38,0.16)]";
  const generateButtonClass = isMalt
    ? "inline-flex items-center gap-2 rounded-xl border border-[#C8A44D] bg-[#C8A44D] px-4 py-2 text-sm font-semibold text-slate-950 transition-colors hover:bg-[#b89333] disabled:cursor-not-allowed disabled:border-slate-300 disabled:bg-slate-300 disabled:text-slate-600"
    : "inline-flex items-center gap-2 rounded-xl border border-red-500 bg-red-500 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-red-600 disabled:cursor-not-allowed disabled:border-slate-300 disabled:bg-slate-300 disabled:text-slate-600";
  const [activeBreak, setActiveBreak] = useState<BreakType>("top");
  const [activeSectionByBreak, setActiveSectionByBreak] = useState<Record<BreakType, string>>(EMPTY_SECTION_BY_BREAK);
  const [selectedQuestionIdsByBreak, setSelectedQuestionIdsByBreak] = useState<Record<BreakType, Record<string, string[]>>>(EMPTY_BY_BREAK);
  const [selectedAnalysis, setSelectedAnalysis] = useState<AnalysisOption[]>([]);
  const [selectedDisplayModeId, setSelectedDisplayModeId] = useState<DisplayModeId>(DEFAULT_DISPLAY_MODE_ID);
  const [showPercentSign, setShowPercentSign] = useState<boolean>(DEFAULT_FORMAT_OPTIONS.showPercentSign);
  const [useDecimalPlaces, setUseDecimalPlaces] = useState<boolean>(DEFAULT_FORMAT_OPTIONS.useDecimalPlaces);
  const [decimalPlaces, setDecimalPlaces] = useState<number>(DEFAULT_FORMAT_OPTIONS.decimalPlaces);
  const [questionsBySection, setQuestionsBySection] = useState<Record<string, GroupedQuestion[]>>({});
  const [loadingSectionIds, setLoadingSectionIds] = useState<string[]>([]);
  const loadingSectionIdsRef = useRef<string[]>([]);
  const [loadingError, setLoadingError] = useState<string>("");
  const selectedQuestionIdsByBreakRef = useRef<Record<BreakType, Record<string, string[]>>>(EMPTY_BY_BREAK);
  const selectedAnalysisRef = useRef<AnalysisOption[]>([]);
  const selectedDisplayModeIdRef = useRef<DisplayModeId>(DEFAULT_DISPLAY_MODE_ID);
  const showPercentSignRef = useRef<boolean>(DEFAULT_FORMAT_OPTIONS.showPercentSign);
  const useDecimalPlacesRef = useRef<boolean>(DEFAULT_FORMAT_OPTIONS.useDecimalPlaces);
  const decimalPlacesRef = useRef<number>(DEFAULT_FORMAT_OPTIONS.decimalPlaces);
  const questionRegistryRef = useRef(new Map<string, GroupedQuestion>());
  const hydratedSelectionKeyRef = useRef("");

  const sectionsByBreak = useMemo(
    () => ({
      top: [OVERALL_SECTION, ...sections.filter((section) => section.id !== OVERALL_SECTION_ID)],
      side: sections.filter((section) => section.id !== OVERALL_SECTION_ID),
    }),
    [sections],
  );

  const firstSectionId = sections[0]?.id || "";
  const firstSectionIdByBreak = {
    top: sectionsByBreak.top[0]?.id || "",
    side: sectionsByBreak.side[0]?.id || "",
  };
  const sectionIdSetByBreak = useMemo(
    () => ({
      top: new Set(sectionsByBreak.top.map((section) => section.id)),
      side: new Set(sectionsByBreak.side.map((section) => section.id)),
    }),
    [sectionsByBreak],
  );
  const sortedMonthOptions = useMemo(
    () => [...monthOptions].filter(Boolean).sort((a, b) => (a > b ? -1 : a < b ? 1 : 0)),
    [monthOptions],
  );
  const sectionKey = useMemo(() => sections.map((section) => section.id).join("|"), [sections]);
  const monthKey = useMemo(() => sortedMonthOptions.join("|"), [sortedMonthOptions]);

  useEffect(() => {
    if (!open) return;
    const selectionKey = `${slug}::${initialSelection?.generatedAt || "draft"}`;
    if (hydratedSelectionKeyRef.current === selectionKey) return;
    hydratedSelectionKeyRef.current = selectionKey;

    const selectedIds = initialSelection?.selectedQuestionIdsByBreak
      ? {
          top: { ...(initialSelection.selectedQuestionIdsByBreak.top || {}) },
          side: { ...(initialSelection.selectedQuestionIdsByBreak.side || {}) },
        }
      : { top: {}, side: {} };
    const analysis = initialSelection?.selectedAnalysis || [];
    const displayMode = initialSelection?.selectedDisplayModeId || DEFAULT_DISPLAY_MODE_ID;
    const formatOptions = {
      ...DEFAULT_FORMAT_OPTIONS,
      ...(initialSelection?.formatOptions || {}),
    };
    const firstSelectedTopSection = Object.keys(selectedIds.top || {}).find((sectionId) => (selectedIds.top?.[sectionId] || []).length > 0);
    const firstSelectedSideSection = Object.keys(selectedIds.side || {}).find((sectionId) => (selectedIds.side?.[sectionId] || []).length > 0);
    const nextActiveSections = {
      top: firstSelectedTopSection && sectionIdSetByBreak.top.has(firstSelectedTopSection)
        ? firstSelectedTopSection
        : firstSectionIdByBreak.top,
      side: firstSelectedSideSection && sectionIdSetByBreak.side.has(firstSelectedSideSection)
        ? firstSelectedSideSection
        : firstSectionIdByBreak.side,
    };

    selectedQuestionIdsByBreakRef.current = selectedIds;
    selectedAnalysisRef.current = analysis;
    selectedDisplayModeIdRef.current = displayMode;
    showPercentSignRef.current = Boolean(formatOptions.showPercentSign);
    useDecimalPlacesRef.current = Boolean(formatOptions.useDecimalPlaces);
    decimalPlacesRef.current = DECIMAL_PLACE_OPTIONS.includes(Number(formatOptions.decimalPlaces))
      ? Number(formatOptions.decimalPlaces)
      : DEFAULT_FORMAT_OPTIONS.decimalPlaces;
    const seededRegistry = new Map<string, GroupedQuestion>(questionRegistryRef.current);
    seededRegistry.set(questionRegistryKey(OVERALL_SECTION_ID, OVERALL_QUESTION_ID), OVERALL_QUESTION);
    (["top", "side"] as BreakType[]).forEach((breakId) => {
      (initialSelection?.selectedQuestionsByBreak?.[breakId] || []).forEach((selection) => {
        seededRegistry.set(questionRegistryKey(selection.sectionId, selection.id), selectionToGroupedQuestion(selection));
      });
    });
    questionRegistryRef.current = seededRegistry;

    setSelectedQuestionIdsByBreak(selectedIds);
    setSelectedAnalysis(analysis);
    setSelectedDisplayModeId(displayMode);
    setShowPercentSign(Boolean(formatOptions.showPercentSign));
    setUseDecimalPlaces(Boolean(formatOptions.useDecimalPlaces));
    setDecimalPlaces(
      DECIMAL_PLACE_OPTIONS.includes(Number(formatOptions.decimalPlaces))
        ? Number(formatOptions.decimalPlaces)
        : DEFAULT_FORMAT_OPTIONS.decimalPlaces,
    );
    if (firstSectionIdByBreak.top || firstSectionIdByBreak.side) {
      setActiveSectionByBreak(nextActiveSections);
    }
  }, [open, initialSelection, sectionIdSetByBreak, firstSectionIdByBreak.top, firstSectionIdByBreak.side]);

  useEffect(() => {
    if (!firstSectionIdByBreak.top && !firstSectionIdByBreak.side) {
      setActiveSectionByBreak(EMPTY_SECTION_BY_BREAK);
      return;
    }

    setActiveSectionByBreak((prev) => ({
      top: prev.top && sectionIdSetByBreak.top.has(prev.top) ? prev.top : firstSectionIdByBreak.top,
      side: prev.side && sectionIdSetByBreak.side.has(prev.side) ? prev.side : firstSectionIdByBreak.side,
    }));
  }, [firstSectionIdByBreak.top, firstSectionIdByBreak.side, sectionIdSetByBreak]);

  useEffect(() => {
    setQuestionsBySection({});
    setLoadingSectionIds([]);
    setLoadingError("");
  }, [slug, sectionKey]);

  useEffect(() => {
    loadingSectionIdsRef.current = loadingSectionIds;
  }, [loadingSectionIds]);

  useEffect(() => {
    selectedQuestionIdsByBreakRef.current = selectedQuestionIdsByBreak;
  }, [selectedQuestionIdsByBreak]);

  useEffect(() => {
    selectedAnalysisRef.current = selectedAnalysis;
  }, [selectedAnalysis]);

  useEffect(() => {
    selectedDisplayModeIdRef.current = selectedDisplayModeId;
  }, [selectedDisplayModeId]);

  useEffect(() => {
    showPercentSignRef.current = showPercentSign;
  }, [showPercentSign]);

  useEffect(() => {
    useDecimalPlacesRef.current = useDecimalPlaces;
  }, [useDecimalPlaces]);

  useEffect(() => {
    decimalPlacesRef.current = decimalPlaces;
  }, [decimalPlaces]);

  const activeSectionId = activeSectionByBreak[activeBreak] || firstSectionIdByBreak[activeBreak] || firstSectionId;
  const activeSectionQuestions =
    activeSectionId === OVERALL_SECTION_ID
      ? [OVERALL_QUESTION]
      : questionsBySection[activeSectionId] || [];
  const visibleActiveSectionQuestions = activeSectionQuestions.filter(
    (question) => !shouldExcludeFromCustomTableBreakPicker(question.questionCodes, question.label),
  );
  const activeSectionSelectedQuestionIds = selectedQuestionIdsByBreak[activeBreak][activeSectionId] || [];
  const activeSectionQuestionIds = visibleActiveSectionQuestions.map((question) => question.id);
  const activeSectionSelectedQuestionIdSet = new Set(activeSectionSelectedQuestionIds);
  const activeSectionSelectedLoadedCount = visibleActiveSectionQuestions.reduce(
    (count, question) => count + (activeSectionSelectedQuestionIdSet.has(question.id) ? 1 : 0),
    0,
  );
  const allActiveSectionQuestionsSelected =
    activeSectionQuestionIds.length > 0 && activeSectionSelectedLoadedCount === activeSectionQuestionIds.length;
  const someActiveSectionQuestionsSelected = activeSectionSelectedLoadedCount > 0 && !allActiveSectionQuestionsSelected;
  const activeSectionLoading = loadingSectionIds.includes(activeSectionId);
  const activeSectionIdRef = useRef(activeSectionId);
  const usesPercentageFormatting = selectedDisplayModeId !== "counts";

  useEffect(() => {
    activeSectionIdRef.current = activeSectionId;
  }, [activeSectionId]);

  useEffect(() => {
    setLoadingError("");
  }, [activeSectionId]);

  const selectedQuestionLabelBySectionAndId = useMemo(() => {
    const map = new Map<string, string>();
    Object.entries(questionsBySection).forEach(([sectionId, sectionQuestions]) => {
      sectionQuestions.forEach((item) => map.set(questionRegistryKey(sectionId, item.id), item.label));
    });
    return map;
  }, [questionsBySection]);

  const totalSelectedQuestions = useMemo(() => {
    return (["top", "side"] as BreakType[]).reduce((sum, breakId) => {
      const breakSelections = selectedQuestionIdsByBreak[breakId] || {};
      return (
        sum +
        Object.values(breakSelections).reduce((count, ids) => {
          return count + (Array.isArray(ids) ? ids.length : 0);
        }, 0)
      );
    }, 0);
  }, [selectedQuestionIdsByBreak]);
  const selectedQuestionCountByBreak = useMemo(() => {
    return (["top", "side"] as BreakType[]).reduce(
      (acc, breakId) => {
        const breakSelections = selectedQuestionIdsByBreak[breakId] || {};
        acc[breakId] = Object.values(breakSelections).reduce((count, ids) => count + (Array.isArray(ids) ? ids.length : 0), 0);
        return acc;
      },
      { top: 0, side: 0 } as Record<BreakType, number>,
    );
  }, [selectedQuestionIdsByBreak]);
  const hasSelectedQuestionStillLoading = useMemo(() => {
    return (["top", "side"] as BreakType[]).some((breakId) => {
      const bySection = selectedQuestionIdsByBreak[breakId] || {};
      return Object.entries(bySection).some(([sectionId, ids]) => {
        if (!Array.isArray(ids) || ids.length === 0) return false;
        return loadingSectionIds.includes(sectionId) || ids.some((id) => !questionRegistryRef.current.has(questionRegistryKey(sectionId, id)));
      });
    });
  }, [selectedQuestionIdsByBreak, loadingSectionIds, questionsBySection]);
  const canGenerate =
    selectedQuestionCountByBreak.top > 0 && selectedQuestionCountByBreak.side > 0 && !generating;

  const selectedSummaryByBreak = useMemo(() => {
    return (["top", "side"] as BreakType[]).map((breakId) => {
      const items = Object.entries(selectedQuestionIdsByBreak[breakId])
        .filter(([, ids]) => Array.isArray(ids) && ids.length > 0)
        .map(([sectionId, ids]) => {
          const section = sectionId === OVERALL_SECTION_ID ? OVERALL_SECTION : sections.find((entry) => entry.id === sectionId);
          const labels = ids.map((id) => selectedQuestionLabelBySectionAndId.get(questionRegistryKey(sectionId, id)) || id);
          return {
            sectionId,
            sectionTitle: section?.title || sectionId,
            labels,
          };
        });

      return { breakId, items };
    });
  }, [sections, selectedQuestionIdsByBreak, selectedQuestionLabelBySectionAndId]);

  useEffect(() => {
    if (!open || !slug || sections.length === 0) return;

    const controller = new AbortController();
    const sectionIds = sections.map((section) => section.id);
    questionRegistryRef.current = new Map(questionRegistryRef.current).set(
      questionRegistryKey(OVERALL_SECTION_ID, OVERALL_QUESTION_ID),
      OVERALL_QUESTION,
    );
    setQuestionsBySection((prev) => ({
      ...prev,
      [OVERALL_SECTION_ID]: [OVERALL_QUESTION],
    }));

    setLoadingError("");
    setLoadingSectionIds(sectionIds);

    sectionIds.forEach((sectionId) => {
      fetchSectionQuestions(slug, sectionId, controller.signal, sortedMonthOptions)
        .then((groupedQuestions) => {
          if (controller.signal.aborted) return;
          const nextRegistry = new Map(questionRegistryRef.current);
          groupedQuestions.forEach((question) => {
            nextRegistry.set(questionRegistryKey(sectionId, question.id), question);
          });
          questionRegistryRef.current = nextRegistry;
          setQuestionsBySection((prev) => ({ ...prev, [sectionId]: groupedQuestions }));
        })
        .catch((error: Error) => {
          if (controller.signal.aborted) return;
          if (sectionId === activeSectionIdRef.current) {
            setLoadingError(error.message || "Unable to load section questions.");
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) {
            setLoadingSectionIds((prev) => prev.filter((id) => id !== sectionId));
          }
        });
    });

    return () => controller.abort();
  }, [open, slug, sectionKey, monthKey]);

  const handleSectionSelect = (sectionId: string) => {
    setActiveSectionByBreak((prev) => ({
      ...prev,
      [activeBreak]: sectionId,
    }));
  };

  const handleQuestionToggle = (questionId: string) => {
    const current = selectedQuestionIdsByBreakRef.current;
    const byBreak = current[activeBreak] || {};
    const existing = byBreak[activeSectionId] || [];
    const next = {
      ...current,
      [activeBreak]: {
        ...byBreak,
        [activeSectionId]: toggleInList(existing, questionId),
      },
    };
    selectedQuestionIdsByBreakRef.current = next;
    setSelectedQuestionIdsByBreak(next);
  };

  const handleQuestionSelectAll = () => {
    if (!activeSectionId) return;
    const current = selectedQuestionIdsByBreakRef.current;
    const byBreak = current[activeBreak] || {};
    const next = {
      ...current,
      [activeBreak]: {
        ...byBreak,
        [activeSectionId]: allActiveSectionQuestionsSelected ? [] : activeSectionQuestionIds,
      },
    };
    selectedQuestionIdsByBreakRef.current = next;
    setSelectedQuestionIdsByBreak(next);
  };

  const handleAnalysisToggle = (optionId: AnalysisOption) => {
    const next = toggleInList(selectedAnalysisRef.current, optionId) as AnalysisOption[];
    selectedAnalysisRef.current = next;
    setSelectedAnalysis(next);
  };

  const selectionStillBlocked = (idsSnapshot: Record<BreakType, Record<string, string[]>>) =>
    (["top", "side"] as BreakType[]).some((breakId) => {
      const bySection = idsSnapshot[breakId] || {};
      return Object.entries(bySection).some(([sectionId, ids]) => {
        if (!Array.isArray(ids) || ids.length === 0) return false;
        return (
          loadingSectionIdsRef.current.includes(sectionId) ||
          ids.some((id) => !questionRegistryRef.current.has(questionRegistryKey(sectionId, id)))
        );
      });
    });

  const handleGenerate = async () => {
    if (!canGenerate) return;
    const selectedIdsSnapshot = selectedQuestionIdsByBreakRef.current;
    const waitDeadline = Date.now() + 25000;
    while (selectionStillBlocked(selectedIdsSnapshot) && Date.now() < waitDeadline) {
      await new Promise<void>((resolve) => {
        window.setTimeout(resolve, 120);
      });
    }
    const selectedAnalysisSnapshot = selectedAnalysisRef.current;
    const selectedDisplayModeSnapshot = selectedDisplayModeIdRef.current;
    const formatOptionsSnapshot: CustomTableFormatOptions = {
      showPercentSign: Boolean(showPercentSignRef.current),
      useDecimalPlaces: Boolean(useDecimalPlacesRef.current),
      decimalPlaces: DECIMAL_PLACE_OPTIONS.includes(Number(decimalPlacesRef.current))
        ? Number(decimalPlacesRef.current)
        : DEFAULT_FORMAT_OPTIONS.decimalPlaces,
    };

    setLoadingError("");
    try {
      const selectedQuestionsByBreak = (["top", "side"] as BreakType[]).reduce(
        (acc, breakId) => {
          const bySection = selectedIdsSnapshot[breakId] || {};
          const unresolved: string[] = [];
          acc[breakId] = Object.entries(bySection)
            .flatMap(([sectionId, ids]) => {
              const sectionTitle =
                sectionId === OVERALL_SECTION_ID
                  ? OVERALL_SECTION.title
                  : sections.find((entry) => entry.id === sectionId)?.title || sectionId;
              return ids
                .map((id) => {
                  const question = questionRegistryRef.current.get(questionRegistryKey(sectionId, id));
                  if (!question) {
                    unresolved.push(`${sectionTitle}: ${id}`);
                    return null;
                  }
                  return {
                    id: question.id,
                    label: question.label,
                    sectionId,
                    sectionTitle,
                    questionCodes: question.questionCodes,
                    sourceQuestionLabels: Array.isArray(question.sourceQuestionLabels) ? question.sourceQuestionLabels : undefined,
                    months: Array.isArray(question.months) ? question.months : undefined,
                  };
                })
                .filter((item): item is NonNullable<typeof item> => Boolean(item));
            });
          if (unresolved.length > 0) {
            throw new Error("Some selected questions are still loading. Please wait a moment and generate again.");
          }
          return acc;
        },
        { top: [], side: [] } as CustomTableSelectionPayload["selectedQuestionsByBreak"],
      );
      await Promise.resolve(onGenerate({
        generatedAt: new Date().toISOString(),
        selectedDisplayModeId: selectedDisplayModeSnapshot,
        selectedAnalysis: selectedAnalysisSnapshot,
        formatOptions: formatOptionsSnapshot,
        selectedQuestionIdsByBreak: selectedIdsSnapshot,
        selectedQuestionsByBreak,
      }));
      await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
      onOpenChange(false);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unable to generate custom table.";
      setLoadingError(message);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="grid h-[min(86vh,780px)] max-w-[min(1120px,96vw)] grid-rows-[auto_1fr_auto] overflow-hidden rounded-2xl border border-white/35 bg-[#d2d7df] p-0 shadow-[0_24px_60px_rgba(15,23,42,0.22)]">
        <DialogHeader className="border-b border-white/45 bg-white/28 px-5 py-4 text-left backdrop-blur-md sm:px-6">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <DialogTitle className="text-lg font-bold text-slate-900">Custom Table Builder</DialogTitle>
              <DialogDescription className="mt-0.5 text-xs text-slate-600">
                Configure sections and questions for top and side breaks.
              </DialogDescription>
            </div>
            <div className="rounded-md border border-white/50 bg-white/35 px-2.5 py-1 text-[11px] font-semibold text-slate-700">
              {totalSelectedQuestions} selected
            </div>
          </div>
        </DialogHeader>

        <div className="min-h-0 overflow-y-auto px-4 py-4 sm:px-6 sm:py-5">
          <div className="space-y-4">
          <section className="morphism-card rounded-2xl p-3">
            <div className="grid gap-2 sm:grid-cols-2">
              {BREAK_BUTTONS.map((button) => {
                const active = activeBreak === button.id;
                return (
                  <button
                    key={button.id}
                    type="button"
                    onClick={() => setActiveBreak(button.id)}
                    className={`rounded-xl border px-4 py-3 text-left transition-all ${
                      active
                        ? activeBreakButtonClass
                        : "border-slate-200 bg-slate-50 text-slate-800 hover:bg-slate-100"
                    }`}
                  >
                    <p className="text-sm font-bold">{button.label}</p>
                    <p className={`text-xs ${active ? activeBreakHintClass : "text-slate-500"}`}>{button.hint}</p>
                  </button>
                );
              })}
            </div>
          </section>

          <section className="grid gap-3 lg:grid-cols-[320px_1fr]">
            <div className="morphism-card rounded-2xl p-3">
              <div className="mb-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <p className="text-sm font-bold text-slate-900">{BREAK_LABEL[activeBreak]} Sections</p>
                    <p className="text-xs text-slate-500">Single-select (radio style)</p>
                  </div>
                </div>
              </div>
              <div className="max-h-[220px] space-y-1.5 overflow-y-auto pr-1">
                {sectionsByBreak[activeBreak].length === 0 ? (
                  <p className="text-xs text-slate-500">No sections available.</p>
                ) : (
                  sectionsByBreak[activeBreak].map((section) => {
                    const checked = activeSectionId === section.id;
                    return (
                      <label
                        key={`${activeBreak}-${section.id}`}
                        className={`flex cursor-pointer items-start gap-2 rounded-xl border px-2.5 py-2 ${
                          checked ? activeSectionClass : "border-slate-200 bg-slate-50/50 hover:bg-slate-100/80"
                        }`}
                      >
                        <Checkbox checked={checked} onCheckedChange={() => handleSectionSelect(section.id)} />
                        <span className="text-sm leading-tight text-slate-800">{section.title}</span>
                      </label>
                    );
                  })
                )}
              </div>
            </div>

            <div className="morphism-card rounded-2xl p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div>
                  <p className="text-sm font-bold text-slate-900">{BREAK_LABEL[activeBreak]} Questions</p>
                  <p className="text-xs text-slate-500">Multi-select question labels</p>
                </div>
                <div className="flex items-center gap-3">
                  <label
                    className={`flex items-center gap-2 text-xs font-medium ${
                      activeSectionLoading || visibleActiveSectionQuestions.length === 0
                        ? "cursor-not-allowed text-slate-400"
                        : "cursor-pointer text-slate-600"
                    }`}
                  >
                    <span>Select all</span>
                    <Checkbox
                      aria-label={`Select all ${BREAK_LABEL[activeBreak].toLowerCase()} questions`}
                      checked={
                        allActiveSectionQuestionsSelected ? true : someActiveSectionQuestionsSelected ? "indeterminate" : false
                      }
                      disabled={activeSectionLoading || visibleActiveSectionQuestions.length === 0}
                      onCheckedChange={handleQuestionSelectAll}
                    />
                  </label>
                  {activeSectionLoading ? <Loader2 className="h-4 w-4 animate-spin text-slate-500" /> : null}
                </div>
              </div>

              {loadingError ? (
                <div className="mb-2 rounded-lg border border-rose-200 bg-rose-50 px-2 py-1.5 text-xs text-rose-700">{loadingError}</div>
              ) : null}

              <div className="max-h-[220px] space-y-1.5 overflow-y-auto pr-1">
                {activeSectionId && visibleActiveSectionQuestions.length === 0 && !activeSectionLoading ? (
                  <p className="text-xs text-slate-500">No questions found for this section.</p>
                ) : (
                  visibleActiveSectionQuestions.map((question, index) => {
                    const checked = activeSectionSelectedQuestionIds.includes(question.id);
                    return (
                      <label
                        key={`${activeBreak}-${activeSectionId}-${question.id}`}
                        className={`flex cursor-pointer items-start gap-2 rounded-xl border px-2.5 py-2 ${
                          checked ? "border-sky-300 bg-sky-50" : "border-slate-200 bg-slate-50/50 hover:bg-slate-100/80"
                        }`}
                      >
                        <Checkbox checked={checked} onCheckedChange={() => handleQuestionToggle(question.id)} />
                        <span className="text-sm leading-tight text-slate-800">
                          <span className="mr-1 font-semibold text-slate-500">{index + 1}.</span>
                          {question.label}
                          {question.grouped ? <span className="ml-1 text-[11px] font-semibold text-sky-700">Grouped</span> : null}
                        </span>
                      </label>
                    );
                  })
                )}
              </div>
            </div>
          </section>

          <section className="morphism-card rounded-2xl p-3">
            <p className="text-sm font-bold text-slate-900">Statistical Test</p>
            <p className="mb-2 text-xs text-slate-500">Select one or both statistical checks</p>
            <div className="flex flex-wrap gap-2">
              {ANALYSIS_OPTIONS.map((option) => {
                const checked = selectedAnalysis.includes(option.id);
                return (
                  <label
                    key={option.id}
                    className={`flex cursor-pointer items-center gap-2 rounded-full border px-3 py-2 text-sm ${
                      checked ? "border-sky-300 bg-sky-50 text-sky-900" : "border-slate-200 bg-slate-50 text-slate-700"
                    }`}
                  >
                    <Checkbox checked={checked} onCheckedChange={() => handleAnalysisToggle(option.id)} />
                    <span>{option.label}</span>
                  </label>
                );
              })}
            </div>
          </section>

          <section className="morphism-card rounded-2xl p-3">
            <p className="mb-2 text-sm font-bold text-slate-900">Display mode</p>
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
              {DISPLAY_MODES.map((mode) => {
                const active = selectedDisplayModeId === mode.id;
                return (
                  <button
                    key={mode.id}
                    type="button"
                    onClick={() => {
                      selectedDisplayModeIdRef.current = mode.id;
                      setSelectedDisplayModeId(mode.id);
                    }}
                    className={`rounded-2xl border px-3 py-3 text-left transition-all ${
                      active
                        ? activeDisplayModeClass
                        : "border-slate-200 bg-slate-50/60 hover:bg-slate-100"
                    }`}
                  >
                    <p className="text-lg font-bold text-slate-900">{mode.title}</p>
                    <p className="mt-1 text-xs leading-relaxed text-slate-600">{mode.description}</p>
                  </button>
                );
              })}
            </div>
            <div className="mt-4 grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_220px]">
              <label
                className={`flex items-center gap-3 rounded-xl border px-3 py-3 text-sm transition-colors ${
                  usesPercentageFormatting
                    ? showPercentSign
                      ? "border-sky-300 bg-sky-50 text-sky-900"
                      : "border-slate-200 bg-slate-50 text-slate-700"
                    : "cursor-not-allowed border-slate-200 bg-slate-100 text-slate-400"
                }`}
              >
                <Checkbox
                  checked={showPercentSign}
                  disabled={!usesPercentageFormatting}
                  onCheckedChange={(checked) => {
                    const next = checked === true;
                    showPercentSignRef.current = next;
                    setShowPercentSign(next);
                  }}
                />
                <div>
                  <p className="font-semibold">Show % sign</p>
                  <p className="text-xs text-slate-500">Applies to percentage display modes.</p>
                </div>
              </label>

              <label
                className={`flex items-center gap-3 rounded-xl border px-3 py-3 text-sm transition-colors ${
                  usesPercentageFormatting
                    ? useDecimalPlaces
                      ? "border-sky-300 bg-sky-50 text-sky-900"
                      : "border-slate-200 bg-slate-50 text-slate-700"
                    : "cursor-not-allowed border-slate-200 bg-slate-100 text-slate-400"
                }`}
              >
                <Checkbox
                  checked={useDecimalPlaces}
                  disabled={!usesPercentageFormatting}
                  onCheckedChange={(checked) => {
                    const next = checked === true;
                    useDecimalPlacesRef.current = next;
                    setUseDecimalPlaces(next);
                  }}
                />
                <div>
                  <p className="font-semibold">Use decimal places</p>
                  <p className="text-xs text-slate-500">Enable custom precision for percentage values.</p>
                </div>
              </label>

              <div
                className={`rounded-xl border px-3 py-3 text-sm transition-colors ${
                  usesPercentageFormatting && useDecimalPlaces
                    ? "border-slate-200 bg-slate-50 text-slate-800"
                    : "border-slate-200 bg-slate-100 text-slate-400"
                }`}
              >
                <label className="block">
                  <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500">Places</span>
                  <select
                    value={decimalPlaces}
                    disabled={!usesPercentageFormatting || !useDecimalPlaces}
                    onChange={(event) => {
                      const next = Number(event.target.value || DEFAULT_FORMAT_OPTIONS.decimalPlaces);
                      decimalPlacesRef.current = next;
                      setDecimalPlaces(next);
                    }}
                    className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 outline-none transition-colors focus:border-sky-300 disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-400"
                  >
                    {DECIMAL_PLACE_OPTIONS.map((option) => (
                      <option key={option} value={option}>
                        {option}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            </div>
            {!usesPercentageFormatting ? (
              <p className="mt-2 text-xs text-slate-500">Formatting controls apply only to Row %, Column %, and Total %.</p>
            ) : null}
          </section>

          <section className="morphism-card rounded-2xl p-3">
            <p className="mb-2 text-sm font-bold text-slate-900">Selected Top Break (Columns) and Side Break (Rows) Questions</p>
            <div className="grid gap-3 lg:grid-cols-2">
              {selectedSummaryByBreak.map((summary) => (
                <div key={summary.breakId} className="rounded-xl border border-slate-200 bg-slate-50/70 p-3">
                  <p className="mb-2 text-xs font-bold uppercase tracking-wide text-slate-700">
                    {BREAK_LABEL[summary.breakId]} {summary.breakId === "top" ? "(Columns)" : "(Rows)"} Selections
                  </p>
                  {summary.items.length === 0 ? (
                    <p className="text-xs text-slate-500">No section questions selected.</p>
                  ) : (
                    <div className="space-y-2">
                      {summary.items.map((item) => (
                        <div key={`${summary.breakId}-${item.sectionId}`} className="rounded-lg border border-slate-200 bg-white p-2">
                          <p className="text-xs font-bold text-slate-800">{item.sectionTitle}</p>
                          <ol className="mt-1 space-y-0.5">
                            {item.labels.map((label, idx) => (
                              <li key={`${summary.breakId}-${item.sectionId}-${idx}`} className="text-xs text-slate-600">
                                <span className="mr-1 font-semibold text-slate-500">{idx + 1}.</span>
                                {label}
                              </li>
                            ))}
                          </ol>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </section>
          </div>
        </div>

        <div className="sticky bottom-0 z-20 flex items-center justify-between gap-3 border-t border-white/45 bg-white/35 px-4 py-3 backdrop-blur-md sm:px-6">
          <p className="text-xs text-slate-600">
            {canGenerate
              ? `${totalSelectedQuestions} question selection(s) ready.`
              : hasSelectedQuestionStillLoading
                ? "Selected question details are still loading."
              : "Select at least one Top Break question and one Side Break question to generate."}
          </p>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => onOpenChange(false)}
              className="rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-700 transition-colors hover:bg-slate-100"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleGenerate}
              disabled={!canGenerate}
              className={generateButtonClass}
            >
              {generating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wand2 className="h-4 w-4" />}
              {generating ? "Generating..." : "Generate Table"}
            </button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
