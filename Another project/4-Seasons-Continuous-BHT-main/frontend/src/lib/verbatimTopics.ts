export const VERBATIMS_TOPICS_ROUTE_SEGMENT = "verbatims-topics";

export type VerbatimQuestionOption = {
  id: string;
  questionCode: string;
  label: string;
  groupByBrand?: boolean;
};

const CATEGORY_CODE_PREFIX_BY_SLUG: Record<string, string> = {
  "breakfast-cereals": "BC",
  noodles: "N",
  toothpaste: "TP",
  bleach: "BL",
  "wet-hair": "WH",
  "dry-hair": "DH",
  "condiment-mixes": "CM",
  malt: "ML",
  snacks: "SK",
  "edible-oil": "EO",
  "toilet-cleaner": "TC",
};

const CATEGORY_SPECIFIC_VERBATIMS: Record<string, VerbatimQuestionOption[]> = {
  noodles: [
    { id: "qfh2", questionCode: "N_QFH2", label: "Campaign Check Key Messages" },
    { id: "qfs2", questionCode: "N_QFS2", label: "School Sampling Key Messages" },
    { id: "qfsb2", questionCode: "N_QFSB2", label: "Pick Ur Vibe Key Messages" },
    { id: "qfw2", questionCode: "N_QFW2", label: "Naija Pepper Key Messages" },
  ],
  snacks: [
    { id: "sk-a4", questionCode: "SK_A4", label: "Advert Recall Memory" },
    { id: "sk-a7", questionCode: "SK_A7", label: "What People Like About It" },
  ],
  toothpaste: [
    { id: "tp-a3", questionCode: "TP_A3", label: "What People Like About The Advert" },
    { id: "tp-a4", questionCode: "TP_A4", label: "Advert Recall Memory" },
  ],
};

export function getVerbatimQuestionOptions(slug: string | null | undefined): VerbatimQuestionOption[] {
  const normalizedSlug = String(slug || "").trim().toLowerCase();
  const prefix = CATEGORY_CODE_PREFIX_BY_SLUG[normalizedSlug];
  if (!prefix) return [];

  return [
    {
      id: "bau6d",
      questionCode: `${prefix}_BAU6D`,
      label: "Primary reason for choosing this brand over other brands.",
      groupByBrand: true,
    },
    ...(CATEGORY_SPECIFIC_VERBATIMS[normalizedSlug] || []),
  ];
}

export function hasVerbatimQuestionOptions(slug: string | null | undefined): boolean {
  return getVerbatimQuestionOptions(slug).length > 0;
}
