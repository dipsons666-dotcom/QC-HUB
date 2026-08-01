from __future__ import annotations

import json
import logging
import re
import tempfile
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
import numpy as np
import pyreadstat
from fastapi import HTTPException
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows

from psycopg import sql as psql

from backend.app.auth import AuthUser
from backend.app.database import db_connection
from backend.app.etl_bridge import describe_sync_failure, run_main_survey_sync_job
from backend.app.services.main_data_scope import main_case_scope_clause, main_row_scope_clause
from backend.app.settings import Settings
from backend.app.sync_lock import survey_sync_lock_wait
from survey_platform.config import load_main_survey_pipeline_config
from survey_platform.db import clear_manual_sync_override, request_manual_sync_override


logger = logging.getLogger(__name__)
BHT_OVERVIEW_CACHE_TTL_SECONDS = 60
BHT_OVERVIEW_CACHE: dict[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]], tuple[float, dict[str, Any]]] = {}
BHT_MAP_CACHE_TTL_SECONDS = 60
BHT_MAP_CACHE: dict[tuple[str, tuple[str, ...], int], tuple[float, dict[str, Any]]] = {}
BHT_OVERVIEW_CASE_MART_CHECKED_AT = 0.0
BHT_OVERVIEW_CASE_MART_CHECK_TTL_SECONDS = 300


def clear_bht_analytics_caches(settings: Settings | None = None, *, refresh_map_mart: bool = False) -> None:
    global BHT_OVERVIEW_CASE_MART_CHECKED_AT
    BHT_OVERVIEW_CACHE.clear()
    BHT_MAP_CACHE.clear()
    BHT_OVERVIEW_CASE_MART_CHECKED_AT = 0.0
    if refresh_map_mart and settings is not None:
        refresh_bht_map_mart(settings)
        BHT_OVERVIEW_CASE_MART_CHECKED_AT = time.monotonic()
        prewarm_bht_overview_cache(settings)


def _ensure_bht_overview_marts_fresh(settings: Settings) -> bool:
    if not settings.database_url:
        return True
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    (SELECT COUNT(*)::int FROM clean.main_case) AS clean_cases,
                    (SELECT COALESCE(MAX(total_case_count), 0)::int FROM mart.bht_category_kpi) AS mart_cases
                """
            )
            row = cur.fetchone() or {}
            if int(row.get("clean_cases") or 0) == int(row.get("mart_cases") or 0):
                return True
    BHT_OVERVIEW_CACHE.clear()
    return False


def _ensure_bht_overview_case_mart(settings: Settings) -> None:
    global BHT_OVERVIEW_CASE_MART_CHECKED_AT
    if not settings.database_url:
        return
    now = time.monotonic()
    if now - BHT_OVERVIEW_CASE_MART_CHECKED_AT < BHT_OVERVIEW_CASE_MART_CHECK_TTL_SECONDS:
        return
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'mart' AND table_name = 'bht_case_overview_dim'
                ) AS exists
                """
            )
            exists = bool((cur.fetchone() or {}).get("exists"))
            if exists:
                BHT_OVERVIEW_CASE_MART_CHECKED_AT = now
                return
    logger.info("BHT overview mart is missing; it will be rebuilt by startup/background refresh.")
    BHT_OVERVIEW_CASE_MART_CHECKED_AT = time.monotonic()


def prewarm_bht_overview_cache(settings: Settings) -> None:
    if not settings.database_url:
        return
    if not _bht_active_case_mart_ready(settings):
        return
    category_key = "all"
    category_meta = {"label": "All Categories", "panelCode": None}
    cache_key = (settings.main_survey_data_start_date or "", category_key, tuple(), tuple(), tuple(), tuple())
    payload = _get_bht_overview_from_case_mart(settings, category_key, category_meta, [], [], [], [])
    BHT_OVERVIEW_CACHE[cache_key] = (time.monotonic(), payload)


def _empty_bht_overview_payload(
    category_meta: dict[str, Any],
    months_sel: list[str],
    regions_sel: list[str],
    sectors_sel: list[str],
    *,
    status: str = "refreshing",
    message: str = "Refreshing active data. Try again shortly.",
) -> dict[str, Any]:
    return {
        "category": category_meta,
        "monthsAvailable": [],
        "monthsSelected": months_sel,
        "regionsSelected": regions_sel,
        "sectorsAvailable": [],
        "sectorsSelected": sectors_sel,
        "kpis": {"totalCases": 0, "categoryCases": 0, "omnibusAnswers": 0, "mediaFiles": 0},
        "statusKpis": {"totalSynced": 0, "approved": 0, "pendingApproval": 0, "cancelledRejected": 0},
        "months": [],
        "panels": [],
        "distributions": {},
        "status": status,
        "message": message,
    }


def _bht_active_case_mart_ready(settings: Settings) -> bool:
    if not settings.main_survey_data_start_date:
        return True
    if not settings.database_url:
        return False
    try:
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '5000ms'")
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'mart'
                          AND table_name = 'bht_case_overview_dim'
                          AND column_name = 'start_time'
                    ) AS has_start_time
                    """
                )
                if not bool((cur.fetchone() or {}).get("has_start_time")):
                    return False
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM mart.bht_case_overview_dim
                        WHERE NULLIF(TRIM(COALESCE(start_time, '')), '') IS NOT NULL
                    ) AS has_start_values
                    """
                )
                return bool((cur.fetchone() or {}).get("has_start_values"))
    except Exception:
        logger.warning("Unable to confirm active BHT overview mart readiness.", exc_info=True)
        return False

# Main survey SAV exports are delivered as a ZIP. Use maximum ZIP compression
# because these files are usually downloaded and shared outside the platform.
MAIN_SURVEY_ZIP_COMPRESSION_LEVEL = 9

BHT_CATEGORY_PANEL_MAP = {
    "breakfast-cereals": {"label": "Breakfast Cereals", "panelCode": "Panel_7"},
    "noodles": {"label": "Noodles", "panelCode": "Panel_1"},
    "toothpaste": {"label": "Toothpaste", "panelCode": "Panel_2"},
    "bleach": {"label": "Bleach", "panelCode": "Panel_4"},
    "wet-hair": {"label": "Wet Hair", "panelCode": "Panel_9"},
    "dry-hair": {"label": "Dry Hair", "panelCode": "Panel_10"},
    "condiment-mixes": {"label": "Condiment Mixes", "panelCode": "Panel_8"},
    "malt": {"label": "Malt", "panelCode": "Panel_11"},
    "snacks": {"label": "Snacks", "panelCode": "Panel_6"},
    "edible-oil": {"label": "Edible Oil", "panelCode": "Panel_3"},
    "toilet-cleaner": {"label": "Toilet Cleaner", "panelCode": "Panel_5"},
    "omnibus": {"label": "Omnibus", "panelCode": None},
}

BHT_PANEL_LABEL_BY_CODE = {
    str(meta["panelCode"]): str(meta["label"])
    for meta in BHT_CATEGORY_PANEL_MAP.values()
    if meta.get("panelCode")
}

BHT_CATEGORY_BAU5A_PREFIX = {
    "breakfast-cereals": "BC",
    "noodles": "N",
    "toothpaste": "TP",
    "bleach": "BL",
    "wet-hair": "WH",
    "dry-hair": "DH",
    "condiment-mixes": "CM",
    "malt": "ML",
    "snacks": "SK",
    "edible-oil": "EO",
    "toilet-cleaner": "TC",
}

VERBATIM_CATEGORY_PREFIXES = {
    "breakfast-cereals": ("Breakfast Cereals", ("BC_",), {"Breakfast Cereal"}),
    "noodles": ("Noodles", ("N_",), {"Noodles", "Flavour/Variants"}),
    "toothpaste": ("Toothpaste", ("TP_",), {"Toothpaste"}),
    "bleach": ("Bleach", ("BL_",), {"Bleach"}),
    "wet-hair": ("Wet Hair", ("WH_",), {"Wet Hair"}),
    "dry-hair": ("Dry Hair", ("DH_",), {"Dry Hair", "DH_campaign.grp1", "DH_campaign.grp2"}),
    "condiment-mixes": ("Condiment Mixes", ("CM_",), {"Condiment Mixes"}),
    "malt": ("Malt", ("ML_",), {"Malt Beverage"}),
    "snacks": ("Snacks", ("SK_",), {"Snacks products"}),
    "edible-oil": ("Edible Oil", ("EO_",), {"Edible Oil"}),
    "toilet-cleaner": ("Toilet Cleaner", ("TC_",), {"Toilet Cleaner"}),
}

VERBATIM_THEME_KEYWORDS = [
    ("Price / Value", ("price", "cost", "expensive", "cheap", "affordable", "value", "money", "promo", "discount")),
    ("Availability", ("available", "availability", "scarce", "find", "market", "shop", "store", "everywhere", "nearby")),
    ("Quality / Performance", ("quality", "effective", "works", "strong", "durable", "clean", "fresh", "taste", "tasty", "sweet", "good", "better")),
    ("Brand / Trust", ("brand", "trusted", "trust", "popular", "known", "familiar", "recommend", "original")),
    ("Packaging / Size", ("pack", "packet", "package", "packaging", "size", "sachet", "bottle", "container", "small", "large")),
    ("Advertising / Recall", ("advert", "advertisement", "radio", "tv", "television", "billboard", "jingle", "social", "facebook", "instagram")),
    ("Usage / Preference", ("use", "consume", "prefer", "buy", "purchase", "often", "regular", "habit", "family")),
    ("Negative Feedback", ("bad", "poor", "dislike", "bitter", "weak", "problem", "complain", "complaint", "delay", "difficult")),
]

VERBATIM_STOPWORDS = {
    "about", "after", "again", "also", "because", "been", "before", "being", "between", "both", "cannot", "could",
    "does", "doing", "done", "each", "even", "every", "from", "have", "having", "into", "just", "like", "more",
    "most", "much", "only", "other", "others", "over", "same", "should", "some", "such", "than", "that", "their",
    "them", "then", "there", "these", "they", "this", "those", "through", "under", "very", "what", "when", "where",
    "which", "while", "with", "would", "your", "you", "and", "for", "the", "are", "was", "were", "not", "but",
    "can", "will", "our", "has", "had", "too", "any", "all", "its", "it's", "don", "don't", "did", "get", "got",
}

BHT_OVERVIEW_DISTRIBUTIONS = {
    "region": {
        "title": "Region",
        "variable": "City_1",
        "labels": {
            "1": "Lagos",
            "2": "Ibadan",
            "3": "Abuja",
            "4": "Kano",
            "5": "Kaduna",
            "6": "PHC",
            "7": "Benin",
            "8": "Onitsha",
            "9": "Enugu",
            "10": "Owerri",
            "11": "Jos",
            "12": "Uyo",
            "13": "Ilorin",
            "14": "Sokoto",
            "15": "Warri",
        },
    },
    "sector": {"title": "Sector", "variable": "Sector", "labels": {}},
    "sec": {"title": "SEC", "variable": "SEC", "labels": {}},
    "week": {
        "title": "Week",
        "variable": "Week",
        "labels": {"1": "Week 1", "2": "Week 2", "3": "Week 3", "4": "Week 4"},
    },
    "gender": {"title": "Gender", "variable": "Gender", "labels": {"1": "Male", "2": "Female"}},
    "age": {"title": "Age", "variable": "Age_cal", "labels": {}},
}

SECTOR_LABELS = {
    "1": "Egbeda",
    "2": "Ogba",
    "3": "Ketu",
    "4": "Surulere",
    "5": "Oshodi Mafoluku",
    "6": "Ojota",
    "7": "Oko oba",
    "8": "Tabon Tabon",
    "9": "Keke",
    "10": "Pero",
    "11": "Agege Stadium",
    "12": "Dopemu",
    "13": "SOKA/EYINI",
    "14": "OREMEJI AGUGU",
    "15": "ELEWURA",
    "16": "AJEIGBE",
    "17": "ADEOYO",
    "18": "OSOSAMI",
    "19": "BABALOLA ESTATE",
    "20": "AGBAJE",
    "21": "BOLUMOLE",
    "22": "ANFANI",
    "23": "FODASCIS",
    "24": "IDI OPE",
    "25": "Kuchigoro",
    "26": "Lugbe",
    "27": "Durumi",
    "28": "Pyakassa",
    "29": "Area 1",
    "30": "Wuse 2",
    "31": "Chika",
    "32": "Wuse",
    "33": "Aco",
    "34": "Aleita",
    "35": "Garki",
    "36": "Karmajiji",
    "37": "Sauka",
    "38": "brgade",
    "39": "Kurna",
    "40": "Dakata",
    "41": "Badawa",
    "42": "Zaria Road",
    "43": "Tudun Wada",
    "44": "Tudun Murtala",
    "45": "Gadon Kaya",
    "46": "Zango",
    "47": "Kabuga",
    "48": "Hotoro",
    "49": "Yankaba",
    "50": "Narayi",
    "51": "Barnawa",
    "52": "Kawo",
    "53": "Kabala",
    "54": "Television Garage",
    "55": "Ungwan Sunday",
    "56": "Trikania",
    "57": "Sabo",
    "58": "Ungwan Rimi",
    "59": "Malali",
    "60": "Kakuri",
    "61": "Gonin Gora",
    "62": "Diobu",
    "63": "Elekahia",
    "64": "Town",
    "65": "Nkpolu",
    "66": "Ada George",
    "67": "Rumuodumaya",
    "68": "Mgbuoba",
    "69": "Spaele Road",
    "70": "Okhoro Road",
    "71": "Aduwawa",
    "72": "Siloko Road",
    "73": "Ikpoba Hill",
    "74": "Upper Mission",
    "75": "Niger Cat Road",
    "76": "St Saviour",
    "77": "Welfare Road",
    "78": "Jesus Christ Road",
    "79": "Erediawa Road",
    "80": "Nomayo Road",
    "81": "Goodnews",
    "82": "Evbuabogun Road",
    "83": "Ogbugbankwa sector",
    "84": "Woliwo sector",
    "85": "Ugwunagbankpa inland town",
    "86": "Uke sector",
    "87": "Omagba phase 1 sector",
    "88": "Inland town sector",
    "89": "Sm Okeke sector",
    "90": "Amorji",
    "91": "Abakpa",
    "92": "New haven",
    "93": "Obiagu",
    "94": "Asata",
    "95": "Umuchigbo",
    "96": "Achara Layout",
    "97": "Ibeagwa",
    "98": "Wetheral",
    "99": "Douglas",
    "100": "Amakohia",
    "101": "Akwakuma",
    "102": "Orji",
    "103": "Naze",
    "104": "Nekede",
    "105": "Jenta",
    "106": "Rukuba",
    "107": "Utan",
    "108": "Busa Buji",
    "109": "Apata",
    "110": "Oku",
    "111": "Nwaniba",
    "112": "Mbiaobong",
    "113": "Itiam",
    "114": "OKE ODO",
    "115": "BUBU",
    "116": "ILEDU",
    "117": "BALOGUN",
    "118": "AGBOOBA",
    "119": "Tamje",
    "120": "Old Airport",
    "121": "Mana Area",
    "122": "Mabera",
    "123": "Permanent camp (DSC",
    "124": "Ekete water side",
    "125": "Ekete inland",
    "126": "Express junction",
    "127": "Mangoro",
}


MAIN_SURVEY_STATE_TARGET_CASES = {
    "ABIA": 500,
    "ADAMAWA": 500,
    "AKWA IBOM": 500,
    "ANAMBRA": 500,
    "BAUCHI": 500,
    "BAYELSA": 400,
    "BENUE": 500,
    "BORNO": 500,
    "CROSS RIVER": 500,
    "DELTA": 500,
    "EBONYI": 500,
    "EDO": 500,
    "EKITI": 400,
    "ENUGU": 500,
    "FCT": 500,
    "FEDERAL CAPITAL TERRITORY": 500,
    "GOMBE": 400,
    "IMO": 500,
    "JIGAWA": 500,
    "KADUNA": 6900,
    "KANO": 750,
    "KATSINA": 500,
    "KEBBI": 500,
    "KOGI": 500,
    "KWARA": 500,
    "LAGOS": 750,
    "NASARAWA": 500,
    "NIGER": 500,
    "OGUN": 750,
    "ONDO": 500,
    "OSUN": 500,
    "OYO": 500,
    "PLATEAU": 500,
    "RIVERS": 500,
    "SOKOTO": 500,
    "TARABA": 500,
    "YOBE": 500,
    "ZAMFARA": 500,
}


def _main_survey_state_target_cases(state_name: str | None) -> int:
    normalized = (state_name or "").strip().upper()
    return MAIN_SURVEY_STATE_TARGET_CASES.get(normalized, 0)


MAIN_EXPORT_DROP_NOTE_VARS = [
    "HHIFO", "C_intro", "note.1", "C3a.note", "C3b.note",
    "hh.sel.note1", "hh.sel.note2", "Selectedmember_note",
    "E.0.1.note", "E.0.2.note", "E14", "F9.note", "F12.note",
    "F12b.note", "F14.note", "CP.note", "CP8.note", "CP12",
    "QF3", "BA.note", "MF.note", "NB.note", "PY1a.note",
    "MM.note", "MM.Def", "MT.note", "DMT", "IMT", "MON",
    "MPC.note", "SA14", "SA15", "LC.note", "LC1.note",
    "LC1b.note", "CC2", "RM1.note", "RM1d.note", "RM11.note",
    "RM10.note", "RM2a.note", "INF.note", "INF1b.note",
    "INF2.note", "INF3.note", "PC1", "PC2.note", "PC3.note",
    "IE.note", "Gen1.note", "Gen1.note2", "Gen1a.note",
    "Gen3.note", "Gen5.note", "Efina.note", "B.note", "Thank",
]

MAIN_EXPORT_MULTIPLE_RESPONSE_PARENTS = [
    "D2", "D3", "E1", "E2", "E9", "E13c", "F4a",
    "CP1", "CP1ba", "CP2", "CP10", "CP14b",
    "QF1",
    "QF6.1", "QF7c.1", "QF9.1",
    "QF6.2", "QF7c.2", "QF9.2",
    "QF6.3", "QF7c.3", "QF9.3",
    "QF6.4", "QF7c.4", "QF9.4",
    "QF6.5", "QF7c.5", "QF9.5",
    "QF6.6", "QF7c.6", "QF9.6",
    "QF6.7", "QF7c.7", "QF9.7",
    "QF6.8", "QF7c.8", "QF9.8",
    "QF6.9", "QF7c.9", "QF9.9",
    "QF6.10", "QF7c.10", "QF9.10",
    "QF6.11", "QF7c.11", "QF9.11",
    "QF6.12", "QF7c.12", "QF9.12",
    "QF6.13", "QF7c.13", "QF9.13",
    "QF6.14", "QF7c.14", "QF9.14",
    "QF6.15", "QF7c.15", "QF9.15",
    "QF6.16", "QF7c.16", "QF9.16",
    "QF6.17", "QF7c.17", "QF9.17",
    "BAA2", "BA1", "BA3a", "BA4",
    "MF2", "MF3",
    "NB2", "NB3",
    "PY1a.1", "PY1a.2", "PY1a.3", "PY1a.4", "PY3b",
    "TE3",
    "MM3b", "MM5", "MM9a", "MM10b", "MM11",
    "MT1", "MT2a", "MT5", "MT7a", "MT10", "MT12a", "MT15", "MT17a", "MT20",
    "SA2", "SA3a", "SA6", "SA7a", "SA8b", "SA11a", "SA16",
    "LC2a",
    "CC1", "CC4", "CC6",
    "RM1a", "RM6", "RM9", "RM13",
    "INF1a", "INF4",
    "PWD1",
    "interest",
]


def _drop_main_export_note_variables(df: pd.DataFrame) -> pd.DataFrame:
    cols_to_drop = [c for c in MAIN_EXPORT_DROP_NOTE_VARS if c in df.columns]
    return df.drop(columns=cols_to_drop, errors="ignore")


def _split_main_export_hh_gps(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "hh_gps" not in out.columns:
        return out
    gps_parts = out["hh_gps"].astype("string").str.strip().str.split(r"\s+", expand=True)
    lat_series = pd.to_numeric(gps_parts[0], errors="coerce") if 0 in gps_parts.columns else pd.Series(np.nan, index=out.index)
    lng_series = pd.to_numeric(gps_parts[1], errors="coerce") if 1 in gps_parts.columns else pd.Series(np.nan, index=out.index)
    alt_series = pd.to_numeric(gps_parts[2], errors="coerce") if 2 in gps_parts.columns else pd.Series(np.nan, index=out.index)
    acc_series = pd.to_numeric(gps_parts[3], errors="coerce") if 3 in gps_parts.columns else pd.Series(np.nan, index=out.index)
    for col, series in [("hh_gps_Latitude", lat_series), ("hh_gps_Longitude", lng_series), ("hh_gps_Altitude", alt_series), ("hh_gps_Accuracy", acc_series)]:
        if col in out.columns:
            existing = pd.to_numeric(out[col], errors="coerce")
            out[col] = existing.where(existing.notna(), series)
        else:
            out[col] = series
    return out


def _fix_main_export_multiple_response_nulls(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    blank_values = {None: np.nan, "": np.nan, "nan": np.nan, "NaN": np.nan, "None": np.nan, "none": np.nan, "null": np.nan, "NULL": np.nan, "<NA>": np.nan}
    for parent in MAIN_EXPORT_MULTIPLE_RESPONSE_PARENTS:
        pattern = re.compile(rf"^{re.escape(parent)}_(.+)$")
        option_cols = [c for c in out.columns if pattern.match(str(c))]
        if not option_cols:
            continue
        temp = out[option_cols].astype("object").replace(blank_values)
        asked_mask = temp.notna().any(axis=1)
        temp.loc[asked_mask] = temp.loc[asked_mask].fillna(0)
        for col in option_cols:
            out[col] = pd.to_numeric(temp[col], errors="coerce")
    return out


def _clean_main_export_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out = _split_main_export_hh_gps(out)
    out = _fix_main_export_multiple_response_nulls(out)
    return out


MAIN_SURVEY_DICTIONARY_FILE = "MAIN_data_dictionary.xlsx"

MAIN_SURVEY_SECTION_CONFIG = [
    {"section": "META DTA", "title": "Meta DTA", "slug": "meta-dta", "pageEnabled": False},
    {"section": "A. HOUSEHOLD IDENTIFICATION", "title": "Household Identification", "slug": "household-identification", "pageEnabled": False},
    {"section": "B. PARTICULARS OF VISIT", "title": "Particulars Of Visit", "slug": "particulars-of-visit", "pageEnabled": False},
    {"section": "C. INTRODUCTION AND SCREENING QUESTIONS", "title": "Introduction And Screening Questions", "slug": "introduction-and-screening-questions", "pageEnabled": False},
    {"section": "D. HOUSEHOLD QUESTIONS", "title": "Household Questions", "slug": "household-questions", "pageEnabled": True},
    {"section": "E. DEMOGRAPHICS", "title": "Demographics", "slug": "demographics", "pageEnabled": True},
    {"section": "F. FINANCIAL CAPABILITY", "title": "Financial Capability", "slug": "financial-capability", "pageEnabled": True},
    {"section": "CONSUMER PROTECTION & FRAUD", "title": "Consumer Protection And Fraud", "slug": "consumer-protection-and-fraud", "pageEnabled": True},
    {"section": "QF: QUALITY OF FINANCIAL SERVICES", "title": "Quality Of Financial Services", "slug": "quality-of-financial-services", "pageEnabled": True},
    {"section": "BA: COMMERCIAL BANKS", "title": "Commercial Banks", "slug": "commercial-banks", "pageEnabled": True},
    {"section": "MF: MICROFINANCE & DIGITAL MICROFINANCE", "title": "Microfinance And Digital Microfinance", "slug": "microfinance-and-digital-microfinance", "pageEnabled": True},
    {"section": "NB: NON-INTEREST BANKING", "title": "Non-Interest Banking", "slug": "non-interest-banking", "pageEnabled": True},
    {"section": "PY: PAYMENT", "title": "Payment", "slug": "payment", "pageEnabled": True},
    {"section": "MM: MOBILE MONEY", "title": "Mobile Money", "slug": "mobile-money", "pageEnabled": True},
    {"section": "MT: MONEY TRANSFER", "title": "Money Transfer", "slug": "money-transfer", "pageEnabled": True},
    {"section": "SA: SAVINGS", "title": "Savings", "slug": "savings", "pageEnabled": True},
    {"section": "LC: LOANS & CREDIT", "title": "Loans And Credit", "slug": "loans-and-credit", "pageEnabled": True},
    {"section": "RM: RISK MANAGEMENT AND INSURANCE", "title": "Risk Management And Insurance", "slug": "risk-management-and-insurance", "pageEnabled": True},
    {"section": "GOVERNMENT POLICES", "title": "Government Policies", "slug": "government-policies", "pageEnabled": True},
    {"section": "INF: INFORMAL SERVICE PROVIDERS", "title": "Informal Service Providers", "slug": "informal-service-providers", "pageEnabled": True},
    {
        "section": "PC: POTENTIAL CHANNELS FOR CONDUCTING FINANCIAL TRANSCATIONS",
        "title": "Potential Channels For Conducting Financial Transactions",
        "slug": "potential-channels-for-conducting-financial-transactions",
        "pageEnabled": True,
    },
    {"section": "IE: INCOME AND EXPENDITURE", "title": "Income And Expenditure", "slug": "income-and-expenditure", "pageEnabled": True},
    {"section": "GEN: GENDER ROLES/NORMS", "title": "Gender Roles And Norms", "slug": "gender-roles-and-norms", "pageEnabled": True},
    {"section": "QUALITY CONTROL", "title": "Quality Control", "slug": "quality-control", "pageEnabled": False},
    {"section": "META DATA", "title": "Meta Data", "slug": "meta-data", "pageEnabled": False},
]

SECTION_BY_SLUG = {item["slug"]: item for item in MAIN_SURVEY_SECTION_CONFIG}
PAGE_SECTION_CONFIG = [item for item in MAIN_SURVEY_SECTION_CONFIG if item["pageEnabled"]]
HELPER_SUFFIXES = (".OTH", ".cal", ".label", ".auto")
MAX_OPEN_TEXT_ROWS = 20
MAX_CHART_ROWS = 12
MAIN_EXPORT_COLUMNS = [
    "submission_key",
    "case_id",
    "ea_id",
    "interviewer_id",
    "supervisor_id",
    "approval_stage",
    "is_callback_required",
    "submitted_at",
    "reviewed_at",
    "approved_at",
    "deleted_at",
    "deleted_by",
    "deletion_reason",
    "auto_flagged_qc_issue_count",
    "auto_flagged_qc_issue_codes",
    "auto_flagged_qc_issues",
]

# Demographic filter field names.
# These reference the JSONB record in clean.main_case_section for section E. DEMOGRAPHICS.
# TODO: Verify these variable names match the actual survey instrument before use.
DEMO_SECTION_NAME = "E. DEMOGRAPHICS"
DEMO_GENDER_FIELD = "E6"
DEMO_MARITAL_FIELD = "E4"
DEMO_EDUCATION_FIELD = "E8"
EXCLUDED_IE_VARIABLES = {f"IE1a.{idx}" for idx in range(1, 19)}

E6_LABELS = {
    "1": "Male",
    "2": "Female",
    "95": "Refused",
}

E4_LABELS = {
    "1": "Married (Monogamy)",
    "2": "Married (Polygamy)",
    "3": "Co-Habiting",
    "4": "Divorced",
    "5": "Separated",
    "6": "Widowed",
    "7": "Never married",
    "95": "Refused",
}

E8_LABELS = {
    "1": "Pre-school",
    "2": "Primary (incomplete)",
    "3": "Primary (complete)",
    "4": "Secondary (incomplete)",
    "5": "Secondary (complete)",
    "6": "Polytechnic OND/HND",
    "7": "University",
    "8": "Post-university (incomplete)",
    "9": "Post-university (complete)",
    "10": "Islamic school",
    "11": "Vocational/Technical",
    "12": "Non-Formal Religious",
    "13": "No education",
}


def _invert_label_map(label_map: dict[str, str]) -> dict[str, str]:
    return {str(label): str(code) for code, label in label_map.items()}


def _resolve_filter_values(values: list[str], label_map: dict[str, str] | None = None) -> list[str]:
    if not values:
        return []
    if not label_map:
        return [str(v) for v in values if str(v).strip()]
    inverse = _invert_label_map(label_map)
    resolved: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if not value:
            continue
        resolved.append(inverse.get(value, value))
    return resolved


def _label_for_filter_value(value: Any, label_map: dict[str, str] | None = None) -> str:
    text = _safe_text(value)
    if not text:
        return ""
    if not label_map:
        return text
    return label_map.get(text, text)


def _dictionary_path(settings: Settings) -> Path:
    return settings.root_dir / MAIN_SURVEY_DICTIONARY_FILE


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_boundary_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"-?\d+\.0+", text):
        return text.split(".", 1)[0]
    return text


def _normalize_boundary_text(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", " ", str(value or "").upper()).strip()
    return re.sub(r"\s+", " ", text)


def _feature_matches_boundary(
    feature: dict[str, Any],
    ea_id: str | None = None,
    boundary_id: str | None = None,
    ea_name: str | None = None,
    state_name: str | None = None,
    lga_name: str | None = None,
) -> bool:
    props = feature.get("properties") or {}
    candidates = {
        _normalize_boundary_key(props.get("sd_EA_ID")),
        _normalize_boundary_key(props.get("ea_id")),
        _normalize_boundary_key(props.get("EA_ID")),
        _normalize_boundary_key(props.get("boundary_id")),
        _normalize_boundary_key(props.get("sd_BOUNDARY_ID")),
    }
    candidates.discard("")
    targets = {_normalize_boundary_key(ea_id), _normalize_boundary_key(boundary_id)}
    targets.discard("")
    if candidates and targets and not candidates.isdisjoint(targets):
        return True

    name_target = _normalize_boundary_text(ea_name)
    if not name_target:
        return False

    name_candidates = {
        _normalize_boundary_text(props.get("sd_EA_NAME")),
        _normalize_boundary_text(props.get("name")),
        _normalize_boundary_text(props.get("ea_name")),
    }
    name_candidates.discard("")
    if name_target not in name_candidates:
        return False

    feature_state = _normalize_boundary_text(props.get("sd_STATE_NAME") or props.get("state") or props.get("state_name"))
    target_state = _normalize_boundary_text(state_name)
    if feature_state and target_state and feature_state != target_state:
        return False

    feature_lga = _normalize_boundary_text(props.get("sd_LGA_NAME") or props.get("lga_name"))
    target_lga = _normalize_boundary_text(lga_name)
    if feature_lga and target_lga and feature_lga != target_lga:
        return False

    return True


def _load_boundary_feature_from_zip(
    boundary_zip_path: str,
    ea_id: str | None = None,
    boundary_id: str | None = None,
    ea_name: str | None = None,
    state_name: str | None = None,
    lga_name: str | None = None,
) -> dict[str, Any] | None:
    zip_path = Path(boundary_zip_path)
    if not zip_path.exists():
        return None

    with zipfile.ZipFile(zip_path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.endswith(".geojson")
            and (name.startswith("output_geojson/Main/") or name.startswith("output_geojson/Deep Dive/"))
        ]
        if not names:
            names = [name for name in archive.namelist() if name.endswith(".geojson")]

        for name in names:
            with archive.open(name) as handle:
                data = json.load(handle)
            for feature in data.get("features", []):
                if _feature_matches_boundary(feature, ea_id, boundary_id, ea_name, state_name, lga_name):
                    geometry = feature.get("geometry")
                    if isinstance(geometry, dict):
                        properties = dict(feature.get("properties") or {})
                        if ea_id and not properties.get("sd_EA_ID"):
                            properties["sd_EA_ID"] = ea_id
                        if boundary_id and not properties.get("boundary_id"):
                            properties["boundary_id"] = boundary_id
                        return {
                            "type": "Feature",
                            "geometry": geometry,
                            "properties": properties,
                        }
    return None


def _has_postgis(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'reference'
              AND table_name = 'geo_boundaries_ea'
              AND column_name = 'geom'
            """
        )
        return cur.fetchone() is not None


def _is_helper_variable(variable_name: str) -> bool:
    return any(variable_name.endswith(suffix) for suffix in HELPER_SUFFIXES)


def _question_block(variable_name: str) -> str:
    """Return the parent block for a variable.

    Multi-select child variables always end with _N (underscore + one or more digits).
    Stripping that suffix gives the parent/group code.  This covers patterns like:
      D3_1, D3_2        → D3
      QF6.1_1, QF6.1_2  → QF6.1
      F4a_1, F4a_22     → F4a
      BAA2_1            → BAA2
    Variables without a numeric suffix are their own block.
    """
    m = re.match(r"^(.+)_(\d+)$", variable_name)
    if m:
        return m.group(1)
    return variable_name


def _canonical_value(value: Any) -> str:
    text = _safe_text(value)
    if not text:
        return ""

    try:
        numeric = float(text)
    except ValueError:
        return text

    if numeric.is_integer():
        return f"{numeric:.1f}"
    return str(numeric)


def _parse_value_labels(raw: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for part in _safe_text(raw).split("|"):
        cleaned = part.strip()
        if not cleaned or "=" not in cleaned:
            continue
        code, label = cleaned.split("=", 1)
        labels[_canonical_value(code)] = _safe_text(label)
    return labels


def _ordered_value_labels(raw: str) -> list[tuple[str, str]]:
    labels: list[tuple[str, str]] = []
    for part in _safe_text(raw).split("|"):
        cleaned = part.strip()
        if not cleaned or "=" not in cleaned:
            continue
        code, label = cleaned.split("=", 1)
        labels.append((_canonical_value(code), _safe_text(label)))
    return labels


def _ordered_inline_value_labels(raw_label: str) -> list[tuple[str, str]]:
    cleaned = re.sub(r"<[^>]+>", " ", str(raw_label or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return []

    matches = re.findall(
        r"[\"']?(\d+(?:\.\d+)?)[\"']?\s*for\s*(.+?)(?=(?:\s+(?:and|or)\s+[\"']?\d)|$)",
        cleaned,
        flags=re.IGNORECASE,
    )
    labels: list[tuple[str, str]] = []
    for code, label in matches:
        normalized_label = _safe_text(label).strip(" .,:;")
        if normalized_label:
            labels.append((_canonical_value(code), normalized_label))
    return labels


def _plain_numeric_code(value: Any) -> str | None:
    text = _safe_text(value)
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        return None
    if numeric.is_integer():
        return str(int(numeric))
    return str(numeric)


def _answer_key_candidates(code: Any, label: Any) -> list[str]:
    candidates: list[str] = []
    for raw in (code, label):
        text = _safe_text(raw)
        if not text:
            continue
        candidates.append(text)
        candidates.append(_canonical_value(text))
        plain_numeric = _plain_numeric_code(text)
        if plain_numeric:
            candidates.append(plain_numeric)
    return list(dict.fromkeys(candidates))


def _count_answer_option(counter: Counter[str], code: str, label: str, consumed: set[str]) -> int:
    total = 0
    for candidate in _answer_key_candidates(code, label):
        if candidate in consumed:
            continue
        total += counter.get(candidate, 0)
        consumed.add(candidate)
    return total


def _is_placeholder_numeric_label(code: str, label: str) -> bool:
    return _plain_numeric_code(code) is not None and _plain_numeric_code(code) == _plain_numeric_code(label)


def _enhance_ordered_value_labels(variable: str, ordered: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if not ordered:
        return ordered
    labels_by_code = {code: label for code, label in ordered}
    label_text = " ".join(labels_by_code.values()).lower()
    if "not at all confident" in label_text and "very confident" in label_text:
        confidence_labels = {
            "2.0": "Not very confident",
            "3.0": "Somewhat confident",
            "4.0": "Confident",
        }
        return [
            (code, confidence_labels.get(code, label) if _is_placeholder_numeric_label(code, label) else label)
            for code, label in ordered
        ]
    return ordered


def _normalize_yes_no_answer(value: Any) -> str | None:
    text = _safe_text(value).strip().lower()
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    if text in {"yes", "y", "true"}:
        return "Yes"
    if text in {"no", "n", "false"}:
        return "No"
    return None


def _yes_no_table_rows(counter: Counter[str], percent_base: int) -> list[dict[str, Any]] | None:
    if not counter:
        return None

    counts = {"Yes": 0, "No": 0}
    saw_yes_no_label = False
    for code, count in counter.items():
        normalized = _normalize_yes_no_answer(code)
        if normalized is None:
            return None
        saw_yes_no_label = True
        counts[normalized] += count

    if not saw_yes_no_label:
        return None

    return [
        {
            "code": label,
            "label": label,
            "count": counts[label],
            "percent": round((counts[label] / percent_base) * 100, 1) if percent_base else 0.0,
        }
        for label in ("Yes", "No")
    ]


def _chart_rows(counter: Counter[str], label_map: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for key, value in counter.most_common():
        label = label_map.get(key, key or "Blank")
        rows.append({"label": label, "value": value})
    return rows


def _extract_record_answers(
    raw_value: Any,
    split_multi_value: bool,
    *,
    canonicalize_numeric: bool = True,
) -> list[str]:
    if raw_value is None:
        return []

    if isinstance(raw_value, (list, tuple, set)):
        candidates = list(raw_value)
    else:
        text = _safe_text(raw_value)
        if not text:
            return []
        if split_multi_value:
            if any(separator in text for separator in (",", ";", "|")):
                candidates = [part.strip() for part in re.split(r"\s*[,;|]\s*", text)]
            else:
                space_parts = text.split()
                if len(space_parts) > 1 and all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in space_parts):
                    candidates = space_parts
                else:
                    candidates = [text]
        else:
            candidates = [text]

    values: list[str] = []
    for candidate in candidates:
        text = _safe_text(candidate)
        if not text:
            continue
        values.append(_canonical_value(text) if canonicalize_numeric else text)
    return values


def _option_label_from_child(parent_label: str, child_label: str) -> str:
    child = _safe_text(child_label)
    parent = _safe_text(parent_label)
    if parent and child.startswith(parent):
        trimmed = child[len(parent) :].strip(" :?-")
        if trimmed:
            return trimmed
    if ": " in child:
        return child.rsplit(": ", 1)[-1].strip()
    return child


def _prompt_label_from_child(child_label: str) -> str:
    child = _safe_text(child_label)
    if ": " in child:
        return child.rsplit(": ", 1)[0].strip(" :?-")
    return ""


def _is_binary_multi_option(value_labels: str) -> bool:
    ordered = _ordered_value_labels(value_labels)
    if not ordered:
        return False
    codes = {code for code, _ in ordered}
    return codes.issubset({"0", "0.0", "1", "1.0"}) and len(codes) >= 2


def _derive_multi_select_label(
    block: str,
    parent_row: dict[str, str] | None,
    child_rows: list[dict[str, str]],
) -> str:
    if parent_row and parent_row["label"]:
        return parent_row["label"]

    prefix_counter = Counter(
        prefix
        for prefix in (_prompt_label_from_child(child["label"]) for child in child_rows)
        if prefix
    )
    if prefix_counter:
        return prefix_counter.most_common(1)[0][0]

    if child_rows:
        return child_rows[0]["label"]
    return block


def _is_multi_select_sibling_group(
    parent_row: dict[str, str] | None,
    child_rows: list[dict[str, str]],
) -> bool:
    """Return True when child_rows form a multi-select group.

    Per spec: detect multi-select ONLY using the _N suffix pattern.
    _question_block() already strips the suffix, so all siblings passed
    here share the same block name.  We just need at least 2 binary options.
    """
    if len(child_rows) < 2:
        return False

    binary_children = [child for child in child_rows if _is_binary_multi_option(child["valueLabels"])]
    return len(binary_children) >= 2


def _build_multi_select_question_card(
    card_variable: str,
    card_label: str,
    card_row: dict[str, str],
    child_rows: list[dict[str, str]],
    records: list[dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    base_respondents: set[int] = set()
    option_rows: list[dict[str, Any]] = []

    # For select-multiple sibling columns stored as yes/no flags, the base for every
    # option must be the same: respondents with any non-blank answer across the group.
    # Blank rows are excluded from the base; no/0 responses still count in the base.
    for idx, record in enumerate(records):
        if any(_extract_record_answers(record.get(child["variable"]), split_multi_value=True) for child in child_rows):
            base_respondents.add(idx)

    responding_records = len(base_respondents)

    for child in child_rows:
        selected_codes = _selected_codes(child["valueLabels"])
        selected_count = 0

        for idx, record in enumerate(records):
            if idx not in base_respondents:
                continue
            answers = _extract_record_answers(record.get(child["variable"]), split_multi_value=True)
            if selected_codes:
                if any(answer in selected_codes for answer in answers):
                    selected_count += 1
            elif any(answer not in {"0", "0.0"} for answer in answers):
                selected_count += 1

        option_rows.append(
            {
                "code": child["variable"],
                "label": _option_label_from_child(card_label, child["label"]),
                "count": selected_count,
                "percent": round((selected_count / responding_records) * 100, 1) if responding_records else 0.0,
            }
        )

    table_rows = option_rows
    chart_data = [
        {"label": item["label"], "value": item["count"]}
        for item in option_rows
        if item["count"] > 0
    ][:MAX_CHART_ROWS]

    if responding_records:
        note = (
            f"{responding_records} respondent row{'s' if responding_records != 1 else ''} answered at least one option in this multi-response question. "
            "Percentages use the common base of yes+no responses across the option group and exclude blanks only."
        )
        source = "records"
    else:
        note = (
            "No responses are loaded yet. Workbook option rows are shown so the multi-select "
            "question structure is still visible."
        )
        source = "dictionary"

    card = {
        "variable": card_variable,
        "label": card_label,
        "storageType": card_row["storageType"],
        "measure": card_row["measure"],
        "valueLabels": card_row["valueLabels"],
        "source": source,
        "isMultiSelect": True,
        "responseCount": responding_records,
        "distinctResponseCount": len([item for item in option_rows if item["count"] > 0]),
        "note": note,
        "tableRows": table_rows,
        "chartData": chart_data,
    }
    return card, bool(responding_records)


def _selected_codes(value_labels: str) -> set[str]:
    ordered = _ordered_value_labels(value_labels)
    if not ordered:
        return set()

    yes_like = {
        code
        for code, label in ordered
        if any(token in label.lower() for token in ("yes", "selected", "checked"))
    }
    if yes_like:
        return yes_like

    excluded_tokens = ("no", "none", "not applicable", "don't know", "do not know", "refused")
    selected: set[str] = set()
    for code, label in ordered:
        normalized = label.lower()
        if any(token in normalized for token in excluded_tokens):
            continue
        if code in {"0", "0.0"}:
            continue
        selected.add(code)
    return selected


@lru_cache(maxsize=1)
def _load_dictionary(root_dir: str) -> tuple[str, dict[str, list[dict[str, str]]]]:
    dictionary_path = Path(root_dir) / MAIN_SURVEY_DICTIONARY_FILE
    if not dictionary_path.exists():
        raise FileNotFoundError(f"Main Survey dictionary file not found: {dictionary_path}")

    df = pd.read_excel(dictionary_path).fillna("")
    expected = {
        "variable_name",
        "variable_label",
        "storage_type",
        "measure",
        "value_labels",
        "Section",
    }
    normalized_columns = {str(column).strip(): column for column in df.columns}
    if not expected.issubset(set(normalized_columns)):
        missing = ", ".join(sorted(expected.difference(set(normalized_columns))))
        raise RuntimeError(f"Main Survey dictionary is missing required columns: {missing}")

    by_section: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in df.to_dict(orient="records"):
        section_name = _safe_text(row.get("Section"))
        by_section[section_name].append(
            {
                "variable": _safe_text(row.get("variable_name")),
                "label": _safe_text(row.get("variable_label")),
                "storageType": _safe_text(row.get("storage_type")).lower(),
                "measure": _safe_text(row.get("measure")).lower(),
                "valueLabels": _safe_text(row.get("value_labels")),
            }
        )

    return str(dictionary_path), dict(by_section)


def _latest_monthly_xlsform_path(root_dir: str) -> Path | None:
    xlsform_dir = Path(root_dir) / "data" / "monthly_xlsform_dictionary"
    if not xlsform_dir.exists():
        return None
    files = sorted(
        (path for path in xlsform_dir.glob("*.xlsx") if not path.name.startswith("~$")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def _clean_verbatim_label(value: Any) -> str:
    text = _safe_text(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\$\{[^}]+\}", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


@lru_cache(maxsize=8)
def _load_verbatim_question_metadata(root_dir: str, dictionary_signature: str) -> dict[str, dict[str, str]]:
    dictionary_path = Path(dictionary_signature.split("|", 1)[0])
    if not dictionary_path.exists():
        return {}

    try:
        survey_df = pd.read_excel(dictionary_path, sheet_name="survey").fillna("")
    except Exception:
        logger.warning("Unable to load XLSForm verbatim metadata from %s.", dictionary_path, exc_info=True)
        return {}

    columns = {str(column).strip(): column for column in survey_df.columns}
    if not {"type", "name"}.issubset(columns):
        return {}

    type_col = columns["type"]
    name_col = columns["name"]
    label_col = columns.get("label")

    records = survey_df.to_dict(orient="records")
    label_by_variable = {
        _safe_text(row.get(name_col)).strip(): _clean_verbatim_label(row.get(label_col)) if label_col else ""
        for row in records
        if _safe_text(row.get(name_col)).strip()
    }

    metadata: dict[str, dict[str, str]] = {}
    section_stack: list[str] = []
    for row in records:
        raw_type = _safe_text(row.get(type_col)).strip()
        variable = _safe_text(row.get(name_col)).strip()
        label = _clean_verbatim_label(row.get(label_col)) if label_col else ""
        if not raw_type:
            continue
        question_type = raw_type.split()[0].lower()

        if raw_type.lower().startswith("begin "):
            section_stack.append(label or variable or (section_stack[-1] if section_stack else ""))
            continue
        if raw_type.lower().startswith("end "):
            if section_stack:
                section_stack.pop()
            continue
        if question_type != "text" or not variable:
            continue
        if variable.lower().endswith("_oth"):
            continue

        section_name = next((section for section in reversed(section_stack) if section), "")
        question_label = label or variable
        if variable.endswith("_OTH") and label.lower() in {"others", "other", "specify others", "specify other"}:
            parent_variable = variable.removesuffix("_OTH")
            parent_label = label_by_variable.get(parent_variable, "")
            if parent_label and parent_label.lower() not in {"others", "other"}:
                question_label = f"Other specified response for: {parent_label}"
        for category_slug, (category_label, prefixes, allowed_sections) in VERBATIM_CATEGORY_PREFIXES.items():
            if variable.startswith(prefixes) or section_name in allowed_sections:
                metadata[variable] = {
                    "variableName": variable,
                    "questionLabel": question_label,
                    "section": section_name or category_label,
                    "category": category_slug,
                    "categoryLabel": category_label,
                    "questionType": question_type,
                }
                break
    return metadata


def _get_verbatim_question_metadata(root_dir: str) -> dict[str, dict[str, str]]:
    dictionary_path = _latest_monthly_xlsform_path(root_dir)
    if dictionary_path is None:
        return {}
    stat = dictionary_path.stat()
    signature = f"{dictionary_path}|{stat.st_mtime_ns}|{stat.st_size}"
    return _load_verbatim_question_metadata(root_dir, signature)


def _selected_verbatim_metadata(root_dir: str, categories: list[str], questions: list[str]) -> dict[str, dict[str, str]]:
    metadata = _get_verbatim_question_metadata(root_dir)
    if not metadata:
        return {}
    selected_categories = {category for category in categories if category and category != "all"}
    selected_questions = {question for question in questions if question}
    return {
        variable: meta
        for variable, meta in metadata.items()
        if (not selected_categories or meta["category"] in selected_categories)
        and (not selected_questions or variable in selected_questions)
    }


def _split_csv_filter(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _classify_verbatim_theme(text: str) -> str:
    normalized = f" {text.lower()} "
    for theme, keywords in VERBATIM_THEME_KEYWORDS:
        if any(re.search(rf"\b{re.escape(keyword)}\b", normalized) for keyword in keywords):
            return theme
    return "Other"


def _build_verbatim_theme_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        response = _safe_text(row.get("response"))
        if not response:
            continue
        theme = _classify_verbatim_theme(response)
        counts[theme] += 1
        if len(samples[theme]) < 3:
            samples[theme].append(response[:220])

    total = sum(counts.values()) or 1
    return [
        {
            "theme": theme,
            "count": count,
            "percent": round((count / total) * 100, 1),
            "samples": samples.get(theme, []),
        }
        for theme, count in counts.most_common()
    ]


def _build_verbatim_word_cloud(rows: list[dict[str, Any]], limit: int = 70) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for row in rows:
        response = _safe_text(row.get("response")).lower()
        for word in re.findall(r"[a-zA-Z][a-zA-Z'-]{2,}", response):
            normalized = word.strip("'-").lower()
            if len(normalized) < 3 or normalized in VERBATIM_STOPWORDS:
                continue
            counts[normalized] += 1
    return [{"text": word, "count": count} for word, count in counts.most_common(limit)]


def _ensure_main_verbatim_mart(cur: Any) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mart.main_verbatim_answer (
            case_id text NOT NULL REFERENCES clean.main_case (case_id) ON DELETE CASCADE,
            submission_key text,
            survey_month text,
            category_slug text NOT NULL,
            category_label text NOT NULL,
            section_name text,
            variable_name text NOT NULL,
            question_label text,
            response_text text NOT NULL,
            theme text NOT NULL DEFAULT 'Other',
            region_label text,
            region_respondent_ordinal integer,
            interviewer_id text,
            start_time text,
            submitted_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (case_id, variable_name)
        )
        """
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_main_verbatim_category_submitted ON mart.main_verbatim_answer (category_slug, submitted_at DESC, case_id)",
        "CREATE INDEX IF NOT EXISTS idx_main_verbatim_variable_submitted ON mart.main_verbatim_answer (variable_name, submitted_at DESC, case_id)",
        "CREATE INDEX IF NOT EXISTS idx_main_verbatim_theme ON mart.main_verbatim_answer (theme, category_slug)",
        "CREATE INDEX IF NOT EXISTS idx_main_verbatim_response_fts ON mart.main_verbatim_answer USING gin (to_tsvector('simple', response_text))",
    ):
        cur.execute(statement)


def _verbatim_theme_sql_expression(value_expression: str = "lower(response_text)", question_expression: str = "''") -> str:
    return f"""
        CASE
            WHEN {value_expression} ~ '\\m(price|cost|expensive|cheap|affordable|value|money|promo|discount)\\M' THEN 'Price / Value'
            WHEN {value_expression} ~ '\\m(available|availability|scarce|find|market|shop|store|everywhere|nearby)\\M' THEN 'Availability'
            WHEN {value_expression} ~ '\\m(quality|effective|works|strong|durable|clean|fresh|taste|tasty|sweet|good|better)\\M' THEN 'Quality / Performance'
            WHEN {value_expression} ~ '\\m(brand|trusted|trust|popular|known|familiar|recommend|original)\\M' THEN 'Brand / Trust'
            WHEN {value_expression} ~ '\\m(pack|packet|package|packaging|size|sachet|bottle|container|small|large)\\M' THEN 'Packaging / Size'
            WHEN {value_expression} ~ '\\m(advert|advertisement|radio|tv|television|billboard|jingle|social|facebook|instagram)\\M' THEN 'Advertising / Recall'
            WHEN {value_expression} ~ '\\m(use|consume|prefer|buy|purchase|often|regular|habit|family)\\M' THEN 'Usage / Preference'
            WHEN {value_expression} ~ '\\m(bad|poor|dislike|bitter|weak|problem|complain|complaint|delay|difficult)\\M' THEN 'Negative Feedback'
            WHEN {question_expression} ~ '\\m(advert|advertisement|message|remember|recall)\\M' THEN 'Advertising / Recall'
            WHEN {question_expression} ~ '\\m(reason|consume|consuming|brand more|like)\\M' THEN 'Usage / Preference'
            ELSE 'Other'
        END
    """


def refresh_main_verbatim_answer_mart(settings: Settings, submission_keys: list[str] | None = None) -> dict[str, Any]:
    if not settings.database_url:
        return {"status": "skipped", "reason": "DATABASE_URL is not configured."}

    metadata = _get_verbatim_question_metadata(str(settings.root_dir))
    metadata_rows = [
        (
            item["variableName"],
            item["category"],
            item["categoryLabel"],
            item["section"],
            item["questionLabel"],
        )
        for item in metadata.values()
        if item.get("variableName") and not str(item.get("variableName")).endswith("_OTH")
    ]
    if not metadata_rows:
        return {"status": "skipped", "reason": "No non-_OTH open-ended category variables found in XLSForm."}

    selected_submission_keys = [str(key or "").strip() for key in (submission_keys or []) if str(key or "").strip()]
    city_labels = BHT_OVERVIEW_DISTRIBUTIONS["region"]["labels"]
    theme_expr = _verbatim_theme_sql_expression("lower(answer.response_text)", "lower(answer.question_label)")
    mc_scope_clause, mc_scope_params = main_case_scope_clause(settings, "mc")
    mc_all_scope_clause, mc_all_scope_params = main_case_scope_clause(settings, "mc_all")

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            # Full verbatim rebuilds are background maintenance. They are kept
            # out of web startup and may legitimately exceed interactive limits.
            cur.execute("SET LOCAL statement_timeout = '10min'")
            _ensure_main_verbatim_mart(cur)
            cur.execute("CREATE TEMP TABLE tmp_main_verbatim_metadata (variable_name text PRIMARY KEY, category_slug text, category_label text, section_name text, question_label text) ON COMMIT DROP")
            cur.executemany(
                """
                INSERT INTO tmp_main_verbatim_metadata (
                    variable_name, category_slug, category_label, section_name, question_label
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (variable_name) DO UPDATE SET
                    category_slug = EXCLUDED.category_slug,
                    category_label = EXCLUDED.category_label,
                    section_name = EXCLUDED.section_name,
                    question_label = EXCLUDED.question_label
                """,
                metadata_rows,
            )
            if selected_submission_keys:
                cur.execute(
                    """
                    DELETE FROM mart.main_verbatim_answer
                    WHERE submission_key = ANY(%s)
                    """,
                    (selected_submission_keys,),
                )
                scope_clause = "AND mc.submission_key = ANY(%s)"
                scope_params: list[Any] = [selected_submission_keys]
            else:
                cur.execute("DELETE FROM mart.main_verbatim_answer")
                scope_clause = ""
                scope_params = []

            cur.execute(
                f"""
                WITH region_ranked AS (
                    SELECT
                        mc_all.case_id,
                        COALESCE(
                            NULLIF(TRIM(mc_all.record->>'City_1'), ''),
                            NULLIF(TRIM(mc_all.record->>'state_name'), ''),
                            'Region'
                        ) AS region_raw,
                        ROW_NUMBER() OVER (
                            PARTITION BY COALESCE(
                                NULLIF(TRIM(mc_all.record->>'City_1'), ''),
                                NULLIF(TRIM(mc_all.record->>'state_name'), ''),
                                'Region'
                            )
                            ORDER BY mc_all.submitted_at ASC NULLS LAST, mc_all.created_at ASC NULLS LAST, mc_all.case_id ASC
                        )::int AS region_respondent_ordinal
                    FROM clean.main_case mc_all
                    WHERE NOT EXISTS (
                        SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = mc_all.submission_key
                    )
                    {mc_all_scope_clause}
                ),
                answer AS (
                    SELECT
                        mc.case_id,
                        mc.submission_key,
                        mc.survey_month,
                        meta.category_slug,
                        meta.category_label,
                        meta.section_name,
                        meta.variable_name,
                        meta.question_label,
                        btrim(mc.record->>meta.variable_name) AS response_text,
                        COALESCE(
                            NULLIF(TRIM(mc.record->>'City_1'), ''),
                            NULLIF(TRIM(mc.record->>'state_name'), ''),
                            'Region'
                        ) AS region_raw,
                        COALESCE(NULLIF(TRIM(mc.record->>'username'), ''), mc.interviewer_id) AS interviewer_id,
                        COALESCE(
                            NULLIF(TRIM(mc.record->>'starttime'), ''),
                            NULLIF(TRIM(mc.record->>'start_time'), ''),
                            NULLIF(TRIM(mc.record->>'StartTime'), ''),
                            NULLIF(TRIM(mc.record->>'start'), '')
                        ) AS start_time,
                        mc.submitted_at
                    FROM clean.main_case mc
                    CROSS JOIN tmp_main_verbatim_metadata meta
                    WHERE mc.record ? meta.variable_name
                      AND mc.record->>meta.variable_name IS NOT NULL
                      AND btrim(mc.record->>meta.variable_name) <> ''
                      AND lower(btrim(mc.record->>meta.variable_name)) NOT IN ('nan', 'none', 'nat', 'null')
                      AND NOT EXISTS (
                          SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = mc.submission_key
                      )
                      {mc_scope_clause}
                      {scope_clause}
                )
                INSERT INTO mart.main_verbatim_answer (
                    case_id, submission_key, survey_month, category_slug, category_label,
                    section_name, variable_name, question_label, response_text, theme,
                    region_label, region_respondent_ordinal, interviewer_id, start_time,
                    submitted_at, updated_at
                )
                SELECT
                    answer.case_id,
                    answer.submission_key,
                    answer.survey_month,
                    answer.category_slug,
                    answer.category_label,
                    answer.section_name,
                    answer.variable_name,
                    answer.question_label,
                    answer.response_text,
                    {theme_expr},
                    COALESCE(%s::jsonb ->> answer.region_raw, answer.region_raw, 'Region') AS region_label,
                    region_ranked.region_respondent_ordinal,
                    answer.interviewer_id,
                    answer.start_time,
                    answer.submitted_at,
                    now()
                FROM answer
                LEFT JOIN region_ranked ON region_ranked.case_id = answer.case_id
                ON CONFLICT (case_id, variable_name) DO UPDATE SET
                    submission_key = EXCLUDED.submission_key,
                    survey_month = EXCLUDED.survey_month,
                    category_slug = EXCLUDED.category_slug,
                    category_label = EXCLUDED.category_label,
                    section_name = EXCLUDED.section_name,
                    question_label = EXCLUDED.question_label,
                    response_text = EXCLUDED.response_text,
                    theme = EXCLUDED.theme,
                    region_label = EXCLUDED.region_label,
                    region_respondent_ordinal = EXCLUDED.region_respondent_ordinal,
                    interviewer_id = EXCLUDED.interviewer_id,
                    start_time = EXCLUDED.start_time,
                    submitted_at = EXCLUDED.submitted_at,
                    updated_at = now()
                """,
                [*mc_all_scope_params, *mc_scope_params, *scope_params, json.dumps(city_labels)],
            )
            cur.execute("SELECT COUNT(*)::int AS c FROM mart.main_verbatim_answer")
            count = int((cur.fetchone() or {}).get("c") or 0)
        conn.commit()

    return {"status": "success", "rowCount": count, "variableCount": len(metadata_rows)}


def _main_verbatim_filter_context(
    settings: Settings,
    categories: str | None,
    questions: str | None,
    search: str | None,
) -> tuple[list[dict[str, str]], list[dict[str, str]], str, list[Any], dict[str, dict[str, str]]]:
    category_filters = _split_csv_filter(categories)
    question_filters = _split_csv_filter(questions)
    all_metadata = _get_verbatim_question_metadata(str(settings.root_dir))
    selected_metadata = _selected_verbatim_metadata(str(settings.root_dir), category_filters, question_filters)
    selected_variables = sorted(selected_metadata)

    category_options = [
        {"value": slug, "label": label}
        for slug, (label, _, _) in VERBATIM_CATEGORY_PREFIXES.items()
    ]
    question_options = sorted(
        all_metadata.values(),
        key=lambda item: (item["categoryLabel"], item["section"], item["variableName"]),
    )

    where_parts = ["1 = 1"]
    params: list[Any] = []
    if selected_variables:
        where_parts.append("variable_name = ANY(%s)")
        params.append(selected_variables)
    category_slugs = [category for category in category_filters if category and category != "all"]
    if category_slugs:
        where_parts.append("category_slug = ANY(%s)")
        params.append(category_slugs)
    search_term = (search or "").strip()
    if search_term:
        like = f"%{search_term}%"
        where_parts.append(
            """(
                response_text ILIKE %s
                OR variable_name ILIKE %s
                OR question_label ILIKE %s
                OR category_label ILIKE %s
                OR section_name ILIKE %s
                OR submission_key ILIKE %s
                OR interviewer_id ILIKE %s
                OR to_tsvector('simple', response_text) @@ plainto_tsquery('simple', %s)
            )"""
        )
        params.extend([like, like, like, like, like, like, like, search_term])
    return category_options, question_options, " AND ".join(where_parts), params, all_metadata


def get_main_survey_verbatims_summary(
    settings: Settings,
    user: AuthUser,
    *,
    categories: str | None = None,
    questions: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    del user
    category_options, question_options, where_sql, params, _all_metadata = _main_verbatim_filter_context(settings, categories, questions, search)

    if not settings.database_url:
        return {
            "total": 0,
            "categories": category_options,
            "questions": question_options,
            "themeSummary": [],
            "wordCloud": [],
        }

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            _ensure_main_verbatim_mart(cur)
            cur.execute(f"SELECT COUNT(*)::int AS total FROM mart.main_verbatim_answer WHERE {where_sql}", params)
            total = int((cur.fetchone() or {}).get("total") or 0)

            cur.execute(
                f"""
                SELECT theme, COUNT(*)::int AS count
                FROM mart.main_verbatim_answer
                WHERE {where_sql}
                GROUP BY theme
                ORDER BY count DESC, theme
                LIMIT 8
                """,
                params,
            )
            theme_count_rows = [dict(row) for row in cur.fetchall()]

            cur.execute(
                f"""
                SELECT
                    category_label,
                    variable_name,
                    question_label,
                    COUNT(*)::int AS count
                FROM mart.main_verbatim_answer
                WHERE {where_sql}
                  AND theme = 'Other'
                GROUP BY category_label, variable_name, question_label
                ORDER BY count DESC, category_label, variable_name
                LIMIT 12
                """,
                params,
            )
            other_breakdown_rows = [dict(row) for row in cur.fetchall()]

            cur.execute(
                f"""
                WITH tokens AS (
                    SELECT lower(trim(both '''' from regexp_split_to_table(response_text, '[^A-Za-z'']+'))) AS word
                    FROM mart.main_verbatim_answer
                    WHERE {where_sql}
                    LIMIT 10000
                )
                SELECT word AS text, COUNT(*)::int AS count
                FROM tokens
                WHERE length(word) >= 3
                  AND NOT (word = ANY(%s))
                GROUP BY word
                ORDER BY count DESC, word
                LIMIT 70
                """,
                [*params, sorted(VERBATIM_STOPWORDS)],
            )
            word_cloud = [dict(row) for row in cur.fetchall()]

    total_for_pct = total or 1
    theme_summary = [
        {
            "theme": row["theme"],
            "count": int(row["count"] or 0),
            "percent": round((int(row["count"] or 0) / total_for_pct) * 100, 1),
            "samples": [],
            "breakdown": [
                {
                    "categoryLabel": item.get("category_label"),
                    "variableName": item.get("variable_name"),
                    "questionLabel": item.get("question_label"),
                    "count": int(item.get("count") or 0),
                }
                for item in other_breakdown_rows
            ] if row["theme"] == "Other" else [],
        }
        for row in theme_count_rows
    ]

    return {
        "total": total,
        "categories": category_options,
        "questions": question_options,
        "themeSummary": theme_summary,
        "wordCloud": word_cloud,
    }


def get_main_survey_verbatims(
    settings: Settings,
    user: AuthUser,
    *,
    categories: str | None = None,
    questions: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    del user
    page = max(1, int(page or 1))
    page_size = max(10, min(int(page_size or 50), 200))
    offset = (page - 1) * page_size

    category_options, question_options, where_sql, params, all_metadata = _main_verbatim_filter_context(settings, categories, questions, search)

    if not settings.database_url:
        return {
            "items": [],
            "total": 0,
            "page": page,
            "pageSize": page_size,
            "hasMore": False,
            "categories": category_options,
            "questions": question_options,
            "themeSummary": [],
            "wordCloud": [],
        }

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            _ensure_main_verbatim_mart(cur)
            cur.execute(f"SELECT COUNT(*)::int AS total FROM mart.main_verbatim_answer WHERE {where_sql}", params)
            total = int((cur.fetchone() or {}).get("total") or 0)

            cur.execute(
                f"""
                SELECT theme, COUNT(*)::int AS count
                FROM mart.main_verbatim_answer
                WHERE {where_sql}
                GROUP BY theme
                ORDER BY count DESC, theme
                LIMIT 8
                """,
                params,
            )
            theme_count_rows = [dict(row) for row in cur.fetchall()]

            cur.execute(
                f"""
                SELECT
                    case_id,
                    submission_key,
                    interviewer_id,
                    submitted_at,
                    start_time,
                    region_label,
                    region_respondent_ordinal,
                    category_slug,
                    category_label,
                    section_name,
                    variable_name,
                    question_label,
                    response_text,
                    theme
                FROM mart.main_verbatim_answer
                WHERE {where_sql}
                ORDER BY submitted_at DESC NULLS LAST, submission_key, variable_name
                LIMIT %s OFFSET %s
                """,
                [*params, page_size, offset],
            )
            rows = [dict(row) for row in cur.fetchall()]


    items: list[dict[str, Any]] = []
    for row in rows:
        variable = _safe_text(row.get("variable_name"))
        meta = all_metadata.get(variable) or {}
        region_label = _safe_text(row.get("region_label")) or "Region"
        ordinal = row.get("region_respondent_ordinal")
        case_label = f"{region_label}_Resp._{ordinal}" if ordinal else _safe_text(row.get("submission_key")) or _safe_text(row.get("case_id"))
        items.append(
            {
                "id": f"{row.get('submission_key') or row.get('case_id')}:{variable}",
                "caseId": _safe_text(row.get("case_id")),
                "submissionKey": _safe_text(row.get("submission_key")),
                "caseLabel": case_label,
                "region": region_label,
                "regionRespondentOrdinal": ordinal,
                "interviewerId": _safe_text(row.get("interviewer_id")),
                "submittedAt": row.get("submitted_at").isoformat() if hasattr(row.get("submitted_at"), "isoformat") else row.get("submitted_at"),
                "startTime": _safe_text(row.get("start_time")),
                "category": row.get("category_slug") or meta.get("category", ""),
                "categoryLabel": row.get("category_label") or meta.get("categoryLabel", ""),
                "section": row.get("section_name") or meta.get("section", ""),
                "variableName": variable,
                "questionLabel": row.get("question_label") or meta.get("questionLabel", variable),
                "response": _safe_text(row.get("response_text")),
                "theme": row.get("theme") or _classify_verbatim_theme(_safe_text(row.get("response_text"))),
            }
        )

    total_for_pct = total or 1
    theme_summary = [
        {
            "theme": row["theme"],
            "count": int(row["count"] or 0),
            "percent": round((int(row["count"] or 0) / total_for_pct) * 100, 1),
            "samples": [],
        }
        for row in theme_count_rows
    ]

    return {
        "items": items,
        "total": total,
        "page": page,
        "pageSize": page_size,
        "hasMore": offset + len(items) < total,
        "categories": category_options,
        "questions": question_options,
        "themeSummary": theme_summary,
        "wordCloud": [],
    }


def _get_section_record_counts(settings: Settings, user: AuthUser) -> dict[str, int]:
    if not settings.database_url:
        return {}

    where_parts: list[str] = [
        "NOT EXISTS (SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = c.submission_key)"
    ]
    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    try:
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT s.section_name, COUNT(*)::int AS row_count
                    FROM clean.main_case_section s
                    JOIN clean.main_case c
                      ON c.case_id = s.case_id
                    {where_sql}
                    GROUP BY s.section_name
                    """,
                )
                rows = cur.fetchall()
    except Exception:
        return {}

    return {_safe_text(row["section_name"]): int(row["row_count"] or 0) for row in rows}


def _load_section_records(
    settings: Settings,
    user: AuthUser,
    section_name: str,
    filters: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    if not settings.database_url:
        return []

    filters = filters or {}
    where_parts = [
        "s.section_name = %s",
        "NOT EXISTS (SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = c.submission_key)",
    ]
    params: list[Any] = [section_name]

    # State filter — join to geo reference table via ea_id
    if filters.get("states"):
        where_parts.append(
            """
            EXISTS (
                SELECT 1 FROM reference.geo_boundaries_ea g
                WHERE g.ea_id = c.ea_id
                  AND g.state_name = ANY(%s)
            )
            """
        )
        params.append(filters["states"])

    # Demographic filters — sub-select from the E. DEMOGRAPHICS section record
    if filters.get("genders"):
        where_parts.append(
            f"""
            EXISTS (
                SELECT 1 FROM clean.main_case_section ed
                WHERE ed.case_id = c.case_id
                  AND ed.section_name = %s
                  AND ed.record->>%s = ANY(%s)
            )
            """
        )
        params.extend([DEMO_SECTION_NAME, DEMO_GENDER_FIELD, _resolve_filter_values(filters["genders"], E6_LABELS)])

    if filters.get("marital_statuses"):
        where_parts.append(
            f"""
            EXISTS (
                SELECT 1 FROM clean.main_case_section ed
                WHERE ed.case_id = c.case_id
                  AND ed.section_name = %s
                  AND ed.record->>%s = ANY(%s)
            )
            """
        )
        params.extend([DEMO_SECTION_NAME, DEMO_MARITAL_FIELD, _resolve_filter_values(filters["marital_statuses"], E4_LABELS)])

    if filters.get("education_levels"):
        where_parts.append(
            f"""
            EXISTS (
                SELECT 1 FROM clean.main_case_section ed
                WHERE ed.case_id = c.case_id
                  AND ed.section_name = %s
                  AND ed.record->>%s = ANY(%s)
            )
            """
        )
        params.extend([DEMO_SECTION_NAME, DEMO_EDUCATION_FIELD, _resolve_filter_values(filters["education_levels"], E8_LABELS)])

    if filters.get("statuses"):
        where_parts.append("LOWER(COALESCE(c.approval_stage, '')) = ANY(%s)")
        params.append([str(item).strip().lower() for item in filters["statuses"] if str(item).strip()])

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.record
                FROM clean.main_case_section s
                JOIN clean.main_case c
                  ON c.case_id = s.case_id
                WHERE {' AND '.join(where_parts)}
                ORDER BY c.submitted_at NULLS LAST, s.row_no
                """,
                params,
            )
            rows = cur.fetchall()

    return [row.get("record") or {} for row in rows]


def _build_section_stats(rows: list[dict[str, str]], record_count: int) -> dict[str, int]:
    coded_count = sum(1 for row in rows if row["valueLabels"])
    helper_count = sum(1 for row in rows if _is_helper_variable(row["variable"]))
    block_count = len({_question_block(row["variable"]) for row in rows})
    return {
        "variableCount": len(rows),
        "codedCount": coded_count,
        "helperCount": helper_count,
        "blockCount": block_count,
        "recordCount": record_count,
    }


def _build_block_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[_question_block(row["variable"])].append(row)

    block_rows: list[dict[str, Any]] = []
    for block, items in grouped.items():
        focus = next((item["label"] for item in items if not _is_helper_variable(item["variable"])), items[0]["label"])
        block_rows.append(
            {
                "block": block,
                "variableCount": len(items),
                "focus": focus,
                "note": f"{len(items)} variables mapped under {block}",
            }
        )

    block_rows.sort(key=lambda item: (-item["variableCount"], item["block"]))
    return block_rows


def _build_metadata_charts(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    block_counter = Counter(_question_block(row["variable"]) for row in rows)
    storage_counter = Counter(row["storageType"] or "unknown" for row in rows)
    measure_counter = Counter(row["measure"] or "unknown" for row in rows)

    return [
        {
            "key": "variables-by-block",
            "title": "Variables By Block",
            "data": [{"label": label, "value": value} for label, value in block_counter.most_common(10)],
        },
        {
            "key": "storage-types",
            "title": "Storage Types",
            "data": [{"label": label.title(), "value": value} for label, value in storage_counter.most_common()],
        },
        {
            "key": "measure-types",
            "title": "Measure Types",
            "data": [{"label": label.title(), "value": value} for label, value in measure_counter.most_common()],
        },
    ]


def _build_question_cards(rows: list[dict[str, str]], records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    question_cards: list[dict[str, Any]] = []
    observed_question_count = 0

    non_helper_rows = [row for row in rows if not _is_helper_variable(row["variable"])]
    rows_by_block: dict[str, list[dict[str, str]]] = defaultdict(list)
    block_order: list[str] = []
    seen_blocks: set[str] = set()
    for row in non_helper_rows:
        block = _question_block(row["variable"])
        rows_by_block[block].append(row)
        if block not in seen_blocks:
            seen_blocks.add(block)
            block_order.append(block)

    consumed_variables: set[str] = set()

    for block in block_order:
        block_rows = [row for row in rows_by_block[block] if row["variable"] not in consumed_variables]
        if not block_rows:
            continue

        parent_row = next((row for row in block_rows if row["variable"] == block), None)
        child_rows = [row for row in block_rows if row["variable"] != block]

        if _is_multi_select_sibling_group(parent_row, child_rows):
            card_row = parent_row or child_rows[0]
            card_label = _derive_multi_select_label(block, parent_row, child_rows)
            card_variable = parent_row["variable"] if parent_row else block
            card, has_records = _build_multi_select_question_card(
                card_variable=card_variable,
                card_label=card_label,
                card_row=card_row,
                child_rows=child_rows,
                records=records,
            )
            question_cards.append(card)
            if has_records:
                observed_question_count += 1
            for grouped_row in block_rows:
                consumed_variables.add(grouped_row["variable"])
            continue

        for row in block_rows:
            variable = row["variable"]
            if variable in consumed_variables:
                continue

            consumed_variables.add(variable)
            ordered_value_labels = _ordered_value_labels(row["valueLabels"])
            if not ordered_value_labels:
                ordered_value_labels = _ordered_inline_value_labels(row["label"])
            ordered_value_labels = _enhance_ordered_value_labels(variable, ordered_value_labels)
            label_map = dict(ordered_value_labels)
            split_multi_value = bool(ordered_value_labels)

            counter: Counter[str] = Counter()
            response_count = 0
            for record in records:
                answers = _extract_record_answers(
                    record.get(variable),
                    split_multi_value,
                    canonicalize_numeric=bool(ordered_value_labels),
                )
                if answers:
                    response_count += 1
                for answer in answers:
                    counter[answer] += 1

            total_responses = sum(counter.values())
            if response_count:
                observed_question_count += 1

            # Detect select_multiple: total selections exceed respondent count
            # means each respondent selected more than one option.
            # Use response_count as denominator so % = share of respondents.
            is_multi_value_column = split_multi_value and total_responses > response_count
            percent_base = response_count if is_multi_value_column else total_responses

            table_rows: list[dict[str, Any]] = []
            if ordered_value_labels:
                seen_codes: set[str] = set()
                consumed_answer_keys: set[str] = set()
                for code, label in ordered_value_labels:
                    count = _count_answer_option(counter, code, label, consumed_answer_keys)
                    percent = round((count / percent_base) * 100, 1) if percent_base else 0.0
                    table_rows.append(
                        {
                            "code": code,
                            "label": label,
                            "count": count,
                            "percent": percent,
                        }
                    )
                    seen_codes.add(code)

                for code, count in counter.most_common():
                    if code in seen_codes or code in consumed_answer_keys:
                        continue
                    percent = round((count / percent_base) * 100, 1) if percent_base else 0.0
                    table_rows.append(
                        {
                            "code": code,
                            "label": label_map.get(code, code),
                            "count": count,
                            "percent": percent,
                        }
                    )
            elif counter:
                yes_no_rows = _yes_no_table_rows(counter, percent_base)
                if yes_no_rows is not None:
                    table_rows.extend(yes_no_rows)
                else:
                    for code, count in counter.most_common(MAX_OPEN_TEXT_ROWS):
                        table_rows.append(
                            {
                                "code": code,
                                "label": code,
                                "count": count,
                                "percent": round((count / percent_base) * 100, 1) if percent_base else 0.0,
                            }
                        )

            chart_data = [
                {"label": item["label"], "value": item["count"]}
                for item in table_rows
                if item["count"] > 0
            ][:MAX_CHART_ROWS]

            if response_count:
                if is_multi_value_column:
                    note = (
                        f"{response_count} respondent row{'s' if response_count != 1 else ''} contributed to this question. "
                        "Percentages show the share of respondents selecting each option."
                    )
                else:
                    note = f"{response_count} recorded response{'s' if response_count != 1 else ''} observed for this question."
                    if not ordered_value_labels and len(counter) > MAX_OPEN_TEXT_ROWS:
                        note += f" Showing the top {MAX_OPEN_TEXT_ROWS} unique responses in the table."
                source = "records"
            elif ordered_value_labels:
                note = "No responses are loaded yet. Workbook answer options are shown so the question structure is still visible."
                source = "dictionary"
            else:
                note = "No responses are loaded yet for this question."
                source = "dictionary"

            question_cards.append(
                {
                    "variable": variable,
                    "label": row["label"],
                    "storageType": row["storageType"],
                    "measure": row["measure"],
                    "valueLabels": row["valueLabels"],
                    "source": source,
                    "isMultiSelect": is_multi_value_column,
                    "responseCount": response_count,
                    "distinctResponseCount": len([item for item in table_rows if item["count"] > 0]),
                    "note": note,
                    "tableRows": table_rows,
                    "chartData": chart_data,
                }
            )

    return question_cards, observed_question_count


def _build_variable_charts(question_cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    charts: list[dict[str, Any]] = []
    for question in question_cards:
        if question["source"] != "records" or not question["chartData"]:
            continue
        charts.append(
            {
                "variable": question["variable"],
                "title": question["label"],
                "label": question["label"],
                "source": "records",
                "data": question["chartData"],
            }
        )
        if len(charts) >= 6:
            break
    return charts


def _section_payload(
    settings: Settings,
    user: AuthUser,
    config: dict[str, Any],
    rows_by_section: dict[str, list[dict[str, str]]],
    record_counts: dict[str, int],
    filters: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    section_name = config["section"]
    rows = rows_by_section.get(section_name, [])
    unfiltered_record_count = record_counts.get(section_name, 0)
    records = _load_section_records(settings, user, section_name, filters=filters) if unfiltered_record_count else []
    record_count = len(records)
    block_rows = _build_block_rows(rows)
    question_cards, observed_question_count = _build_question_cards(rows, records)
    variable_charts = _build_variable_charts(question_cards)
    stats = _build_section_stats(rows, record_count)
    stats["observedQuestionCount"] = observed_question_count

    active_filter_count = sum(1 for values in (filters or {}).values() if values)
    filter_suffix = (
        f" after applying {active_filter_count} active filter{'s' if active_filter_count != 1 else ''}"
        if active_filter_count
        else ""
    )

    if record_count:
        summary = (
            f"{config['title']} is backed by {stats['variableCount']} workbook-defined variables and currently has "
            f"{record_count} section row{'s' if record_count != 1 else ''} available in the ACCESS data model{filter_suffix}."
        )
        status = "records"
    else:
        summary = (
            f"{config['title']} is backed by {stats['variableCount']} workbook-defined variables. "
            f"No section records are currently available{filter_suffix}, so the page falls back to dictionary-driven analytics."
        )
        status = "dictionary-only"

    return {
        "status": status,
        "section": {
            **config,
            "dictionaryLoaded": bool(rows),
            "variableCount": stats["variableCount"],
            "codedCount": stats["codedCount"],
            "helperCount": stats["helperCount"],
            "blockCount": stats["blockCount"],
            "recordCount": stats["recordCount"],
        },
        "summary": summary,
        "stats": stats,
        "blockRows": block_rows,
        "metadataCharts": _build_metadata_charts(rows),
        "variableCharts": variable_charts,
        "questionCards": question_cards,
        "dictionary": rows,
    }


def get_main_survey_overview(settings: Settings, user: AuthUser) -> dict[str, Any]:
    workbook_path, rows_by_section = _load_dictionary(str(settings.root_dir))
    record_counts = _get_section_record_counts(settings, user)

    sections = [
        {
            **config,
            "dictionaryLoaded": bool(rows_by_section.get(config["section"], [])),
            **_build_section_stats(rows_by_section.get(config["section"], []), record_counts.get(config["section"], 0)),
        }
        for config in PAGE_SECTION_CONFIG
    ]

    total_variables = sum(section["variableCount"] for section in sections)
    total_records = sum(section["recordCount"] for section in sections)
    populated_sections = sum(1 for section in sections if section["recordCount"] > 0)
    dictionary_backed_sections = sum(1 for section in sections if section["dictionaryLoaded"])

    if total_records:
        summary = (
            f"The Main Survey workspace currently exposes {len(sections)} routed section pages, backed by {total_variables} "
            f"dictionary variables and {total_records} loaded section row{'s' if total_records != 1 else ''}."
        )
        status = "records"
    else:
        summary = (
            f"The Main Survey workspace currently exposes {len(sections)} routed section pages, backed by {total_variables} "
            "dictionary variables. Section pages are ready now and will switch to respondent analytics as data lands in clean.main_case_section."
        )
        status = "dictionary-only"

    return {
        "status": status,
        "workbookPath": workbook_path,
        "summary": summary,
        "totalPageSections": len(sections),
        "dictionaryBackedSections": dictionary_backed_sections,
        "populatedSections": populated_sections,
        "totalVariables": total_variables,
        "totalRecords": total_records,
        "sections": sections,
    }


def get_main_survey_state_ea_summary(settings: Settings, user: AuthUser) -> dict[str, Any]:
    """Return state-level and EA-level case counts with accompaniment stats."""
    approved_only_sql = ""
    try:
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '25000ms'")
                cur.execute(f"""
                WITH base AS (
                    SELECT
                        d.ea_id,
                        COALESCE(NULLIF(TRIM(d.ea_name), ''), d.ea_id, 'Unknown') AS ea_name,
                        COALESCE(NULLIF(TRIM(d.state_name), ''), 'Unknown') AS state_name,
                        CASE
                            WHEN LOWER(TRIM(COALESCE(d.state_name, ''))) = 'kaduna' THEN 15
                            ELSE 10
                        END::int AS target_cases,
                        TRIM(LOWER(COALESCE(d.final_outcome_code, ''))) AS final_outcome,
                        TRIM(LOWER(COALESCE(d.slot_type, ''))) AS slot_type,
                        COALESCE(d.approval_stage, '') AS approval_stage,
                        LOWER(COALESCE(NULLIF(TRIM(d.supacc_confirm), ''), '')) AS supacc_confirm
                    FROM mart.main_case_dim d
                    WHERE COALESCE(NULLIF(TRIM(d.state_name), ''), 'Unknown') <> 'Unknown'
                    {approved_only_sql}
                )
                SELECT
                    CASE WHEN GROUPING(ea_id) = 1 THEN 'state' ELSE 'ea' END AS row_level,
                    state_name,
                    ea_id,
                    ea_name,
                    target_cases,
                    COUNT(*)::int AS total_cases,
                    COUNT(*) FILTER (
                        WHERE slot_type = 'main sample'
                    )::int AS main_achieved_cases,
                    COUNT(*) FILTER (
                        WHERE slot_type = 'replacement sample'
                    )::int AS replacement_achieved_cases,
                    COUNT(*) FILTER (
                        WHERE approval_stage = 'approved'
                    )::int AS approved_cases,
                    COUNT(*) FILTER (
                        WHERE approval_stage = 'rejected'
                    )::int AS rejected_cases,
                    COUNT(*) FILTER (
                        WHERE approval_stage NOT IN ('approved','rejected')
                    )::int AS pending_cases,
                    COUNT(*) FILTER (
                        WHERE supacc_confirm IN ('yes','1','true')
                    )::int AS accompaniment_yes
                FROM base
                GROUP BY GROUPING SETS (
                    (state_name),
                    (state_name, ea_id, ea_name, target_cases)
                )
                ORDER BY row_level DESC, state_name, ea_name
            """)
                state_rows = []
                ea_rows = []
                for raw_row in cur.fetchall():
                    row = dict(raw_row)
                    row_level = row.pop("row_level")
                    if row_level == "state":
                        state_rows.append({
                            "state_name": row["state_name"],
                            "total_cases": row["total_cases"],
                            "main_achieved_cases": row["main_achieved_cases"],
                            "replacement_achieved_cases": row["replacement_achieved_cases"],
                            "approved_cases": row["approved_cases"],
                            "rejected_cases": row["rejected_cases"],
                            "pending_cases": row["pending_cases"],
                            "accompaniment_yes": row["accompaniment_yes"],
                        })
                    else:
                        ea_rows.append(row)
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "57014":
            logger.warning("Main survey state/EA summary timed out; returning empty fallback.")
            return {"stateRows": [], "eaRows": [], "partial": True, "message": "Summary timed out. Try again after sync/QC finishes."}
        if getattr(exc, "sqlstate", None) in {"42703", "42P01"}:
            logger.warning("Main survey state/EA summary schema is still updating; returning fallback.", exc_info=True)
            return {"stateRows": [], "eaRows": [], "partial": True, "message": "Overview schema is updating. Refresh after startup finishes."}
        raise

    for row in state_rows:
        total = row["total_cases"]
        row["accompaniment_pct"] = round(row["accompaniment_yes"] / total * 100, 1) if total else 0.0
        row["target_cases"] = _main_survey_state_target_cases(row.get("state_name"))

    for row in ea_rows:
        total = row["total_cases"]
        row["accompaniment_pct"] = round(row["accompaniment_yes"] / total * 100, 1) if total else 0.0

    return {"stateRows": state_rows, "eaRows": ea_rows}


def get_main_survey_answer_breakdown(
    settings: Settings,
    user: AuthUser,
    slug: str,
    variable: str,
    code: str,
    is_multi: bool = False,
    statuses: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return list of submission_key + metadata for cases that selected a given answer."""
    config = SECTION_BY_SLUG.get(slug)
    if not config:
        raise HTTPException(status_code=404, detail=f"Section '{slug}' not found.")
    section_name: str = config["section"]

    status_values = [str(item).strip().lower() for item in (statuses or []) if str(item).strip()]
    stage_sql = psql.SQL("")
    stage_params: list[Any] = []
    if status_values:
        stage_sql = psql.SQL(" AND LOWER(COALESCE(mc.approval_stage, '')) = ANY(%s) ")
        stage_params.append(status_values)
    code_candidates: list[str] = [str(code)]
    code_text = str(code).strip()
    try:
        if code_text.endswith(".0"):
            code_candidates.append(str(int(float(code_text))))
        elif "." not in code_text:
            code_candidates.append(f"{int(code_text)}.0")
    except (ValueError, TypeError):
        pass
    # preserve order and uniqueness
    code_candidates = list(dict.fromkeys(code_candidates))

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            if is_multi:
                # code IS the child variable name; match rows where it is not 0/empty
                query = psql.SQL("""
                    SELECT DISTINCT ON (mc.submission_key)
                        mc.submission_key,
                        COALESCE(NULLIF(TRIM(d.state_name), ''), NULLIF(TRIM(mc.record->>'state_name'), ''), 'Unknown') AS state_name,
                        COALESCE(NULLIF(TRIM(mc.record->>'ea_name'), ''), mc.ea_id, mc.record->>'ea_id', 'Unknown') AS ea_name,
                        mc.submitted_at::text AS submitted_at,
                        mc.interviewer_id,
                        mc.approval_stage
                    FROM clean.main_case mc
                    LEFT JOIN mart.main_case_dim d ON d.case_id = mc.case_id
                    WHERE (
                        EXISTS (
                            SELECT 1 FROM clean.main_case_section mcs
                            WHERE mcs.case_id = mc.case_id
                              AND mcs.record->>{field} NOT IN ('0', '0.0', '')
                              AND mcs.record->>{field} IS NOT NULL
                        )
                        OR (
                            mc.record->>{field} NOT IN ('0', '0.0', '')
                            AND mc.record->>{field} IS NOT NULL
                        )
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM clean.deleted_main_cases dmc
                        WHERE dmc.submission_key = mc.submission_key
                    )
                    {stage}
                    ORDER BY mc.submission_key, mc.submitted_at
                """).format(field=psql.Literal(code), stage=stage_sql)
                cur.execute(query, stage_params)
            else:
                query = psql.SQL("""
                    SELECT DISTINCT ON (mc.submission_key)
                        mc.submission_key,
                        COALESCE(NULLIF(TRIM(d.state_name), ''), NULLIF(TRIM(mc.record->>'state_name'), ''), 'Unknown') AS state_name,
                        COALESCE(NULLIF(TRIM(mc.record->>'ea_name'), ''), mc.ea_id, mc.record->>'ea_id', 'Unknown') AS ea_name,
                        mc.submitted_at::text AS submitted_at,
                        mc.interviewer_id,
                        mc.approval_stage
                    FROM clean.main_case mc
                    LEFT JOIN mart.main_case_dim d ON d.case_id = mc.case_id
                    WHERE (
                        EXISTS (
                            SELECT 1 FROM clean.main_case_section mcs
                            WHERE mcs.case_id = mc.case_id
                              AND mcs.record->>{field} = ANY(%s)
                        )
                        OR mc.record->>{field} = ANY(%s)
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM clean.deleted_main_cases dmc
                        WHERE dmc.submission_key = mc.submission_key
                    )
                    {stage}
                    ORDER BY mc.submission_key, mc.submitted_at
                """).format(field=psql.Literal(variable), stage=stage_sql)
                cur.execute(query, [code_candidates, code_candidates, *stage_params])

            return [dict(r) for r in cur.fetchall()]


def get_filter_options(settings: Settings, user: AuthUser) -> dict[str, list[str]]:
    """Return distinct values for each demographic filter dimension."""
    if not settings.database_url:
        return {"states": [], "genders": [], "maritalStatuses": [], "educationLevels": []}

    try:
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT g.state_name
                    FROM clean.main_case m
                    JOIN reference.geo_boundaries_ea g ON g.ea_id = COALESCE(m.ea_id, m.record->>'ea_id')
                    WHERE g.state_name IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM clean.deleted_main_cases dmc
                          WHERE dmc.submission_key = m.submission_key
                      )
                    ORDER BY g.state_name
                    """
                )
                states = [row["state_name"] for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT DISTINCT
                        mcs.record->>%s AS gender,
                        mcs.record->>%s AS marital_status,
                        mcs.record->>%s AS education_level
                    FROM clean.main_case_section mcs
                    JOIN clean.main_case m ON m.case_id = mcs.case_id
                    WHERE mcs.section_name = %s
                      AND mcs.record IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM clean.deleted_main_cases dmc
                          WHERE dmc.submission_key = m.submission_key
                      )
                    """,
                    (DEMO_GENDER_FIELD, DEMO_MARITAL_FIELD, DEMO_EDUCATION_FIELD, DEMO_SECTION_NAME),
                )
                gender_values, marital_values, education_values = set(), set(), set()
                for row in cur.fetchall():
                    if row.get("gender"):
                        gender_values.add(str(row["gender"]))
                    if row.get("marital_status"):
                        marital_values.add(str(row["marital_status"]))
                    if row.get("education_level"):
                        education_values.add(str(row["education_level"]))

                cur.execute(
                    """
                    SELECT DISTINCT NULLIF(TRIM(m.record->>'final_outcome_code'), '') AS final_outcome_code
                    FROM clean.main_case m
                    WHERE NULLIF(TRIM(m.record->>'final_outcome_code'), '') IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM clean.deleted_main_cases dmc
                          WHERE dmc.submission_key = m.submission_key
                      )
                    ORDER BY final_outcome_code
                    """
                )
                final_outcomes = [row["final_outcome_code"] for row in cur.fetchall() if row.get("final_outcome_code")]

        return {
            "states": states,
            "genders": sorted({_label_for_filter_value(value, E6_LABELS) for value in gender_values if _label_for_filter_value(value, E6_LABELS)}),
            "maritalStatuses": sorted({_label_for_filter_value(value, E4_LABELS) for value in marital_values if _label_for_filter_value(value, E4_LABELS)}),
            "educationLevels": sorted({_label_for_filter_value(value, E8_LABELS) for value in education_values if _label_for_filter_value(value, E8_LABELS)}),
            "finalOutcomeCodes": final_outcomes,
        }
    except Exception:
        return {"states": [], "genders": [], "maritalStatuses": [], "educationLevels": [], "finalOutcomeCodes": []}


def get_main_survey_section(
    settings: Settings,
    user: AuthUser,
    slug: str,
    filters: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    config = SECTION_BY_SLUG.get(slug)
    if not config or not config["pageEnabled"]:
        raise KeyError(slug)

    workbook_path, rows_by_section = _load_dictionary(str(settings.root_dir))
    record_counts = _get_section_record_counts(settings, user)
    payload = _section_payload(settings, user, config, rows_by_section, record_counts, filters=filters)
    payload["workbookPath"] = workbook_path
    return payload


def manual_main_survey_sync(
    settings: Settings,
    user: AuthUser,
    device_id: str | None = None,
    client_ip: str | None = None,
    forwarded_for: str | None = None,
    surveycto_username: str | None = None,
    surveycto_password: str | None = None,
) -> dict[str, Any]:
    request_started_at = time.monotonic()
    sync_request_token = str(uuid4())
    pipeline_config = load_main_survey_pipeline_config(
        settings.root_dir,
        sync_source="manual",
        sync_request_token=sync_request_token,
    )
    logger.info(
        "Manual main survey sync requested by user=%s user_id=%s role=%s device_id=%s client_ip=%s forwarded_for=%s request_token=%s",
        user.username,
        user.id,
        user.role,
        device_id or "-",
        client_ip or "-",
        forwarded_for or "-",
        sync_request_token,
    )
    request_manual_sync_override(
        pipeline_config,
        sync_request_token,
        user.username,
        "Manual main survey sync takes priority over cron/background SurveyCTO work.",
    )

    try:
        with db_connection(settings) as conn:
            lock_wait_started_at = time.monotonic()
            with survey_sync_lock_wait(conn) as waited_for_lock:
                wait_seconds = time.monotonic() - lock_wait_started_at
                logger.info(
                    "Manual main survey sync lock acquired by user=%s waited_for_lock=%s wait_seconds=%.3f request_token=%s",
                    user.username,
                    waited_for_lock,
                    wait_seconds,
                    sync_request_token,
                )
                sync_result = run_main_survey_sync_job(
                    source="manual",
                    sync_request_token=sync_request_token,
                    surveycto_username=surveycto_username,
                    surveycto_password=surveycto_password,
                )
    except Exception as exc:
        status_code, detail = describe_sync_failure(exc)
        raise HTTPException(status_code=status_code, detail=f"Main Survey sync failed: {detail}") from exc
    finally:
        clear_manual_sync_override(pipeline_config, sync_request_token)

    total_seconds = time.monotonic() - request_started_at
    logger.info(
        "Manual main survey sync finished for user=%s total_seconds=%.3f",
        user.username,
        total_seconds,
    )
    status = sync_result.get("status", "success") if isinstance(sync_result, dict) else "success"
    if status == "success":
        clear_bht_analytics_caches(settings, refresh_map_mart=True)
    summary = sync_result.get("message") if isinstance(sync_result, dict) else None
    if status == "warning":
        message = f"Main Survey sync finished with warnings. {summary or ''}".strip()
    elif status == "upstream_busy":
        message = sync_result.get("reason") or "Main Survey sync could not start because SurveyCTO is already serving another request."
    else:
        message = summary or "Main Survey sync completed."
    return {
        "message": message,
        "sync": sync_result,
        "targetModel": "clean.main_case + clean.main_case_section + clean.main_case_roster + mart.main_case_dim",
    }


def _overview_approval_clause(user: AuthUser, alias: str = "m") -> tuple[str, list[Any]]:
    return f"COALESCE(NULLIF(TRIM({alias}.state_name), ''), 'Unknown') <> 'Unknown'", []


def get_main_overview_demographics(
    settings: Settings,
    user: AuthUser,
    months: list[str] | None = None,
    states: list[str] | None = None,
) -> dict[str, Any]:
    """Hub-style executive summary distributions using mart.main_case_dim."""
    if not settings.database_url:
        return {
            "category": "main",
            "totalRespondents": 0,
            "monthsAvailable": [],
            "monthsSelected": months or [],
            "statesSelected": states or [],
            "distributions": {},
        }

    months_sel = [str(m).strip() for m in (months or []) if str(m).strip()]
    states_sel = [str(s).strip() for s in (states or []) if str(s).strip()]
    appr, appr_params = _overview_approval_clause(user, "d")
    month_sql = " AND d.interview_month = ANY(%s) " if months_sel else ""
    state_sql = " AND d.state_name = ANY(%s) " if states_sel else ""
    base_params = list(appr_params)
    if months_sel:
        base_params.append(months_sel)
    if states_sel:
        base_params.append(states_sel)

    def _dist(cur, column_sql: str) -> list[dict[str, Any]]:
        cur.execute(
            f"""
            SELECT
                COALESCE(NULLIF(TRIM({column_sql}), ''), '(No response)') AS value,
                COUNT(*)::int AS count
            FROM mart.main_case_dim d
            WHERE {appr}
            {month_sql}
            {state_sql}
            GROUP BY 1
            ORDER BY count DESC
            """,
            tuple(base_params),
        )
        rows = cur.fetchall()
        total = sum(int(r["count"] or 0) for r in rows)
        out = []
        for r in rows:
            c = int(r["count"] or 0)
            out.append(
                {
                    "value": str(r["value"]),
                    "count": c,
                    "pct": round((c / total) * 100, 2) if total > 0 else 0.0,
                }
            )
        return out

    try:
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '25000ms'")
                cur.execute(
                    f"""
                    SELECT COUNT(*)::int AS total
                    FROM mart.main_case_dim d
                    WHERE {appr}
                    {month_sql}
                    {state_sql}
                    """,
                    tuple(base_params),
                )
                total_respondents = int((cur.fetchone() or {}).get("total") or 0)

                cur.execute(
                    f"""
                    SELECT DISTINCT d.interview_month AS value
                    FROM mart.main_case_dim d
                    WHERE {appr} AND d.interview_month IS NOT NULL
                    ORDER BY 1
                    """,
                    tuple(appr_params),
                )
                months_available = [str(r["value"]) for r in cur.fetchall() if r.get("value")]

                cur.execute(
                    f"""
                    SELECT
                        COUNT(DISTINCT d.ea_id)::int AS total_eas,
                        COUNT(*)::int AS total_households,
                        COUNT(*) FILTER (
                            WHERE LOWER(COALESCE(NULLIF(TRIM(d.approval_stage), ''), '')) = 'approved'
                        )::int AS approved_hh,
                        COUNT(*) FILTER (
                            WHERE LOWER(COALESCE(NULLIF(TRIM(d.approval_stage), ''), '')) = 'rejected'
                        )::int AS rejected_hh
                    FROM mart.main_case_dim d
                    WHERE {appr}
                    {month_sql}
                    {state_sql}
                    """,
                    tuple(base_params),
                )
                kpi_row = cur.fetchone() or {}
                pipeline_kpis = {
                    "totalEAs": int(kpi_row.get("total_eas") or 0),
                    "totalHouseholds": int(kpi_row.get("total_households") or 0),
                    "approvedHH": int(kpi_row.get("approved_hh") or 0),
                    "rejectedHH": int(kpi_row.get("rejected_hh") or 0),
                }

                distributions = {
                    "state": _dist(cur, "d.state_name"),
                    "gender": _dist(cur, "d.gender"),
                    "ageGroup": _dist(cur, "d.age_group"),
                    "sec": _dist(cur, "d.sec_class"),
                }
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "57014":
            logger.warning("Main overview demographics timed out; returning fallback.")
            return {
                "category": "main",
                "totalRespondents": 0,
                "monthsAvailable": [],
                "monthsSelected": months_sel,
                "statesSelected": states_sel,
                "pipelineKpis": {"totalEAs": 0, "totalHouseholds": 0, "approvedHH": 0, "rejectedHH": 0},
                "distributions": {},
                "partial": True,
                "message": "Overview timed out. Try again after sync/QC finishes.",
            }
        if getattr(exc, "sqlstate", None) in {"42703", "42P01"}:
            logger.warning("Main overview demographics schema is still updating; returning fallback.", exc_info=True)
            return {
                "category": "main",
                "totalRespondents": 0,
                "monthsAvailable": [],
                "monthsSelected": months_sel,
                "statesSelected": states_sel,
                "pipelineKpis": {"totalEAs": 0, "totalHouseholds": 0, "approvedHH": 0, "rejectedHH": 0},
                "distributions": {},
                "partial": True,
                "message": "Overview schema is updating. Refresh after startup finishes.",
            }
        raise

    return {
        "category": "main",
        "totalRespondents": total_respondents,
        "monthsAvailable": months_available,
        "monthsSelected": months_sel,
        "statesSelected": states_sel,
        "pipelineKpis": pipeline_kpis,
        "distributions": distributions,
    }


def get_bht_overview(
    settings: Settings,
    user: AuthUser,
    category: str = "omnibus",
    months: list[str] | None = None,
    regions: list[str] | None = None,
    sectors: list[str] | None = None,
    categories: list[str] | None = None,
) -> dict[str, Any]:
    if not settings.database_url:
        return {
            "category": BHT_CATEGORY_PANEL_MAP["omnibus"],
            "monthsAvailable": [],
            "monthsSelected": months or [],
            "regionsSelected": regions or [],
            "sectorsAvailable": [],
            "sectorsSelected": sectors or [],
            "kpis": {"totalCases": 0, "categoryCases": 0, "omnibusAnswers": 0, "mediaFiles": 0},
            "statusKpis": {"totalSynced": 0, "approved": 0, "pendingApproval": 0, "cancelledRejected": 0},
            "months": [],
            "panels": [],
            "distributions": {},
        }

    selected_category_keys = [
        str(item).strip()
        for item in (categories or [])
        if str(item).strip() in BHT_CATEGORY_PANEL_MAP and str(item).strip() not in {"all"}
    ]
    if "omnibus" in selected_category_keys and len(selected_category_keys) > 1:
        selected_category_keys = []
    if selected_category_keys:
        if len(selected_category_keys) == 1:
            category_key = selected_category_keys[0]
            category_meta = BHT_CATEGORY_PANEL_MAP[category_key]
        else:
            category_key = "custom"
            category_meta = {"label": f"{len(selected_category_keys)} Categories", "panelCode": None}
    else:
        category_key = "all" if category == "all" else category if category in BHT_CATEGORY_PANEL_MAP else "omnibus"
        category_meta = {"label": "All Categories", "panelCode": None} if category_key == "all" else BHT_CATEGORY_PANEL_MAP[category_key]
    months_sel = [str(m).strip() for m in (months or []) if str(m).strip()]
    regions_sel = [str(r).strip() for r in (regions or []) if str(r).strip()]
    sectors_sel = [str(s).strip() for s in (sectors or []) if str(s).strip()]
    cache_key = (
        settings.main_survey_data_start_date or "",
        category_key,
        tuple(selected_category_keys),
        tuple(months_sel),
        tuple(regions_sel),
        tuple(sectors_sel),
    )
    cached = BHT_OVERVIEW_CACHE.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < BHT_OVERVIEW_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        _ensure_bht_overview_case_mart(settings)
        if not _bht_active_case_mart_ready(settings):
            payload = _get_bht_overview_from_clean(
                settings,
                category_key,
                category_meta,
                months_sel,
                regions_sel,
                sectors_sel,
                selected_category_keys,
            )
            BHT_OVERVIEW_CACHE[cache_key] = (time.monotonic(), payload)
            return payload
        payload = _get_bht_overview_from_case_mart(settings, category_key, category_meta, months_sel, regions_sel, sectors_sel, selected_category_keys)
        BHT_OVERVIEW_CACHE[cache_key] = (time.monotonic(), payload)
        return payload
    except Exception:
        logger.warning("Case-level BHT overview mart failed; falling back to legacy overview path.", exc_info=True)
        if settings.main_survey_data_start_date:
            payload = _empty_bht_overview_payload(
                category_meta,
                months_sel,
                regions_sel,
                sectors_sel,
                message="Active BHT overview data is still refreshing. Try again shortly.",
            )
            BHT_OVERVIEW_CACHE[cache_key] = (time.monotonic(), payload)
            return payload

    panel_code = category_meta["panelCode"]
    sector_labels = SECTOR_LABELS
    marts_fresh = _ensure_bht_overview_marts_fresh(settings)
    kpi_month_sql = " AND survey_month = ANY(%s) " if months_sel else ""

    if regions_sel or sectors_sel or (selected_category_keys and len(selected_category_keys) > 1):
        payload = _get_bht_overview_from_clean(settings, category_key, category_meta, months_sel, regions_sel, sectors_sel, selected_category_keys)
        BHT_OVERVIEW_CACHE[cache_key] = (time.monotonic(), payload)
        return payload

    if not marts_fresh:
        try:
            payload = _get_bht_overview_from_clean(settings, category_key, category_meta, months_sel, regions_sel, sectors_sel, selected_category_keys)
            BHT_OVERVIEW_CACHE[cache_key] = (time.monotonic(), payload)
            return payload
        except Exception:
            logger.warning("Clean-table BHT overview failed; falling back to mart tables.", exc_info=True)

    try:
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '25000ms'")
                cur.execute(
                    """
                    SELECT DISTINCT survey_month AS value
                    FROM mart.bht_category_kpi
                    WHERE survey_month IS NOT NULL
                    ORDER BY survey_month DESC
                    """
                )
                months_available = [str(r["value"]) for r in cur.fetchall() if r.get("value")]

                sector_params: list[Any] = []
                sector_join_sql = "" if category_key == "all" else "INNER JOIN mart.bht_map_point_category pc ON pc.case_id = m.case_id AND pc.category_slug = %s"
                if category_key != "all":
                    sector_params.append(category_key)
                sector_month_sql = ""
                if months_sel:
                    sector_month_sql = "AND m.survey_month = ANY(%s)"
                    sector_params.append(months_sel)
                cur.execute(
                    f"""
                    SELECT DISTINCT COALESCE(NULLIF(TRIM(m.record_sector), ''), NULLIF(TRIM(m.sector_code), '')) AS sector
                    FROM mart.bht_map_point m
                    {sector_join_sql}
                    WHERE COALESCE(NULLIF(TRIM(m.record_sector), ''), NULLIF(TRIM(m.sector_code), '')) IS NOT NULL
                    {sector_month_sql}
                    ORDER BY sector
                    """,
                    tuple(sector_params),
                )
                sectors_available = [_choice_label(sector_labels, r.get("sector")) for r in cur.fetchall() if r.get("sector")]

                kpi_category_sql = "" if category_key == "all" else "category_slug = %s AND"
                kpi_params: list[Any] = [] if category_key == "all" else [category_key]
                if months_sel:
                    kpi_params.append(months_sel)
                cur.execute(
                    f"""
                    SELECT
                        COALESCE(MAX(total_case_count), 0)::int AS total_cases,
                        COALESCE(SUM(category_case_count), 0)::int AS category_cases,
                        COALESCE(SUM(omnibus_answer_count), 0)::int AS omnibus_answers,
                        COALESCE(SUM(media_file_count), 0)::int AS media_files
                    FROM mart.bht_category_kpi
                    WHERE {kpi_category_sql} TRUE
                    {kpi_month_sql}
                    """,
                    tuple(kpi_params),
                )
                kpi_row = cur.fetchone() or {}
                total_cases = int(kpi_row.get("total_cases") or 0)
                category_cases = int(kpi_row.get("category_cases") or 0)
                if category_key == "all":
                    category_cases = total_cases
                omnibus_answers = int(kpi_row.get("omnibus_answers") or 0)
                media_files = int(kpi_row.get("media_files") or 0)

                status_params: list[Any] = []
                status_join_sql = "" if category_key == "all" else "INNER JOIN mart.bht_map_point_category pc ON pc.case_id = m.case_id AND pc.category_slug = %s"
                if category_key != "all":
                    status_params.append(category_key)
                status_month_sql = ""
                if months_sel:
                    status_month_sql = "AND m.survey_month = ANY(%s)"
                    status_params.append(months_sel)
                cur.execute(
                    f"""
                    SELECT
                        COUNT(DISTINCT m.case_id)::int AS total_synced,
                        COUNT(DISTINCT m.case_id) FILTER (WHERE LOWER(COALESCE(NULLIF(TRIM(m.approval_status), ''), 'pending')) IN ('approved', 'reviewed_approved'))::int AS approved,
                        COUNT(DISTINCT m.case_id) FILTER (WHERE LOWER(COALESCE(NULLIF(TRIM(m.approval_status), ''), 'pending')) IN ('rejected', 'reviewed_rejected', 'cancelled', 'canceled'))::int AS cancelled_rejected
                    FROM mart.bht_map_point m
                    {status_join_sql}
                    WHERE TRUE
                    {status_month_sql}
                    """,
                    tuple(status_params),
                )
                status_row = cur.fetchone() or {}
                status_total = int(status_row.get("total_synced") or 0)
                status_approved = int(status_row.get("approved") or 0)
                status_cancelled_rejected = int(status_row.get("cancelled_rejected") or 0)
                status_pending = max(status_total - status_approved - status_cancelled_rejected, 0)

                month_category_sql = "" if category_key == "all" else "category_slug = %s AND"
                month_params: list[Any] = [] if category_key == "all" else [category_key]
                if months_sel:
                    month_params.append(months_sel)
                cur.execute(
                    f"""
                    SELECT survey_month,
                           CASE WHEN %s THEN MAX(total_case_count)::int ELSE SUM(category_case_count)::int END AS cases
                    FROM mart.bht_category_kpi
                    WHERE {month_category_sql} TRUE
                    {kpi_month_sql}
                    GROUP BY survey_month
                    ORDER BY survey_month DESC
                    """,
                    tuple([category_key == "all", *month_params]),
                )
                month_rows = [{"surveyMonth": str(r["survey_month"]), "cases": int(r["cases"] or 0)} for r in cur.fetchall()]

                panel_params: list[Any] = []
                if months_sel:
                    panel_params.append(months_sel)
                panel_month_sql = "WHERE survey_month = ANY(%s)" if months_sel else ""
                cur.execute(
                    f"""
                    SELECT panel_code,
                           COALESCE(MAX(panel_label), panel_code) AS panel_label,
                           SUM(case_count)::int AS cases
                    FROM mart.bht_panel_summary
                    {panel_month_sql}
                    GROUP BY panel_code
                    ORDER BY
                      CASE panel_code
                        WHEN 'Panel_1' THEN 1 WHEN 'Panel_2' THEN 2 WHEN 'Panel_3' THEN 3
                        WHEN 'Panel_4' THEN 4 WHEN 'Panel_5' THEN 5 WHEN 'Panel_6' THEN 6
                        WHEN 'Panel_7' THEN 7 WHEN 'Panel_8' THEN 8 WHEN 'Panel_9' THEN 9
                        WHEN 'Panel_10' THEN 10 WHEN 'Panel_11' THEN 11 ELSE 99
                      END
                    """,
                    tuple(panel_params),
                )
                panel_rows = [
                    {
                        "panelCode": str(r["panel_code"]),
                        "panelLabel": BHT_PANEL_LABEL_BY_CODE.get(str(r["panel_code"]), str(r["panel_label"])),
                        "cases": int(r["cases"] or 0),
                    }
                    for r in cur.fetchall()
                ]

                dist_category_sql = "category_slug = 'omnibus'" if category_key == "all" else "category_slug = %s"
                dist_params: list[Any] = [] if category_key == "all" else [category_key]
                dist_month_sql = " AND survey_month = ANY(%s) " if months_sel else ""
                if months_sel:
                    dist_params.append(months_sel)
                region_dist_sql = " AND (distribution_key <> 'region' OR answer_label = ANY(%s)) " if regions_sel else ""
                if regions_sel:
                    dist_params.append(regions_sel)
                cur.execute(
                    f"""
                    WITH grouped AS (
                        SELECT
                            distribution_key,
                            MAX(distribution_title) AS distribution_title,
                            MAX(variable_name) AS variable_name,
                            answer_value,
                            MAX(answer_label) AS answer_label,
                            SUM(case_count)::int AS case_count
                        FROM mart.bht_overview_distribution
                        WHERE {dist_category_sql}
                        {dist_month_sql}
                        {region_dist_sql}
                        GROUP BY distribution_key, answer_value
                    ),
                    bases AS (
                        SELECT distribution_key, SUM(case_count)::int AS base_count
                        FROM grouped
                        GROUP BY distribution_key
                    )
                    SELECT
                        g.distribution_key,
                        g.distribution_title,
                        g.variable_name,
                        g.answer_value,
                        g.answer_label,
                        g.case_count,
                        b.base_count,
                        CASE WHEN b.base_count > 0 THEN ROUND((g.case_count::numeric / b.base_count::numeric) * 100, 2) ELSE 0 END AS pct
                    FROM grouped g
                    JOIN bases b ON b.distribution_key = g.distribution_key
                    ORDER BY g.distribution_key, g.case_count DESC
                    """,
                    tuple(dist_params),
                )
                distribution_rows: dict[str, Any] = {}
                for r in cur.fetchall():
                    key = str(r["distribution_key"])
                    bucket = distribution_rows.setdefault(
                        key,
                        {
                            "title": str(r["distribution_title"]),
                            "variable": str(r["variable_name"]),
                            "base": int(r["base_count"] or 0),
                            "rows": [],
                        },
                    )
                    if len(bucket["rows"]) < 20:
                        bucket["rows"].append(
                            {
                                "label": str(r["answer_label"]),
                                "value": int(r["case_count"] or 0),
                                "pct": float(r["pct"] or 0),
                            }
                        )
                if "sector" not in distribution_rows:
                    payload = _get_bht_overview_from_clean(settings, category_key, category_meta, months_sel, regions_sel, sectors_sel, selected_category_keys)
                    BHT_OVERVIEW_CACHE[cache_key] = (time.monotonic(), payload)
                    return payload
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "57014":
            return {
                "category": category_meta,
                "monthsAvailable": [],
                "monthsSelected": months_sel,
                "regionsSelected": regions_sel,
                "sectorsAvailable": [],
                "sectorsSelected": sectors_sel,
                "kpis": {"totalCases": 0, "categoryCases": 0, "omnibusAnswers": 0, "mediaFiles": 0},
                "statusKpis": {"totalSynced": 0, "approved": 0, "pendingApproval": 0, "cancelledRejected": 0},
                "months": [],
                "panels": [],
                "distributions": {},
                "partial": True,
                "message": "BHT overview timed out. Try again after sync finishes.",
            }
        raise

    payload = {
        "category": {"slug": category_key, **category_meta},
        "monthsAvailable": months_available,
        "monthsSelected": months_sel,
        "regionsSelected": regions_sel,
        "sectorsAvailable": sectors_available,
        "sectorsSelected": sectors_sel,
        "kpis": {
            "totalCases": total_cases,
            "categoryCases": category_cases,
            "omnibusAnswers": omnibus_answers,
            "mediaFiles": media_files,
        },
        "statusKpis": {
            "totalSynced": status_total,
            "approved": status_approved,
            "pendingApproval": status_pending,
            "cancelledRejected": status_cancelled_rejected,
        },
        "months": month_rows,
        "panels": panel_rows,
        "distributions": distribution_rows,
    }
    BHT_OVERVIEW_CACHE[cache_key] = (time.monotonic(), payload)
    return payload


def _get_bht_overview_from_case_mart(
    settings: Settings,
    category_key: str,
    category_meta: dict[str, Any],
    months_sel: list[str],
    regions_sel: list[str],
    sectors_sel: list[str],
    selected_category_keys: list[str] | None = None,
) -> dict[str, Any]:
    selected_keys = [key for key in (selected_category_keys or []) if key in BHT_CATEGORY_PANEL_MAP and key != "all"]
    region_values = _bht_region_filter_values(regions_sel)
    sector_labels = SECTOR_LABELS
    sector_values = _choice_filter_values(sector_labels, sectors_sel)

    def build_where(*, include_category: bool = True, include_sector: bool = True) -> tuple[str, list[Any]]:
        clauses = ["1 = 1"]
        params: list[Any] = []
        active_scope_sql, active_scope_params = main_row_scope_clause(settings, "", prefix="AND")
        if active_scope_sql:
            clauses.append(active_scope_sql.removeprefix("AND ").strip())
            params.extend(active_scope_params)
        if months_sel:
            clauses.append("survey_month = ANY(%s)")
            params.append(months_sel)
        if include_category:
            if selected_keys:
                clauses.append("category_slugs && %s::text[]")
                params.append(selected_keys)
            elif category_key != "all":
                clauses.append("category_slugs @> ARRAY[%s]::text[]")
                params.append(category_key)
        if region_values:
            clauses.append("(region_label = ANY(%s) OR region_code = ANY(%s))")
            params.extend([regions_sel, region_values])
        if include_sector and sector_values:
            clauses.append("(sector_label = ANY(%s) OR sector_code = ANY(%s))")
            params.extend([sectors_sel, sector_values])
        return " AND ".join(clauses), params

    where_sql, where_params = build_where()
    sector_option_where, sector_option_params = build_where(include_sector=False)
    panel_where_sql, panel_where_params = build_where(include_category=False)

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '15000ms'")
            month_scope_sql, month_scope_params = main_row_scope_clause(settings, "", prefix="AND")
            cur.execute(
                f"""
                SELECT DISTINCT survey_month AS value
                FROM mart.bht_case_overview_dim
                WHERE survey_month IS NOT NULL
                {month_scope_sql}
                ORDER BY survey_month DESC
                """,
                tuple(month_scope_params),
            )
            months_available = [str(r["value"]) for r in cur.fetchall() if r.get("value")]

            cur.execute(
                f"""
                SELECT DISTINCT sector_label
                FROM mart.bht_case_overview_dim
                WHERE {sector_option_where}
                  AND NULLIF(TRIM(COALESCE(sector_label, '')), '') IS NOT NULL
                ORDER BY sector_label
                """,
                sector_option_params,
            )
            sectors_available = [str(r["sector_label"]) for r in cur.fetchall() if r.get("sector_label")]

            cur.execute(
                f"""
                SELECT
                    COUNT(DISTINCT d.case_id)::int AS total_synced,
                    COUNT(DISTINCT d.case_id) FILTER (
                        WHERE LOWER(COALESCE(NULLIF(TRIM(mq.approval_stage), ''), 'pending')) IN ('approved', 'reviewed_approved')
                    )::int AS approved,
                    COUNT(DISTINCT d.case_id) FILTER (
                        WHERE LOWER(COALESCE(NULLIF(TRIM(mq.approval_stage), ''), 'pending')) IN ('rejected', 'reviewed_rejected', 'cancelled', 'canceled')
                    )::int AS cancelled_rejected
                FROM mart.bht_case_overview_dim d
                INNER JOIN mart.main_case_queue mq ON mq.case_id = d.case_id
                WHERE {where_sql.replace("survey_month", "d.survey_month").replace("category_slugs", "d.category_slugs").replace("region_label", "d.region_label").replace("region_code", "d.region_code").replace("sector_label", "d.sector_label").replace("sector_code", "d.sector_code").replace("start_time", "d.start_time").replace("submitted_at", "d.submitted_at")}
                """,
                where_params,
            )
            status_row = cur.fetchone() or {}
            status_total = int(status_row.get("total_synced") or 0)
            status_approved = int(status_row.get("approved") or 0)
            status_cancelled_rejected = int(status_row.get("cancelled_rejected") or 0)
            status_pending = max(status_total - status_approved - status_cancelled_rejected, 0)

            total_where_sql, total_where_params = build_where(include_category=False)
            cur.execute(
                f"""
                SELECT COUNT(*)::int AS total_cases
                FROM mart.bht_case_overview_dim
                WHERE {total_where_sql}
                """,
                total_where_params,
            )
            total_cases = int((cur.fetchone() or {}).get("total_cases") or 0)
            category_cases = status_total

            cur.execute(
                f"""
                SELECT survey_month, COUNT(*)::int AS cases
                FROM mart.bht_case_overview_dim
                WHERE {where_sql}
                GROUP BY survey_month
                ORDER BY survey_month DESC
                """,
                where_params,
            )
            month_rows = [{"surveyMonth": str(r["survey_month"]), "cases": int(r["cases"] or 0)} for r in cur.fetchall()]

            cur.execute(
                f"""
                SELECT u.slug AS category_slug, COUNT(DISTINCT case_id)::int AS cases
                FROM mart.bht_case_overview_dim
                CROSS JOIN LATERAL UNNEST(category_slugs) AS u(slug)
                WHERE {panel_where_sql}
                  AND u.slug <> 'omnibus'
                GROUP BY u.slug
                ORDER BY cases DESC, slug
                """,
                panel_where_params,
            )
            panel_rows = []
            for row in cur.fetchall():
                slug = str(row["category_slug"])
                meta = BHT_CATEGORY_PANEL_MAP.get(slug, {})
                panel_rows.append(
                    {
                        "panelCode": str(meta.get("panelCode") or slug),
                        "panelLabel": str(meta.get("label") or slug.replace("-", " ").title()),
                        "cases": int(row["cases"] or 0),
                    }
                )

            distribution_rows: dict[str, Any] = {}
            cur.execute(
                f"""
                WITH scoped AS (
                    SELECT *
                    FROM mart.bht_case_overview_dim
                    WHERE {where_sql}
                ),
                unpivoted AS (
                    SELECT 'region' AS distribution_key, 'Region' AS distribution_title, 'region_code' AS variable_name,
                           COALESCE(NULLIF(TRIM(region_code), ''), '(No response)') AS answer_value,
                           COALESCE(NULLIF(TRIM(region_label), ''), '(No response)') AS answer_label
                    FROM scoped
                    UNION ALL
                    SELECT 'sector', 'Sector', 'sector_code',
                           COALESCE(NULLIF(TRIM(sector_code), ''), '(No response)'),
                           COALESCE(NULLIF(TRIM(sector_label), ''), '(No response)')
                    FROM scoped
                    UNION ALL
                    SELECT 'sec', 'SEC', 'sec_value',
                           COALESCE(NULLIF(TRIM(sec_value), ''), '(No response)'),
                           COALESCE(NULLIF(TRIM(sec_label), ''), '(No response)')
                    FROM scoped
                    UNION ALL
                    SELECT 'week', 'Week', 'week_value',
                           COALESCE(NULLIF(TRIM(week_value), ''), '(No response)'),
                           COALESCE(NULLIF(TRIM(week_label), ''), '(No response)')
                    FROM scoped
                    UNION ALL
                    SELECT 'gender', 'Gender', 'gender_value',
                           COALESCE(NULLIF(TRIM(gender_value), ''), '(No response)'),
                           COALESCE(NULLIF(TRIM(gender_label), ''), '(No response)')
                    FROM scoped
                    UNION ALL
                    SELECT 'age', 'Age', 'age_value',
                           COALESCE(NULLIF(TRIM(age_value), ''), '(No response)'),
                           COALESCE(NULLIF(TRIM(age_label), ''), '(No response)')
                    FROM scoped
                ),
                grouped AS (
                    SELECT distribution_key, distribution_title, variable_name, answer_value, answer_label, COUNT(*)::int AS case_count
                    FROM unpivoted
                    GROUP BY distribution_key, distribution_title, variable_name, answer_value, answer_label
                ),
                bases AS (
                    SELECT distribution_key, SUM(case_count)::int AS base_count
                    FROM grouped
                    GROUP BY distribution_key
                ),
                ranked AS (
                    SELECT
                        g.*,
                        b.base_count,
                        CASE WHEN b.base_count > 0 THEN ROUND((g.case_count::numeric / b.base_count::numeric) * 100, 2) ELSE 0 END AS pct,
                        ROW_NUMBER() OVER (PARTITION BY g.distribution_key ORDER BY g.case_count DESC, g.answer_label) AS rn
                    FROM grouped g
                    JOIN bases b ON b.distribution_key = g.distribution_key
                )
                SELECT *
                FROM ranked
                WHERE rn <= 20
                ORDER BY distribution_key, rn
                """,
                where_params,
            )
            for row in cur.fetchall():
                key = str(row["distribution_key"])
                bucket = distribution_rows.setdefault(
                    key,
                    {
                        "title": str(row["distribution_title"]),
                        "variable": str(row["variable_name"]),
                        "base": int(row["base_count"] or 0),
                        "rows": [],
                    },
                )
                bucket["rows"].append(
                    {
                        "label": str(row["answer_label"]),
                        "value": int(row["case_count"] or 0),
                        "pct": float(row["pct"] or 0),
                    }
                )

    return {
        "category": {"slug": category_key, **category_meta},
        "monthsAvailable": months_available,
        "monthsSelected": months_sel,
        "regionsSelected": regions_sel,
        "sectorsAvailable": sectors_available,
        "sectorsSelected": sectors_sel,
        "kpis": {
            "totalCases": total_cases,
            "categoryCases": category_cases,
            "omnibusAnswers": 0,
            "mediaFiles": 0,
        },
        "statusKpis": {
            "totalSynced": status_total,
            "approved": status_approved,
            "pendingApproval": status_pending,
            "cancelledRejected": status_cancelled_rejected,
        },
        "months": month_rows,
        "panels": panel_rows,
        "distributions": distribution_rows,
    }


def build_bht_overview_kpi_export_dataframe(
    settings: Settings,
    kpi: str,
    category: str = "all",
    months: list[str] | None = None,
    regions: list[str] | None = None,
    sectors: list[str] | None = None,
    categories: list[str] | None = None,
) -> pd.DataFrame:
    normalized_kpi = str(kpi or "").strip().lower().replace("-", "_")
    if normalized_kpi not in {"total_synced", "approved", "pending_approval", "cancelled_rejected"}:
        raise HTTPException(status_code=400, detail="Unsupported Overview KPI export.")

    selected_category_keys = [
        str(item).strip()
        for item in (categories or [])
        if str(item).strip() in BHT_CATEGORY_PANEL_MAP and str(item).strip() not in {"all"}
    ]
    if "omnibus" in selected_category_keys and len(selected_category_keys) > 1:
        selected_category_keys = []

    category_key = "all" if category == "all" else category if category in BHT_CATEGORY_PANEL_MAP else "omnibus"
    if selected_category_keys and len(selected_category_keys) == 1:
        category_key = selected_category_keys[0]

    months_sel = [str(m).strip() for m in (months or []) if str(m).strip()]
    regions_sel = [str(r).strip() for r in (regions or []) if str(r).strip()]
    sectors_sel = [str(s).strip() for s in (sectors or []) if str(s).strip()]
    region_values = _bht_region_filter_values(regions_sel)
    sector_values = _choice_filter_values(SECTOR_LABELS, sectors_sel)

    where_parts = ["TRUE"]
    params: list[Any] = []
    active_scope_sql, active_scope_params = main_row_scope_clause(settings, "bht", prefix="AND")
    if active_scope_sql:
        where_parts.append(active_scope_sql.removeprefix("AND ").strip())
        params.extend(active_scope_params)
    if months_sel:
        where_parts.append("bht.survey_month = ANY(%s)")
        params.append(months_sel)
    if selected_category_keys:
        where_parts.append("bht.category_slugs && %s::text[]")
        params.append(selected_category_keys)
    elif category_key not in {"all", "omnibus"}:
        where_parts.append("bht.category_slugs @> ARRAY[%s]::text[]")
        params.append(category_key)
    if region_values:
        where_parts.append("(bht.region_label = ANY(%s) OR bht.region_code = ANY(%s))")
        params.extend([regions_sel, region_values])
    if sector_values:
        where_parts.append("(bht.sector_label = ANY(%s) OR bht.sector_code = ANY(%s))")
        params.append(sectors_sel)
        params.append(sector_values)

    approved_statuses = ["approved", "reviewed_approved"]
    cancelled_statuses = ["rejected", "reviewed_rejected", "cancelled", "canceled"]
    if normalized_kpi == "approved":
        where_parts.append("LOWER(COALESCE(NULLIF(TRIM(mq.approval_stage), ''), 'pending')) = ANY(%s)")
        params.append(approved_statuses)
    elif normalized_kpi == "pending_approval":
        where_parts.append("LOWER(COALESCE(NULLIF(TRIM(mq.approval_stage), ''), 'pending')) <> ALL(%s)")
        params.append([*approved_statuses, *cancelled_statuses])
    elif normalized_kpi == "cancelled_rejected":
        where_parts.append("LOWER(COALESCE(NULLIF(TRIM(mq.approval_stage), ''), 'pending')) = ANY(%s)")
        params.append(cancelled_statuses)

    where_sql = " AND ".join(where_parts)
    columns = [
        "Submission Key",
        "Survey Month",
        "Start Date/Time",
        "Submission Date/Time",
        "Region",
        "Sector",
        "Interviewer",
        "Username",
        "Selected Panels",
        "Approval Status",
        "Approved By",
        "Cancellation Reason",
    ]

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '15000ms'")
            cur.execute(
                f"""
                WITH export_cases AS (
                    SELECT
                        mq.case_id,
                        mq.submission_key,
                        bht.survey_month,
                        mq.start_time,
                        mq.submitted_at,
                        bht.region_label,
                        bht.sector_label,
                        mq.interviewer_id,
                        mq.username,
                        mq.selected_panel_labels,
                        mq.approval_stage,
                        mq.approved_by
                    FROM mart.bht_case_overview_dim bht
                    INNER JOIN mart.main_case_queue mq ON mq.case_id = bht.case_id
                    WHERE {where_sql}
                ),
                latest_status_actors AS (
                    SELECT DISTINCT ON (ec.case_id)
                        ec.case_id,
                        h.new_status,
                        CASE
                            WHEN NULLIF(TRIM(ua.username), '') IS NOT NULL AND NULLIF(TRIM(ua.full_name), '') IS NOT NULL THEN CONCAT(ua.username, ': ', ua.full_name)
                            WHEN NULLIF(TRIM(ua.username), '') IS NOT NULL THEN ua.username
                            WHEN NULLIF(TRIM(ua.full_name), '') IS NOT NULL THEN ua.full_name
                            WHEN COALESCE(h.change_note, '') ILIKE 'Automatically approved for export:%%' THEN 'System auto-approval'
                            ELSE NULL
                        END AS status_actor,
                        NULLIF(TRIM(h.change_note), '') AS status_reason
                    FROM export_cases ec
                    INNER JOIN qc.case_status_history h
                        ON h.instrument_code = 'main'
                       AND LOWER(COALESCE(NULLIF(TRIM(ec.approval_stage), ''), 'pending')) = LOWER(COALESCE(NULLIF(TRIM(h.new_status), ''), ''))
                       AND h.submission_key = ec.submission_key
                    LEFT JOIN app.user_account ua ON ua.user_id = h.changed_by_user_id
                    ORDER BY ec.case_id, h.changed_at DESC NULLS LAST
                ),
                latest_callback_reasons AS (
                    SELECT DISTINCT ON (cb.case_id)
                        cb.case_id,
                        NULLIF(TRIM(cb.outcome_note), '') AS callback_reason
                    FROM qc.callback_outcome cb
                    INNER JOIN export_cases ec ON ec.case_id = cb.case_id
                    WHERE NULLIF(TRIM(COALESCE(cb.outcome_note, '')), '') IS NOT NULL
                    ORDER BY cb.case_id, cb.updated_at DESC NULLS LAST, cb.completed_at DESC NULLS LAST, cb.created_at DESC NULLS LAST
                ),
                latest_audio_reasons AS (
                    SELECT DISTINCT ON (al.case_id)
                        al.case_id,
                        NULLIF(TRIM(al.reviewer_note), '') AS audio_reason
                    FROM clean.audio_listening al
                    INNER JOIN export_cases ec ON ec.case_id = al.case_id
                    WHERE NULLIF(TRIM(COALESCE(al.reviewer_note, '')), '') IS NOT NULL
                    ORDER BY al.case_id, al.reviewed_at DESC NULLS LAST, al.created_at DESC NULLS LAST
                )
                SELECT
                    ec.submission_key AS "Submission Key",
                    ec.survey_month AS "Survey Month",
                    ec.start_time AS "Start Date/Time",
                    ec.submitted_at AS "Submission Date/Time",
                    ec.region_label AS "Region",
                    ec.sector_label AS "Sector",
                    ec.interviewer_id AS "Interviewer",
                    ec.username AS "Username",
                    ec.selected_panel_labels AS "Selected Panels",
                    ec.approval_stage AS "Approval Status",
                    COALESCE(NULLIF(TRIM(lsa.status_actor), ''), NULLIF(TRIM(ec.approved_by), '')) AS "Approved By",
                    CASE
                        WHEN LOWER(COALESCE(NULLIF(TRIM(ec.approval_stage), ''), 'pending')) IN ('rejected', 'reviewed_rejected', 'cancelled', 'canceled')
                            THEN COALESCE(lsa.status_reason, lcr.callback_reason, lar.audio_reason)
                        ELSE NULL
                    END AS "Cancellation Reason"
                FROM export_cases ec
                LEFT JOIN latest_status_actors lsa ON lsa.case_id = ec.case_id
                LEFT JOIN latest_callback_reasons lcr ON lcr.case_id = ec.case_id
                LEFT JOIN latest_audio_reasons lar ON lar.case_id = ec.case_id
                ORDER BY ec.submitted_at DESC NULLS LAST, ec.submission_key
                """,
                tuple(params),
            )
            rows = cur.fetchall()

    return pd.DataFrame(rows, columns=columns)


def _bht_city_label(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return BHT_OVERVIEW_DISTRIBUTIONS["region"]["labels"].get(raw, raw)


def _parse_surveycto_gps(value: Any) -> tuple[float | None, float | None]:
    raw = str(value or "").strip()
    if not raw:
        return None, None
    parts = raw.replace(",", " ").split()
    if len(parts) < 2:
        return None, None
    try:
        lat = float(parts[0])
        lng = float(parts[1])
    except (TypeError, ValueError):
        return None, None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None, None
    return lat, lng


def _normalize_choice_code(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.endswith(".0"):
        return raw[:-2]
    return raw


def _choice_labels_for_variable(settings: Settings, variable_name: str) -> dict[str, str]:
    if variable_name == "Sector":
        return {**SECTOR_LABELS, **{f"{code}.0": label for code, label in SECTOR_LABELS.items()}}

    labels = _choice_labels_for_variable_from_xlsform(str(settings.root_dir), variable_name)
    if not settings.database_url:
        return labels
    try:
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT choice_list_name
                    FROM reference.xlsform_question
                    WHERE instrument_code = 'main'
                      AND variable_name = %s
                    LIMIT 1
                    """,
                    (variable_name,),
                )
                question = cur.fetchone() or {}
                list_name = str(question.get("choice_list_name") or "").strip()
                if not list_name:
                    return labels
                cur.execute(
                    """
                    SELECT choice_code, choice_label
                    FROM reference.xlsform_choice
                    WHERE instrument_code = 'main'
                      AND list_name = %s
                    """,
                    (list_name,),
                )
                for row in cur.fetchall():
                    code = str(row.get("choice_code") or "").strip()
                    label = str(row.get("choice_label") or "").strip()
                    if not code or not label:
                        continue
                    labels[code] = label
                    normalized = _normalize_choice_code(code)
                    labels[normalized] = label
                    labels[f"{normalized}.0"] = label
                return labels
    except Exception:
        logger.debug("Unable to load choice labels for %s", variable_name, exc_info=True)
        return labels


@lru_cache(maxsize=8)
def _choice_labels_for_variable_from_xlsform(root_dir: str, variable_name: str) -> dict[str, str]:
    xlsform_dir = Path(root_dir) / "data" / "monthly_xlsform_dictionary"
    dictionary_files = sorted(
        (path for path in xlsform_dir.glob("*.xlsx") if not path.name.startswith("~$")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not dictionary_files:
        return {}
    try:
        survey_df = pd.read_excel(dictionary_files[0], sheet_name="survey").fillna("")
        choices_df = pd.read_excel(dictionary_files[0], sheet_name="choices").fillna("")
    except Exception:
        return {}
    if not {"name", "type"}.issubset(set(str(col) for col in survey_df.columns)) or not {"list_name", "name", "label"}.issubset(set(str(col) for col in choices_df.columns)):
        return {}
    list_name = ""
    for row in survey_df.to_dict(orient="records"):
        if str(row.get("name") or "").strip() != variable_name:
            continue
        qtype = str(row.get("type") or "").strip()
        list_name = qtype.split(None, 1)[1].strip() if " " in qtype else ""
        break
    if not list_name:
        return {}
    labels: dict[str, str] = {}
    for row in choices_df.to_dict(orient="records"):
        if str(row.get("list_name") or "").strip() != list_name:
            continue
        code = str(row.get("name") or "").strip()
        label = str(row.get("label") or "").strip()
        if not code or not label:
            continue
        labels[code] = label
        normalized = _normalize_choice_code(code)
        labels[normalized] = label
        labels[f"{normalized}.0"] = label
    return labels


def _choice_label(labels: dict[str, str], value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return labels.get(raw) or labels.get(_normalize_choice_code(raw)) or raw


def _choice_filter_values(labels: dict[str, str], selected: list[str]) -> list[str]:
    reverse = {label: code for code, label in labels.items()}
    values: set[str] = set()
    for item in selected:
        raw = str(item or "").strip()
        if not raw:
            continue
        values.add(raw)
        if raw in labels:
            values.add(labels[raw])
        if raw in reverse:
            values.add(reverse[raw])
            values.add(_normalize_choice_code(reverse[raw]))
    return sorted(values)


def _normalize_bht_week(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.lower().replace("_", " ").replace("-", " ")
    match = re.search(r"([1-4])", normalized)
    if match:
        return f"Week {match.group(1)}"
    return raw


def _bht_region_filter_values(regions: list[str]) -> list[str]:
    labels = BHT_OVERVIEW_DISTRIBUTIONS["region"]["labels"]
    reverse = {label: code for code, label in labels.items()}
    values: set[str] = set()
    for region in regions:
        raw = str(region or "").strip()
        if not raw:
            continue
        values.add(raw)
        if raw in reverse:
            values.add(reverse[raw])
    return sorted(values)


def _get_bht_overview_from_clean(
    settings: Settings,
    category_key: str,
    category_meta: dict[str, Any],
    months_sel: list[str],
    regions_sel: list[str],
    sectors_sel: list[str],
    selected_category_keys: list[str] | None = None,
) -> dict[str, Any]:
    panel_code = category_meta.get("panelCode")
    selected_panel_codes = [
        str(BHT_CATEGORY_PANEL_MAP[key]["panelCode"])
        for key in (selected_category_keys or [])
        if key in BHT_CATEGORY_PANEL_MAP and BHT_CATEGORY_PANEL_MAP[key].get("panelCode")
    ]
    region_values = _bht_region_filter_values(regions_sel)
    sector_labels = _choice_labels_for_variable(settings, "Sector")
    sector_values = _choice_filter_values(sector_labels, sectors_sel)
    base_clauses = [
        "NOT EXISTS (SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = m.submission_key)"
    ]
    base_params: list[Any] = []
    active_scope_sql, active_scope_params = main_case_scope_clause(settings, "m")
    if active_scope_sql:
        base_clauses.append(active_scope_sql.removeprefix("AND ").strip())
        base_params.extend(active_scope_params)
    if months_sel:
        base_clauses.append("m.survey_month = ANY(%s)")
        base_params.append(months_sel)
    if region_values:
        base_clauses.append("m.record->>'City_1' = ANY(%s)")
        base_params.append(region_values)
    if sector_values:
        base_clauses.append("COALESCE(NULLIF(TRIM(m.record->>'Sector'), ''), '(No response)') = ANY(%s)")
        base_params.append(sector_values)
    if selected_panel_codes:
        base_clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM clean.main_case_panel p
                WHERE p.case_id = m.case_id
                  AND p.panel_code = ANY(%s)
                  AND COALESCE(p.is_selected, TRUE)
            )
            """
        )
        base_params.append(selected_panel_codes)
    elif category_key not in {"all", "omnibus"} and panel_code:
        base_clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM clean.main_case_panel p
                WHERE p.case_id = m.case_id
                  AND p.panel_code = %s
                  AND COALESCE(p.is_selected, TRUE)
            )
            """
        )
        base_params.append(panel_code)
    base_where = " AND ".join(base_clauses)

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '25000ms'")
            month_scope_sql, month_scope_params = main_case_scope_clause(settings, "m")
            cur.execute(
                f"""
                SELECT DISTINCT survey_month AS value
                FROM clean.main_case m
                WHERE survey_month IS NOT NULL
                {month_scope_sql}
                ORDER BY survey_month DESC
                """,
                tuple(month_scope_params),
            )
            months_available = [str(r["value"]) for r in cur.fetchall() if r.get("value")]

            sector_option_clauses = [
                "NOT EXISTS (SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = m.submission_key)",
                "m.record ? 'Sector'",
                "NULLIF(TRIM(m.record->>'Sector'), '') IS NOT NULL",
            ]
            sector_option_params: list[Any] = []
            sector_scope_sql, sector_scope_params = main_case_scope_clause(settings, "m")
            if sector_scope_sql:
                sector_option_clauses.append(sector_scope_sql.removeprefix("AND ").strip())
                sector_option_params.extend(sector_scope_params)
            if months_sel:
                sector_option_clauses.append("m.survey_month = ANY(%s)")
                sector_option_params.append(months_sel)
            if region_values:
                sector_option_clauses.append("m.record->>'City_1' = ANY(%s)")
                sector_option_params.append(region_values)
            if selected_panel_codes:
                sector_option_clauses.append(
                    """
                    EXISTS (
                        SELECT 1
                        FROM clean.main_case_panel p
                        WHERE p.case_id = m.case_id
                          AND p.panel_code = ANY(%s)
                          AND COALESCE(p.is_selected, TRUE)
                    )
                    """
                )
                sector_option_params.append(selected_panel_codes)
            elif category_key not in {"all", "omnibus"} and panel_code:
                sector_option_clauses.append(
                    """
                    EXISTS (
                        SELECT 1
                        FROM clean.main_case_panel p
                        WHERE p.case_id = m.case_id
                          AND p.panel_code = %s
                          AND COALESCE(p.is_selected, TRUE)
                    )
                    """
                )
                sector_option_params.append(panel_code)
            cur.execute(
                f"""
                SELECT DISTINCT TRIM(m.record->>'Sector') AS sector
                FROM clean.main_case m
                WHERE {" AND ".join(sector_option_clauses)}
                ORDER BY sector
                """,
                tuple(sector_option_params),
            )
            sectors_available = [_choice_label(sector_labels, r.get("sector")) for r in cur.fetchall() if r.get("sector")]

            total_params: list[Any] = []
            total_month_sql = ""
            total_scope_sql, total_scope_params = main_case_scope_clause(settings, "m")
            total_params.extend(total_scope_params)
            if months_sel:
                total_month_sql = "AND m.survey_month = ANY(%s)"
                total_params.append(months_sel)
            cur.execute(
                f"""
                WITH scoped AS (
                    SELECT m.case_id, m.survey_month, m.approval_stage
                    FROM clean.main_case m
                    WHERE {base_where}
                )
                SELECT
                    (
                        SELECT COUNT(*)::int
                        FROM clean.main_case m
                        WHERE NOT EXISTS (
                            SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = m.submission_key
                        )
                        {total_scope_sql}
                        {total_month_sql}
                    ) AS total_cases,
                    COUNT(DISTINCT scoped.case_id)::int AS category_cases,
                    (
                        SELECT COUNT(*)::int
                        FROM clean.main_case_answer a
                        INNER JOIN scoped s ON s.case_id = a.case_id
                        WHERE a.answer_scope = 'omnibus'
                    ) AS omnibus_answers,
                    (
                        SELECT COUNT(*)::int
                        FROM clean.main_case_media media
                        INNER JOIN scoped s ON s.case_id = media.case_id
                    ) AS media_files,
                    COUNT(DISTINCT scoped.case_id) FILTER (WHERE LOWER(COALESCE(NULLIF(TRIM(scoped.approval_stage), ''), 'pending')) IN ('approved', 'reviewed_approved'))::int AS approved_cases,
                    COUNT(DISTINCT scoped.case_id) FILTER (WHERE LOWER(COALESCE(NULLIF(TRIM(scoped.approval_stage), ''), 'pending')) IN ('rejected', 'reviewed_rejected', 'cancelled', 'canceled'))::int AS cancelled_rejected_cases
                FROM scoped
                """,
                tuple(base_params + total_params),
            )
            kpi_row = cur.fetchone() or {}
            total_cases = int(kpi_row.get("total_cases") or 0)
            category_cases = int(kpi_row.get("category_cases") or 0)
            status_total = category_cases
            status_approved = int(kpi_row.get("approved_cases") or 0)
            status_cancelled_rejected = int(kpi_row.get("cancelled_rejected_cases") or 0)
            status_pending = max(status_total - status_approved - status_cancelled_rejected, 0)

            cur.execute(
                f"""
                SELECT m.survey_month, COUNT(DISTINCT m.case_id)::int AS cases
                FROM clean.main_case m
                WHERE {base_where}
                GROUP BY m.survey_month
                ORDER BY m.survey_month DESC
                """,
                tuple(base_params),
            )
            month_rows = [{"surveyMonth": str(r["survey_month"]), "cases": int(r["cases"] or 0)} for r in cur.fetchall()]

            panel_params: list[Any] = []
            panel_month_sql = ""
            panel_category_sql = ""
            if months_sel:
                panel_month_sql = "AND p.survey_month = ANY(%s)"
                panel_params.append(months_sel)
            if selected_panel_codes:
                panel_category_sql = "AND p.panel_code = ANY(%s)"
                panel_params.append(selected_panel_codes)
            cur.execute(
                f"""
                SELECT p.panel_code,
                       COALESCE(MAX(p.panel_label), p.panel_code) AS panel_label,
                       COUNT(DISTINCT p.case_id)::int AS cases
                FROM clean.main_case_panel p
                WHERE COALESCE(p.is_selected, TRUE)
                {panel_month_sql}
                {panel_category_sql}
                GROUP BY p.panel_code
                ORDER BY
                  CASE p.panel_code
                    WHEN 'Panel_1' THEN 1 WHEN 'Panel_2' THEN 2 WHEN 'Panel_3' THEN 3
                    WHEN 'Panel_4' THEN 4 WHEN 'Panel_5' THEN 5 WHEN 'Panel_6' THEN 6
                    WHEN 'Panel_7' THEN 7 WHEN 'Panel_8' THEN 8 WHEN 'Panel_9' THEN 9
                    WHEN 'Panel_10' THEN 10 WHEN 'Panel_11' THEN 11 ELSE 99
                  END
                """,
                tuple(panel_params),
            )
            panel_rows = [
                {
                    "panelCode": str(r["panel_code"]),
                    "panelLabel": BHT_PANEL_LABEL_BY_CODE.get(str(r["panel_code"]), str(r["panel_label"])),
                    "cases": int(r["cases"] or 0),
                }
                for r in cur.fetchall()
            ]

            distributions: dict[str, Any] = {}
            for distribution_key, meta in BHT_OVERVIEW_DISTRIBUTIONS.items():
                variable = str(meta["variable"])
                cur.execute(
                    f"""
                    WITH values AS (
                        SELECT
                            m.case_id,
                            COALESCE(NULLIF(TRIM(m.record->>%s), ''), '(No response)') AS answer_value
                        FROM clean.main_case m
                        WHERE {base_where}
                          AND m.record ? %s
                    )
                    SELECT answer_value, COUNT(DISTINCT case_id)::int AS case_count
                    FROM values
                    GROUP BY answer_value
                    ORDER BY case_count DESC
                    LIMIT 20
                    """,
                    tuple([variable, *base_params, variable]),
                )
                rows = cur.fetchall()
                base_count = sum(int(r["case_count"] or 0) for r in rows)
                label_map = sector_labels if variable == "Sector" else meta.get("labels") or {}
                distributions[distribution_key] = {
                    "title": str(meta["title"]),
                    "variable": variable,
                    "base": base_count,
                    "rows": [
                        {
                            "label": str(label_map.get(str(r["answer_value"]).replace(".0", ""), r["answer_value"])),
                            "value": int(r["case_count"] or 0),
                            "pct": round((int(r["case_count"] or 0) / base_count) * 100, 2) if base_count else 0,
                        }
                        for r in rows
                    ],
                }

    return {
        "category": {"slug": category_key, **category_meta},
        "monthsAvailable": months_available,
        "monthsSelected": months_sel,
        "regionsSelected": regions_sel,
        "sectorsAvailable": sectors_available,
        "sectorsSelected": sectors_sel,
        "kpis": {
            "totalCases": total_cases,
            "categoryCases": category_cases,
            "omnibusAnswers": int(kpi_row.get("omnibus_answers") or 0),
            "mediaFiles": int(kpi_row.get("media_files") or 0),
        },
        "statusKpis": {
            "totalSynced": status_total,
            "approved": status_approved,
            "pendingApproval": status_pending,
            "cancelledRejected": status_cancelled_rejected,
        },
        "months": month_rows,
        "panels": panel_rows,
        "distributions": distributions,
    }


@lru_cache(maxsize=4)
def _load_bau5a_choice_labels(dictionary_path: str) -> dict[str, dict[str, str]]:
    if not dictionary_path:
        return {}
    path = Path(dictionary_path)
    if not path.exists():
        return {}

    try:
        survey_df = pd.read_excel(path, sheet_name="survey")
        choices_df = pd.read_excel(path, sheet_name="choices")
    except Exception:
        logger.warning("Unable to load BAU5a labels from %s", path, exc_info=True)
        return {}

    labels_by_prefix: dict[str, dict[str, str]] = {}
    survey_rows = survey_df[survey_df["name"].astype(str).str.fullmatch(r"[A-Z]+_BAU5a", case=False, na=False)]
    for _, row in survey_rows.iterrows():
        variable_name = str(row.get("name") or "").strip()
        if "_" not in variable_name:
            continue
        prefix = variable_name.split("_", 1)[0].upper()
        raw_type = str(row.get("type") or "").strip()
        if " " not in raw_type:
            continue
        list_name = raw_type.split(" ", 1)[1].strip()
        choices = choices_df[choices_df["list_name"].astype(str).eq(list_name)]
        label_map: dict[str, str] = {}
        for _, choice in choices.iterrows():
            code = _normalize_choice_code(choice.get("name"))
            label = str(choice.get("label") or "").strip()
            if code and label and label.lower() != "nan":
                label_map[code] = label
        if label_map:
            labels_by_prefix[prefix] = label_map
    return labels_by_prefix


def _extract_bau5a_answers(record: dict[str, Any], category_key: str) -> list[str]:
    prefix = BHT_CATEGORY_BAU5A_PREFIX.get(category_key)
    if not prefix:
        return []
    try:
        dictionary_path = str(load_main_survey_pipeline_config().dictionary_file)
    except Exception:
        dictionary_path = ""
    labels = _load_bau5a_choice_labels(dictionary_path).get(prefix, {})
    selected: list[str] = []
    marker = f"{prefix}_BAU5a_"
    for key, value in record.items():
        if not str(key).startswith(marker):
            continue
        if str(value or "").strip() not in {"1", "1.0", "true", "True", "yes", "Yes"}:
            continue
        code = _normalize_choice_code(str(key).replace(marker, "", 1))
        selected.append(labels.get(code, code))
    return sorted(selected)


def refresh_bht_map_mart(settings: Settings) -> None:
    if not settings.database_url:
        return
    category_rows = [
        (slug, meta["panelCode"])
        for slug, meta in BHT_CATEGORY_PANEL_MAP.items()
    ]
    region_labels_json = json.dumps(BHT_OVERVIEW_DISTRIBUTIONS["region"]["labels"])
    sector_labels_json = json.dumps(SECTOR_LABELS)
    week_labels_json = json.dumps(BHT_OVERVIEW_DISTRIBUTIONS["week"]["labels"])
    gender_labels_json = json.dumps(BHT_OVERVIEW_DISTRIBUTIONS["gender"]["labels"])
    sec_labels_json = json.dumps(_choice_labels_for_variable(settings, "SEC"))
    age_labels_json = json.dumps(_choice_labels_for_variable(settings, "Age_cal"))
    m_scope_clause, m_scope_params = main_case_scope_clause(settings, "m")
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            # This materialization runs in the ETL workflow and can exceed the
            # interactive query deadline on production-sized SurveyCTO data.
            cur.execute("SET LOCAL statement_timeout = '10min'")
            cur.execute("DELETE FROM mart.bht_map_point_category")
            cur.execute("DELETE FROM mart.bht_map_point")
            cur.execute("DELETE FROM mart.bht_case_overview_dim")
            cur.execute(
                f"""
                WITH panel_categories AS (
                    SELECT
                        p.case_id,
                        ARRAY_AGG(DISTINCT panel_map.category_slug ORDER BY panel_map.category_slug) FILTER (WHERE panel_map.category_slug IS NOT NULL) AS panel_slugs
                    FROM clean.main_case_panel p
                    LEFT JOIN (
                        VALUES
                            ('Panel_1', 'noodles'),
                            ('Panel_2', 'toothpaste'),
                            ('Panel_3', 'edible-oil'),
                            ('Panel_4', 'bleach'),
                            ('Panel_5', 'toilet-cleaner'),
                            ('Panel_6', 'snacks'),
                            ('Panel_7', 'breakfast-cereals'),
                            ('Panel_8', 'condiment-mixes'),
                            ('Panel_9', 'wet-hair'),
                            ('Panel_10', 'dry-hair'),
                            ('Panel_11', 'malt')
                    ) AS panel_map(panel_code, category_slug)
                        ON panel_map.panel_code = p.panel_code
                    WHERE COALESCE(p.is_selected, TRUE)
                    GROUP BY p.case_id
                )
                INSERT INTO mart.bht_case_overview_dim (
                    case_id, submission_key, survey_month, submitted_at, start_time, approval_status,
                    category_slugs, region_code, region_label, sector_code, sector_label,
                    sec_value, sec_label, week_value, week_label, gender_value, gender_label,
                    age_value, age_label, updated_at
                )
                SELECT
                    m.case_id,
                    m.submission_key,
                    COALESCE(m.survey_month, 'unknown') AS survey_month,
                    m.submitted_at,
                    COALESCE(NULLIF(TRIM(m.record->>'starttime'), ''), NULLIF(TRIM(m.record->>'start_time'), ''), NULLIF(TRIM(m.record->>'StartTime'), ''), NULLIF(TRIM(m.record->>'start'), '')) AS start_time,
                    COALESCE(NULLIF(TRIM(m.approval_stage), ''), NULLIF(TRIM(m.current_status), ''), 'pending_review') AS approval_status,
                    ARRAY(
                        SELECT DISTINCT slug
                        FROM UNNEST(ARRAY['omnibus']::text[] || COALESCE(pc.panel_slugs, ARRAY[]::text[])) AS slug
                        ORDER BY slug
                    ) AS category_slugs,
                    NULLIF(TRIM(m.record->>'City_1'), '') AS region_code,
                    COALESCE(
                        %s::jsonb ->> regexp_replace(COALESCE(NULLIF(TRIM(m.record->>'City_1'), ''), ''), '\\.0$', ''),
                        NULLIF(TRIM(m.record->>'City_1'), ''),
                        'Unknown'
                    ) AS region_label,
                    NULLIF(TRIM(m.record->>'Sector'), '') AS sector_code,
                    COALESCE(
                        %s::jsonb ->> regexp_replace(COALESCE(NULLIF(TRIM(m.record->>'Sector'), ''), ''), '\\.0$', ''),
                        NULLIF(TRIM(m.record->>'Sector'), ''),
                        '(No response)'
                    ) AS sector_label,
                    NULLIF(TRIM(m.record->>'SEC'), '') AS sec_value,
                    COALESCE(
                        %s::jsonb ->> regexp_replace(COALESCE(NULLIF(TRIM(m.record->>'SEC'), ''), ''), '\\.0$', ''),
                        NULLIF(TRIM(m.record->>'SEC'), ''),
                        '(No response)'
                    ) AS sec_label,
                    NULLIF(TRIM(m.record->>'Week'), '') AS week_value,
                    COALESCE(
                        %s::jsonb ->> regexp_replace(COALESCE(NULLIF(TRIM(m.record->>'Week'), ''), ''), '\\.0$', ''),
                        NULLIF(TRIM(m.record->>'Week'), ''),
                        '(No response)'
                    ) AS week_label,
                    NULLIF(TRIM(m.record->>'Gender'), '') AS gender_value,
                    COALESCE(
                        %s::jsonb ->> regexp_replace(COALESCE(NULLIF(TRIM(m.record->>'Gender'), ''), ''), '\\.0$', ''),
                        NULLIF(TRIM(m.record->>'Gender'), ''),
                        '(No response)'
                    ) AS gender_label,
                    NULLIF(TRIM(m.record->>'Age_cal'), '') AS age_value,
                    COALESCE(
                        %s::jsonb ->> regexp_replace(COALESCE(NULLIF(TRIM(m.record->>'Age_cal'), ''), ''), '\\.0$', ''),
                        NULLIF(TRIM(m.record->>'Age_cal'), ''),
                        '(No response)'
                    ) AS age_label,
                    now()
                FROM clean.main_case m
                LEFT JOIN panel_categories pc ON pc.case_id = m.case_id
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM clean.deleted_main_cases dmc
                    WHERE dmc.submission_key = m.submission_key
                )
                {m_scope_clause}
                """,
                (region_labels_json, sector_labels_json, sec_labels_json, week_labels_json, gender_labels_json, age_labels_json, *m_scope_params),
            )
            cur.execute(
                f"""
                INSERT INTO mart.bht_map_point (
                    case_id, submission_key, survey_month, ea_id, interviewer_id, username,
                    city_code, sector_code, gps_lat, gps_long, approval_status, submitted_at,
                    start_time, week_value, record_city, record_sector
                )
                SELECT
                    m.case_id,
                    m.submission_key,
                    m.survey_month,
                    m.ea_id,
                    m.interviewer_id,
                    m.username,
                    m.city_code,
                    m.sector_code,
                    m.gps_lat,
                    m.gps_long,
                    COALESCE(NULLIF(TRIM(m.approval_stage), ''), NULLIF(TRIM(m.current_status), ''), 'pending_review') AS approval_status,
                    m.submitted_at,
                    COALESCE(NULLIF(TRIM(m.record->>'starttime'), ''), NULLIF(TRIM(m.record->>'start_time'), ''), NULLIF(TRIM(m.record->>'StartTime'), ''), NULLIF(TRIM(m.record->>'start'), '')) AS start_time,
                    m.record->>'Week' AS week_value,
                    m.record->>'City_1' AS record_city,
                    m.record->>'Sector' AS record_sector
                FROM clean.main_case m
                WHERE m.gps_lat IS NOT NULL
                  AND m.gps_long IS NOT NULL
                  AND m.gps_lat BETWEEN -90 AND 90
                  AND m.gps_long BETWEEN -180 AND 180
                  AND NOT EXISTS (
                      SELECT 1
                      FROM clean.deleted_main_cases dmc
                      WHERE dmc.submission_key = m.submission_key
                  )
                  {m_scope_clause}
                """
                ,
                tuple(m_scope_params),
            )
            cur.execute(
                """
                INSERT INTO mart.bht_map_point_category (category_slug, case_id)
                SELECT 'omnibus', case_id
                FROM mart.bht_map_point
                ON CONFLICT DO NOTHING
                """
            )
            for slug, panel_code in category_rows:
                if not panel_code:
                    continue
                cur.execute(
                    """
                    INSERT INTO mart.bht_map_point_category (category_slug, case_id)
                    SELECT %s, p.case_id
                    FROM clean.main_case_panel p
                    INNER JOIN mart.bht_map_point mp ON mp.case_id = p.case_id
                    WHERE p.panel_code = %s
                      AND COALESCE(p.is_selected, TRUE)
                    ON CONFLICT DO NOTHING
                    """,
                    (slug, panel_code),
                )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bht_map_point_submitted ON mart.bht_map_point (submitted_at DESC, case_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bht_map_point_category_case ON mart.bht_map_point_category (case_id, category_slug)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bht_case_overview_month ON mart.bht_case_overview_dim (survey_month)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bht_case_overview_region ON mart.bht_case_overview_dim (region_label, survey_month)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bht_case_overview_sector ON mart.bht_case_overview_dim (sector_label, survey_month)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bht_case_overview_categories ON mart.bht_case_overview_dim USING gin (category_slugs)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bht_case_overview_status ON mart.bht_case_overview_dim (approval_status)"
            )
        conn.commit()


def _ensure_bht_map_mart(settings: Settings) -> None:
    if not settings.database_url:
        return
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'mart' AND table_name = 'bht_map_point'
                ) AS exists
                """
            )
            exists = bool((cur.fetchone() or {}).get("exists"))
            if exists:
                cur.execute("ALTER TABLE IF EXISTS mart.bht_map_point ADD COLUMN IF NOT EXISTS start_time text")
                conn.commit()
                cur.execute(
                    """
                    SELECT
                        (SELECT COUNT(*)::int FROM mart.bht_map_point) AS mart_cases,
                        (
                            SELECT COUNT(*)::int
                            FROM clean.main_case m
                            WHERE m.gps_lat IS NOT NULL
                              AND m.gps_long IS NOT NULL
                              AND m.gps_lat BETWEEN -90 AND 90
                              AND m.gps_long BETWEEN -180 AND 180
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM clean.deleted_main_cases dmc
                                  WHERE dmc.submission_key = m.submission_key
                              )
                        ) AS clean_cases
                    """
                )
                row = cur.fetchone() or {}
                if int(row.get("mart_cases") or 0) > 0 and int(row.get("mart_cases") or 0) == int(row.get("clean_cases") or 0):
                    return
    logger.info("BHT map mart is missing; it will be rebuilt by startup/background refresh.")
    BHT_MAP_CACHE.clear()


def _bau5a_select_fields(category_key: str) -> tuple[list[str], list[tuple[str, str, str]]]:
    prefix = BHT_CATEGORY_BAU5A_PREFIX.get(category_key)
    if not prefix:
        return [], []
    try:
        dictionary_path = str(load_main_survey_pipeline_config().dictionary_file)
    except Exception:
        dictionary_path = ""
    labels = _load_bau5a_choice_labels(dictionary_path).get(prefix, {})
    select_parts: list[str] = []
    aliases: list[tuple[str, str, str]] = []
    for idx, code in enumerate(sorted(labels.keys(), key=lambda item: (len(item), item))):
        if not re.fullmatch(r"[A-Za-z0-9_]+", code):
            continue
        field_name = f"{prefix}_BAU5a_{code}"
        alias = f"bau5a_{idx}"
        select_parts.append(f"m.record->>'{field_name}' AS {alias}")
        aliases.append((alias, code, labels.get(code, code)))
    return select_parts, aliases


def get_bht_map(
    settings: Settings,
    user: AuthUser,
    category: str = "omnibus",
    months: list[str] | None = None,
    sectors: list[str] | None = None,
    limit: int = 5000,
    categories: list[str] | None = None,
) -> dict[str, Any]:
    if not settings.database_url:
        return {
            "category": {"slug": "omnibus", **BHT_CATEGORY_PANEL_MAP["omnibus"]},
            "monthsAvailable": [],
            "monthsSelected": months or [],
            "sectorsAvailable": [],
            "sectorsSelected": sectors or [],
            "gpsPoints": [],
            "summary": {"totalCases": 0, "mappedCases": 0, "missingGpsCases": 0, "interviewerCount": 0, "weekCounts": {}},
        }

    selected_category_keys = [
        str(item).strip()
        for item in (categories or [])
        if str(item).strip() in BHT_CATEGORY_PANEL_MAP and str(item).strip() not in {"all"}
    ]
    if "omnibus" in selected_category_keys and len(selected_category_keys) > 1:
        selected_category_keys = []
    category_key = "all" if category == "all" else category if category in BHT_CATEGORY_PANEL_MAP else "omnibus"
    if selected_category_keys and len(selected_category_keys) == 1:
        category_key = selected_category_keys[0]
    category_meta = {"label": "All Categories", "panelCode": None} if category_key == "all" else BHT_CATEGORY_PANEL_MAP[category_key]
    months_sel = [str(m).strip() for m in (months or []) if str(m).strip()]
    sectors_sel = [str(s).strip() for s in (sectors or []) if str(s).strip()]
    sector_labels = _choice_labels_for_variable(settings, "Sector")
    sector_values = _choice_filter_values(sector_labels, sectors_sel)
    safe_limit = max(1, min(int(limit or 5000), 10000))
    _ensure_bht_map_mart(settings)
    cache_key = (settings.main_survey_data_start_date or "", category_key, tuple(selected_category_keys), tuple(months_sel), tuple(sectors_sel), safe_limit)
    cached = BHT_MAP_CACHE.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < BHT_MAP_CACHE_TTL_SECONDS:
        return cached[1]

    base_where = ["TRUE"]
    base_params: list[Any] = []
    category_scope_params: list[Any] = []
    category_scope_sql = ""
    if selected_category_keys:
        category_scope_sql = """
                  AND EXISTS (
                      SELECT 1
                      FROM mart.bht_map_point_category pc
                      WHERE pc.case_id = m.case_id
                        AND pc.category_slug = ANY(%s)
                  )
        """
        category_scope_params.append(selected_category_keys)
    elif category_key != "all":
        category_scope_sql = """
                  AND EXISTS (
                      SELECT 1
                      FROM mart.bht_map_point_category pc
                      WHERE pc.case_id = m.case_id
                        AND pc.category_slug = %s
                  )
        """
        category_scope_params.append(category_key)
    active_scope_sql, active_scope_params = main_row_scope_clause(settings, "m", prefix="AND")
    if active_scope_sql:
        base_where.append(active_scope_sql.removeprefix("AND ").strip())
        base_params.extend(active_scope_params)
    if months_sel:
        base_where.append("m.survey_month = ANY(%s)")
        base_params.append(months_sel)
    if sector_values:
        base_where.append("COALESCE(NULLIF(TRIM(m.record_sector), ''), NULLIF(TRIM(m.sector_code), '')) = ANY(%s)")
        base_params.append(sector_values)

    base_sql = " AND ".join(base_where)

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '20000ms'")
            month_scope_sql, month_scope_params = main_row_scope_clause(settings, "", prefix="AND")
            cur.execute(
                f"""
                SELECT DISTINCT survey_month AS value
                FROM mart.bht_map_point
                WHERE survey_month IS NOT NULL
                {month_scope_sql}
                ORDER BY survey_month DESC
                """,
                tuple(month_scope_params),
            )
            months_available = [str(r["value"]) for r in cur.fetchall() if r.get("value")]

            sector_option_params: list[Any] = []
            sector_option_category_sql = ""
            if selected_category_keys:
                sector_option_category_sql = """
                AND EXISTS (
                    SELECT 1
                    FROM mart.bht_map_point_category pc
                    WHERE pc.case_id = m.case_id
                      AND pc.category_slug = ANY(%s)
                )
                """
                sector_option_params.append(selected_category_keys)
            elif category_key != "all":
                sector_option_category_sql = """
                AND EXISTS (
                    SELECT 1
                    FROM mart.bht_map_point_category pc
                    WHERE pc.case_id = m.case_id
                      AND pc.category_slug = %s
                )
                """
                sector_option_params.append(category_key)
            sector_option_month_sql = ""
            sector_option_scope_sql, sector_option_scope_params = main_row_scope_clause(settings, "m", prefix="AND")
            sector_option_params.extend(sector_option_scope_params)
            if months_sel:
                sector_option_month_sql = "AND m.survey_month = ANY(%s)"
                sector_option_params.append(months_sel)
            cur.execute(
                f"""
                SELECT DISTINCT COALESCE(NULLIF(TRIM(m.record_sector), ''), NULLIF(TRIM(m.sector_code), '')) AS sector
                FROM mart.bht_map_point m
                WHERE COALESCE(NULLIF(TRIM(m.record_sector), ''), NULLIF(TRIM(m.sector_code), '')) IS NOT NULL
                {sector_option_category_sql}
                {sector_option_scope_sql}
                {sector_option_month_sql}
                ORDER BY sector
                """,
                tuple(sector_option_params),
            )
            sectors_available = [_choice_label(sector_labels, r.get("sector")) for r in cur.fetchall() if r.get("sector")]

            cur.execute(
                f"""
                SELECT
                    m.case_id,
                    m.submission_key,
                    m.survey_month,
                    m.ea_id,
                    m.interviewer_id,
                    m.username,
                    m.city_code,
                    m.sector_code,
                    m.gps_lat,
                    m.gps_long,
                    m.week_value,
                    m.approval_status,
                    m.submitted_at,
                    m.record_city,
                    m.record_sector,
                    dim.gender_label,
                    mq.selected_panel_labels
                FROM mart.bht_map_point m
                LEFT JOIN mart.bht_case_overview_dim dim ON dim.case_id = m.case_id
                LEFT JOIN mart.main_case_queue mq ON mq.case_id = m.case_id
                WHERE {base_sql}
                {category_scope_sql}
                ORDER BY m.submitted_at DESC NULLS LAST, m.case_id
                LIMIT %s
                """,
                tuple([*base_params, *category_scope_params, safe_limit]),
            )
            point_rows = cur.fetchall()

    gps_points = []
    week_counts: Counter[str] = Counter()
    interviewers: set[str] = set()
    for row in point_rows:
        city = _bht_city_label(row.get("record_city") or row.get("city_code"))
        sector = _choice_label(sector_labels, row.get("record_sector") or row.get("sector_code")) or None
        interviewer = str(row.get("interviewer_id") or row.get("username") or "").strip() or None
        if interviewer:
            interviewers.add(interviewer)
        case_id = str(row.get("case_id") or row.get("submission_key") or "")
        week = _normalize_bht_week(row.get("week_value"))
        if week:
            week_counts[week] += 1
        lat = row.get("gps_lat")
        lng = row.get("gps_long")
        if lat is None or lng is None:
            continue
        gps_points.append(
            {
                "point_id": case_id,
                "submission_key": str(row.get("submission_key") or ""),
                "case_id": case_id,
                "ea_id": row.get("ea_id"),
                "row_type": "respondent",
                "sample_flag": False,
                "gps_lat": float(lat),
                "gps_long": float(lng),
                "approval_status": row.get("approval_status"),
                "ea_name": city,
                "state_name": city,
                "city": city,
                "sector": sector,
                "week": week,
                "survey_month": row.get("survey_month"),
                "interviewer_id": interviewer,
                "submitted_at": row.get("submitted_at").isoformat() if row.get("submitted_at") else None,
                "selected_panel_labels": row.get("selected_panel_labels") or "Omnibus",
                "gender": row.get("gender_label"),
                "bau5aAnswers": [],
            }
        )

    payload = {
        "category": {"slug": category_key, **category_meta},
        "monthsAvailable": months_available,
        "monthsSelected": months_sel,
        "sectorsAvailable": sectors_available,
        "sectorsSelected": sectors_sel,
        "gpsPoints": gps_points,
        "summary": {
            "totalCases": len(gps_points),
            "mappedCases": len(gps_points),
            "missingGpsCases": 0,
            "interviewerCount": len(interviewers),
            "returnedPoints": len(gps_points),
            "limit": safe_limit,
            "weekCounts": {f"Week {idx}": int(week_counts.get(f"Week {idx}", 0)) for idx in range(1, 5)},
        },
    }
    BHT_MAP_CACHE[cache_key] = (time.monotonic(), payload)
    return payload


def get_bht_map_point_bau5a(
    settings: Settings,
    user: AuthUser,
    case_id: str,
    category: str = "omnibus",
) -> dict[str, Any]:
    category_key = "all" if category == "all" else category if category in BHT_CATEGORY_PANEL_MAP else "omnibus"
    if category_key == "all":
        return {"caseId": case_id, "category": {"slug": "all", "label": "All Categories", "panelCode": None}, "bau5aAnswers": []}
    if category_key == "omnibus" or not settings.database_url:
        return {"caseId": case_id, "category": {"slug": category_key, **BHT_CATEGORY_PANEL_MAP[category_key]}, "bau5aAnswers": []}

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT record
                FROM clean.main_case
                WHERE case_id = %s OR submission_key = %s
                LIMIT 1
                """,
                (case_id, case_id),
            )
            row = cur.fetchone() or {}
    record = row.get("record") if isinstance(row, dict) else None
    if not isinstance(record, dict):
        record = {}
    return {
        "caseId": case_id,
        "category": {"slug": category_key, **BHT_CATEGORY_PANEL_MAP[category_key]},
        "bau5aAnswers": _extract_bau5a_answers(record, category_key),
    }


def _parse_template_value_labels(raw: str) -> dict[float | str, str]:
    """Parse '1.0=Yes | 2.0=No' → {1.0: 'Yes', 2.0: 'No'}."""
    result: dict[float | str, str] = {}
    if not raw:
        return result
    for part in raw.split("|"):
        part = part.strip()
        if "=" not in part:
            continue
        key_str, _, val_str = part.partition("=")
        key_str = key_str.strip()
        val_str = val_str.strip()
        try:
            result[float(key_str)] = val_str
        except ValueError:
            result[key_str] = val_str
    return result


MAIN_SURVEY_TEMPLATE_FILE = "Main_Survey_Export_Template.sav"


def _template_value_labels_are_numeric(labels: dict[Any, Any] | None) -> bool:
    if not labels:
        return False
    for key in labels.keys():
        if isinstance(key, (int, float)):
            continue
        try:
            float(str(key).strip())
        except (TypeError, ValueError):
            return False
    return True


def _sanitize_template_value_labels(labels: dict[Any, Any] | None, numeric: bool) -> dict[Any, str]:
    cleaned: dict[Any, str] = {}
    if not labels:
        return cleaned
    for raw_key, raw_label in labels.items():
        label = str(raw_label or '')
        if numeric:
            try:
                number = float(raw_key)
            except (TypeError, ValueError):
                continue
            cleaned[int(number) if number.is_integer() else number] = label
        else:
            cleaned[str(raw_key)] = label
    return cleaned


def _normalize_sav_cell_value(value: Any) -> Any:
    if isinstance(value, pd.Series):
        if len(value) == 1:
            return _normalize_sav_cell_value(value.iloc[0])
        return ""
    if isinstance(value, pd.DataFrame):
        return ""
    if isinstance(value, np.ndarray):
        return _normalize_sav_cell_value(value.tolist())
    if isinstance(value, (list, tuple, set)):
        normalized = [_normalize_sav_cell_value(item) for item in value]
        scalar_items = []
        for item in normalized:
            if item is None:
                scalar_items.append("")
                continue
            try:
                is_missing = bool(pd.isna(item))
            except (TypeError, ValueError):
                is_missing = False
            scalar_items.append("" if is_missing else str(item))
        return " ".join(item for item in scalar_items if item)
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        except TypeError:
            return str(value)
    return value


def _sanitize_sav_dataframe_values(df: pd.DataFrame) -> pd.DataFrame:
    object_cols = [col for col in df.columns if pd.api.types.is_object_dtype(df[col])]
    if not object_cols:
        return df

    out = df
    for col in object_cols:
        series = out[col]
        nested_mask = series.map(lambda value: isinstance(value, (pd.Series, pd.DataFrame, np.ndarray, list, tuple, set, dict)))
        if nested_mask.any():
            if out is df:
                out = df.copy(deep=False)
            out.loc[nested_mask, col] = series.loc[nested_mask].map(_normalize_sav_cell_value)
    return out


def _coerce_template_dataframe_for_sav(
    df: pd.DataFrame,
    template_columns: list[str],
    original_types: dict[str, Any] | None,
    template_value_labels: dict[str, dict[Any, Any]] | None,
) -> tuple[pd.DataFrame, dict[str, dict[Any, str]]]:
    # Shallow-copy the frame metadata only.  The previous implementation made a
    # full dataframe copy and then reassigned one column at a time, which is
    # expensive for large wide exports.  Assignments below materialize only the
    # columns that actually need coercion.
    out = df.copy(deep=False)
    original_types = original_types or {}
    template_value_labels = template_value_labels or {}
    sanitized_labels: dict[str, dict[Any, str]] = {}

    template_set = set(template_columns)
    bool_cols: list[str] = []
    numeric_cols: list[str] = []
    string_cols: list[str] = []
    object_cols: list[str] = []

    for col in template_columns:
        if col not in out.columns:
            continue
        raw_type = str(original_types.get(col, '') or '').strip().upper()
        labels = template_value_labels.get(col)
        string_storage = raw_type.startswith('A')
        numeric = (
            (raw_type and not string_storage and 'DATE' not in raw_type and 'TIME' not in raw_type)
            or _template_value_labels_are_numeric(labels)
        )
        series = out[col]
        if string_storage:
            string_cols.append(col)
            numeric = False
        elif pd.api.types.is_bool_dtype(series):
            bool_cols.append(col)
            numeric = True
        elif numeric:
            numeric_cols.append(col)
        elif pd.api.types.is_object_dtype(series):
            object_cols.append(col)

        sanitized = _sanitize_template_value_labels(labels, numeric)
        if sanitized:
            sanitized_labels[col] = sanitized

    if bool_cols:
        for col in bool_cols:
            out[col] = out[col].astype('int64')
    if numeric_cols:
        for col in numeric_cols:
            out[col] = pd.to_numeric(out[col], errors='coerce')
    if string_cols:
        for col in string_cols:
            out[col] = (
                out[col]
                .map(_normalize_sav_cell_value)
                .astype('string')
                .fillna('')
                .replace({'nan': '', 'None': '', 'NaT': ''})
                .astype(object)
            )

    extra_object_cols = [
        col for col in out.columns
        if col not in template_set and pd.api.types.is_object_dtype(out[col])
    ]
    stringify_cols = object_cols + extra_object_cols
    if stringify_cols:
        for col in stringify_cols:
            out[col] = (
                out[col]
                .astype('string')
                .fillna('')
                .replace({'nan': '', 'None': '', 'NaT': ''})
                .astype(object)
            )

    return _sanitize_sav_dataframe_values(out), sanitized_labels

def _write_sav_with_fallback(df: pd.DataFrame, sav_path: Path, base_kwargs: dict[str, Any]) -> None:
    attempts: list[dict[str, Any]] = []
    cleaned = {k: v for k, v in base_kwargs.items() if v}
    df = _sanitize_sav_dataframe_values(df)

    # Prefer a row-compressed SAV now that file size is the priority. The
    # uncompressed attempts remain as fallbacks for metadata combinations that
    # older pyreadstat builds cannot write with row_compress enabled.
    compressed = dict(cleaned)
    compressed["row_compress"] = True
    attempts.append(compressed)
    attempts.append(dict(cleaned))

    no_missing = dict(cleaned)
    no_missing.pop("missing_ranges", None)
    attempts.append(no_missing)

    no_format = dict(no_missing)
    no_format.pop("variable_format", None)
    attempts.append(no_format)

    no_vlabels = dict(no_format)
    no_vlabels.pop("variable_value_labels", None)
    attempts.append(no_vlabels)

    labels_only = {}
    if cleaned.get("column_labels"):
        labels_only["column_labels"] = cleaned["column_labels"]
    attempts.append(labels_only)
    attempts.append({})

    last_error: Exception | None = None
    seen: set[tuple[tuple[str, str], ...]] = set()
    for kwargs in attempts:
        fingerprint = tuple(sorted((key, type(value).__name__) for key, value in kwargs.items()))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        try:
            pyreadstat.write_sav(df, str(sav_path), **kwargs)
            return
        except Exception as exc:  # pragma: no cover - defensive export fallback
            last_error = exc
            logger.warning("Main survey SAV export fallback failed with kwargs=%s: %s", list(kwargs.keys()), exc)

    raise HTTPException(status_code=500, detail=f"SPSS export failed: {last_error}")


@lru_cache(maxsize=4)
def _main_export_template_meta(root_dir: str) -> dict[str, Any] | None:
    """Cached zero-row read of the main SAV template metadata."""
    template_path = Path(root_dir) / MAIN_SURVEY_TEMPLATE_FILE
    if not template_path.exists():
        return None
    _, meta = pyreadstat.read_sav(str(template_path), row_limit=0)
    return {
        "column_names": list(meta.column_names or []),
        "column_labels": list(meta.column_labels or []),
        "variable_value_labels": dict(meta.variable_value_labels or {}),
        "variable_measure": dict(meta.variable_measure or {}),
        "variable_display_width": dict(getattr(meta, "variable_display_width", {}) or {}),
        "original_variable_types": dict(getattr(meta, "original_variable_types", {}) or {}),
        "missing_ranges": dict(getattr(meta, "missing_ranges", {}) or {}),
    }


@lru_cache(maxsize=4)
def _main_export_column_meta(
    root_dir: str,
) -> tuple[list[str], dict[str, str], dict[str, str], dict[str, int], dict[str, int], dict[str, dict]]:
    """Variable order and full metadata from the main survey .sav template file.

    Reads column names, labels, value labels, and measures directly from the
    SPSS template.  Falls back to the legacy xlsx dictionary if the .sav is absent.

    Returns:
        order           – list of variable names in template order
        labels          – variable_name → variable_label
        measures        – variable_name → measure string ('nominal'/'ordinal'/'scale')
        display_widths  – variable_name → display_width int (empty if not available)
        storage_widths  – variable_name → storage_width int (empty if not available)
        value_labels    – variable_name → {numeric_key: label_str} dict
    """
    template_meta = _main_export_template_meta(root_dir)
    if template_meta is not None:
        order: list[str] = list(template_meta["column_names"])
        raw_labels = list(template_meta["column_labels"])
        labels: dict[str, str] = {
            col: (raw_labels[i] if i < len(raw_labels) else col)
            for i, col in enumerate(order)
        }
        value_labels: dict[str, dict] = dict(template_meta["variable_value_labels"])
        measures: dict[str, str] = dict(template_meta["variable_measure"])
        display_widths: dict[str, int] = dict(template_meta["variable_display_width"])
        return order, labels, measures, display_widths, {}, value_labels

    # Legacy fallback — read from xlsx dictionary
    dictionary_path = Path(root_dir) / MAIN_SURVEY_DICTIONARY_FILE
    if not dictionary_path.exists():
        return [], {}, {}, {}, {}, {}
    df = pd.read_excel(dictionary_path).fillna("")
    norm = {str(c).strip().lower(): c for c in df.columns}
    if "variable_name" not in norm:
        return [], {}, {}, {}, {}, {}

    order2: list[str] = []
    labels2: dict[str, str] = {}
    measures2: dict[str, str] = {}
    display_widths: dict[str, int] = {}
    storage_widths: dict[str, int] = {}
    value_labels2: dict[str, dict] = {}
    seen: set[str] = set()

    for row in df.to_dict(orient="records"):
        var = _safe_text(row.get(norm["variable_name"]))
        if not var or var in seen:
            continue
        seen.add(var)
        order2.append(var)

        label_col = norm.get("variable_label")
        labels2[var] = (_safe_text(row.get(label_col)) if label_col else "") or var

        measure_col = norm.get("measure")
        if measure_col:
            raw_measure = _safe_text(row.get(measure_col)).lower()
            if raw_measure in {"nominal", "ordinal", "scale"}:
                measures2[var] = raw_measure

        disp_col = norm.get("display_width")
        if disp_col:
            try:
                display_widths[var] = int(float(str(row.get(disp_col) or 0)))
            except (ValueError, TypeError):
                pass

        stor_col = norm.get("storage_width")
        if stor_col:
            try:
                storage_widths[var] = int(float(str(row.get(stor_col) or 0)))
            except (ValueError, TypeError):
                pass

        vl_col = norm.get("value_labels")
        if vl_col:
            raw_vl = _safe_text(row.get(vl_col))
            parsed = _parse_template_value_labels(raw_vl)
            if parsed:
                value_labels2[var] = parsed

    return order2, labels2, measures2, display_widths, storage_widths, value_labels2


def _is_missing_export_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in {"", "nan", "NaN", "None", "<NA>"}
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        missing = False
    if isinstance(missing, (bool, np.bool_)):
        return bool(missing)
    return False


def _merge_export_record_values(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if _is_missing_export_value(target.get(key)):
            target[key] = value


def _missing_export_value_mask(series: pd.Series) -> pd.Series:
    return series.isna() | series.astype("string").isin(["", "nan", "NaN", "None", "<NA>"])


def _reconstruct_main_multiple_response_parents(df: pd.DataFrame) -> pd.DataFrame:
    """Vectorized parent select_multiple reconstruction from child columns."""
    if df.empty:
        return df
    out = df
    columns = [str(col) for col in out.columns]
    blank_values = {"", "0", 0, "nan", "NaN", "None", "none", "null", "NULL", "<NA>"}

    for parent in MAIN_EXPORT_MULTIPLE_RESPONSE_PARENTS:
        prefix = f"{parent}_"
        child_cols = [col for col in columns if col.startswith(prefix)]
        if not child_cols:
            continue
        suffixes = [col[len(prefix):] for col in child_cols]
        child = out.loc[:, child_cols].astype("object")
        selected = (child.notna() & ~child.isin(blank_values)).to_numpy(dtype=bool, copy=False)
        suffix_array = np.asarray(suffixes, dtype=object)
        reconstructed = pd.Series(
            (" ".join(suffix_array[row_mask]) if row_mask.any() else pd.NA for row_mask in selected),
            index=out.index,
            dtype="object",
        )
        if parent in out.columns:
            missing_parent = _missing_export_value_mask(out[parent])
            out.loc[missing_parent, parent] = reconstructed.loc[missing_parent]
        else:
            out[parent] = reconstructed
    return out


def build_main_survey_wide_export_dataframe(
    settings: Settings,
    user: AuthUser,
    statuses: list[str] | None = None,
    final_outcome_codes: list[str] | None = None,
) -> pd.DataFrame:
    """Case-level wide export: dictionary column order × values from clean.main_case.record jsonb."""
    order, _labels, _measures, _dw, _sw, _vl = _main_export_column_meta(str(settings.root_dir))
    if not order:
        order = [
            "submission_key",
            "case_id",
            "ea_id",
            "interviewer_id",
            "supervisor_id",
            "approval_stage",
        ]

    cols_meta = [
        "submission_key",
        "case_id",
        "ea_id",
        "interviewer_id",
        "supervisor_id",
        "approval_stage",
        "is_callback_required",
        "submitted_at",
        "reviewed_at",
        "approved_at",
        "deleted_at",
        "deleted_by",
        "deletion_reason",
        "auto_flagged_qc_issue_count",
        "auto_flagged_qc_issue_codes",
        "auto_flagged_qc_issues",
    ]

    allowed_statuses = statuses or ["approved"]

    status_clause = "m.approval_stage = ANY(%s)"
    status_params: list[Any] = [allowed_statuses]
    final_outcomes = [str(item).strip() for item in (final_outcome_codes or []) if str(item).strip()]
    final_outcome_clause = ""
    if final_outcomes:
        final_outcome_clause = """
                  AND TRIM(COALESCE(m.record->>'final_outcome_code', '')) = ANY(%s)
        """
        status_params.append(final_outcomes)

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT m.submission_key, m.case_id, m.ea_id, m.interviewer_id, m.supervisor_id,
                       m.approval_stage, m.is_callback_required, m.submitted_at, m.reviewed_at, m.approved_at,
                       dmc.deleted_at,
                       COALESCE(NULLIF(TRIM(du.full_name), ''), NULLIF(TRIM(du.username), ''), dmc.deleted_by::text) AS deleted_by,
                       dmc.reason AS deletion_reason,
                       COALESCE(qc.auto_flagged_qc_issue_count, 0)::int AS auto_flagged_qc_issue_count,
                       qc.auto_flagged_qc_issue_codes,
                       qc.auto_flagged_qc_issues,
                       m.record
                FROM clean.main_case m
                LEFT JOIN clean.deleted_main_cases dmc ON dmc.submission_key = m.submission_key
                LEFT JOIN app.user_account du ON du.user_id::text = dmc.deleted_by
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(*) FILTER (WHERE COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved')::int AS auto_flagged_qc_issue_count,
                        STRING_AGG(DISTINCT rr.rule_code, ', ' ORDER BY rr.rule_code)
                            FILTER (WHERE COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved') AS auto_flagged_qc_issue_codes,
                        STRING_AGG(
                            DISTINCT COALESCE(NULLIF(TRIM(iq.issue_summary), ''), NULLIF(TRIM(rr.result_message), ''), rr.rule_code),
                            ' | '
                            ORDER BY COALESCE(NULLIF(TRIM(iq.issue_summary), ''), NULLIF(TRIM(rr.result_message), ''), rr.rule_code)
                        ) FILTER (WHERE COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved') AS auto_flagged_qc_issues
                    FROM qc.issue_queue iq
                    INNER JOIN qc.rule_result rr ON rr.rule_result_id = iq.rule_result_id
                    WHERE iq.instrument_code = 'main'
                      AND COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), '')) =
                          COALESCE(NULLIF(TRIM(m.submission_key), ''), NULLIF(TRIM(m.case_id), ''))
                ) qc ON TRUE
                WHERE {status_clause}
                  {final_outcome_clause}
                ORDER BY m.submitted_at DESC NULLS LAST
                """,
                tuple(status_params),
            )
            rows = cur.fetchall()
            case_ids = [str(row.get("case_id") or "").strip() for row in rows if str(row.get("case_id") or "").strip()]
            section_records_by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
            if case_ids:
                cur.execute(
                    """
                    SELECT case_id, record
                    FROM clean.main_case_section
                    WHERE case_id = ANY(%s)
                    """,
                    (case_ids,),
                )
                for section_row in cur.fetchall():
                    case_key = str(section_row.get("case_id") or "").strip()
                    if case_key:
                        section_records_by_case[case_key].append(_coerce_json_record(section_row.get("record")))

    column_order = list(dict.fromkeys(cols_meta + list(order)))
    if not rows:
        return pd.DataFrame(columns=column_order)

    meta_rows: list[dict[str, Any]] = []
    record_rows: list[dict[str, Any]] = []
    # One pass over cases to coerce JSON and merge any section records.  Avoid
    # the previous nested loop over every export variable for every case.
    for row in rows:
        meta_rows.append({col: row.get(col) for col in cols_meta})
        rec = _coerce_json_record(row.get("record"))
        case_key = str(row.get("case_id") or "").strip()
        if section_records_by_case.get(case_key):
            merged_rec = dict(rec)
            for section_record in section_records_by_case[case_key]:
                _merge_export_record_values(merged_rec, section_record)
            rec = merged_rec
        record_rows.append(rec)

    meta_df = pd.DataFrame.from_records(meta_rows, columns=cols_meta)
    record_df = pd.DataFrame.from_records(record_rows)
    if record_df.empty:
        out = meta_df
    else:
        # Authoritative metadata columns come from clean.main_case, not the JSON record.
        record_df = record_df.drop(columns=[col for col in cols_meta if col in record_df.columns], errors="ignore")
        out = pd.concat([meta_df.reset_index(drop=True), record_df.reset_index(drop=True)], axis=1)

    out = _reconstruct_main_multiple_response_parents(out)
    out = _split_main_export_hh_gps(out)

    if "v1" in column_order or "__version__" in out.columns or "formdef_version" in out.columns:
        version_source = out.get("__version__")
        if version_source is None:
            version_source = pd.Series(pd.NA, index=out.index)
        fallback_version = out.get("formdef_version")
        if fallback_version is not None:
            version_source = version_source.where(~_missing_export_value_mask(version_source), fallback_version)
        if "v1" in out.columns:
            out["v1"] = out["v1"].where(~_missing_export_value_mask(out["v1"]), version_source)
        else:
            out["v1"] = version_source
        if "v1" not in column_order:
            column_order.append("v1")

    for date_col in ("SubmissionDate", "CompletionDate", "start", "end", "today"):
        if date_col in out.columns:
            parsed = pd.to_datetime(out[date_col], errors="coerce")
            out[date_col] = out[date_col].where(parsed.isna(), parsed)

    for col in out.columns:
        if col not in column_order:
            column_order.append(col)
    return out.reindex(columns=column_order)

def build_main_survey_case_export_dataframe(
    settings: Settings,
    user: AuthUser,
    statuses: list[str] | None = None,
    final_outcome_codes: list[str] | None = None,
) -> pd.DataFrame:
    allowed_statuses = statuses or ["approved"]
    final_outcomes = [str(item).strip() for item in (final_outcome_codes or []) if str(item).strip()]
    final_outcome_clause = ""
    params: list[Any] = [allowed_statuses]
    if final_outcomes:
        final_outcome_clause = "AND TRIM(COALESCE(m.record->>'final_outcome_code', '')) = ANY(%s)"
        params.append(final_outcomes)

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    m.submission_key,
                    m.case_id,
                    m.ea_id,
                    m.interviewer_id,
                    m.supervisor_id,
                    m.approval_stage,
                    m.is_callback_required,
                    m.submitted_at,
                    m.reviewed_at,
                    m.approved_at,
                    dmc.deleted_at,
                    COALESCE(NULLIF(TRIM(du.full_name), ''), NULLIF(TRIM(du.username), ''), dmc.deleted_by) AS deleted_by,
                    dmc.reason AS deletion_reason,
                    COALESCE(qc.auto_flagged_qc_issue_count, 0)::int AS auto_flagged_qc_issue_count,
                    qc.auto_flagged_qc_issue_codes,
                    qc.auto_flagged_qc_issues
                FROM clean.main_case m
                LEFT JOIN clean.deleted_main_cases dmc ON dmc.submission_key = m.submission_key
                LEFT JOIN app.user_account du ON du.user_id::text = dmc.deleted_by
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(*) FILTER (WHERE COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved')::int AS auto_flagged_qc_issue_count,
                        STRING_AGG(DISTINCT rr.rule_code, ', ' ORDER BY rr.rule_code)
                            FILTER (WHERE COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved') AS auto_flagged_qc_issue_codes,
                        STRING_AGG(
                            DISTINCT COALESCE(NULLIF(TRIM(iq.issue_summary), ''), NULLIF(TRIM(rr.result_message), ''), rr.rule_code),
                            ' | '
                            ORDER BY COALESCE(NULLIF(TRIM(iq.issue_summary), ''), NULLIF(TRIM(rr.result_message), ''), rr.rule_code)
                        ) FILTER (WHERE COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved') AS auto_flagged_qc_issues
                    FROM qc.issue_queue iq
                    INNER JOIN qc.rule_result rr ON rr.rule_result_id = iq.rule_result_id
                    WHERE iq.instrument_code = 'main'
                      AND COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), '')) =
                          COALESCE(NULLIF(TRIM(m.submission_key), ''), NULLIF(TRIM(m.case_id), ''))
                ) qc ON TRUE
                WHERE m.approval_stage = ANY(%s)
                  {final_outcome_clause}
                ORDER BY m.submitted_at DESC NULLS LAST
                """,
                tuple(params),
            )
            rows = cur.fetchall()

    df = pd.DataFrame(rows, columns=MAIN_EXPORT_COLUMNS) if rows else pd.DataFrame(columns=MAIN_EXPORT_COLUMNS)
    for col in ("submitted_at", "reviewed_at", "approved_at", "deleted_at"):
        if col in df.columns:
            df[col] = df[col].astype(str).replace("None", "").replace("NaT", "")
    return df


def list_main_exports(settings: Settings, user: AuthUser) -> list[dict[str, Any]]:
    """Return completed export files plus visible running/failed jobs.

    The frontend polls this endpoint after queueing an export.  Returning the
    queued job status prevents the page from looking stuck when a background
    job is still running or has failed before a file is registered.
    """
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH completed_files AS (
                    SELECT
                        fc.file_id::text AS file_id,
                        fc.export_job_id::text AS export_job_id,
                        fc.export_profile,
                        fc.export_format,
                        fc.file_name,
                        fc.file_path,
                        fc.generated_at,
                        fc.row_count,
                        fc.byte_size,
                        ej.job_status,
                        ej.job_message,
                        ej.created_at,
                        ej.finished_at,
                        true AS download_ready
                    FROM export.file_catalog fc
                    JOIN export.export_job ej
                        ON ej.export_job_id = fc.export_job_id
                    WHERE fc.instrument_code = 'main'
                      AND ej.requested_by_user_id = %s
                      AND ej.job_status = 'completed'
                ), visible_jobs AS (
                    SELECT
                        ej.export_job_id::text AS file_id,
                        ej.export_job_id::text AS export_job_id,
                        ej.export_profile,
                        ej.export_format,
                        CASE
                            WHEN ej.job_status = 'running' THEN 'Generating main survey export…'
                            WHEN ej.job_status = 'cancelled' THEN 'Main survey export cancelled'
                            WHEN ej.job_status = 'failed' THEN 'Main survey export failed'
                            ELSE 'Main survey export'
                        END AS file_name,
                        '' AS file_path,
                        COALESCE(ej.finished_at, ej.started_at, ej.created_at) AS generated_at,
                        0 AS row_count,
                        0 AS byte_size,
                        ej.job_status,
                        ej.job_message,
                        ej.created_at,
                        ej.finished_at,
                        false AS download_ready
                    FROM export.export_job ej
                    WHERE ej.instrument_code = 'main'
                      AND ej.requested_by_user_id = %s
                      AND ej.job_status IN ('running', 'failed', 'cancelled')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM export.file_catalog fc
                          WHERE fc.export_job_id = ej.export_job_id
                            AND fc.instrument_code = 'main'
                      )
                )
                SELECT * FROM completed_files
                UNION ALL
                SELECT * FROM visible_jobs
                ORDER BY generated_at DESC NULLS LAST
                LIMIT 100
                """,
                (user.id, user.id),
            )
            return cur.fetchall()




def clear_main_exports(settings: Settings, user: AuthUser) -> dict[str, Any]:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM export.export_job
                WHERE requested_by_user_id = %s
                  AND instrument_code = 'main'
                """,
                (user.id,),
            )
            deleted = cur.rowcount or 0
        conn.commit()
    return {"deleted": deleted}


def _prepare_main_delivery_dataframe(settings: Settings, df: pd.DataFrame) -> pd.DataFrame:
    ordered = _clean_main_export_dataframe(df)
    dict_order, _labels, _measures, _display_widths, _storage_widths, _value_labels = _main_export_column_meta(str(settings.root_dir))
    template_meta = _main_export_template_meta(str(settings.root_dir))

    if template_meta is not None:
        template_cols: list[str] = list(template_meta["column_names"] or [])
        cleaned = ordered
        ordered = cleaned.reindex(columns=template_cols).copy()
        for col in ["approval_stage", "hh_gps_Latitude", "hh_gps_Longitude", "hh_gps_Altitude", "hh_gps_Accuracy"]:
            if col in cleaned.columns and col not in ordered.columns:
                ordered[col] = cleaned[col]
        return ordered

    if dict_order:
        ordered_columns = list(dict_order)
        if "approval_stage" in df.columns and "approval_stage" not in ordered_columns:
            ordered_columns.append("approval_stage")
        for col in df.columns:
            if col not in ordered_columns:
                ordered_columns.append(col)
        return df.reindex(columns=ordered_columns)

    return ordered

def _normalize_excel_cell_value(value: Any) -> Any:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, pd.Timestamp):
        if value.tzinfo is not None:
            value = value.tz_convert(None)
        return value.to_pydatetime()
    return value


def _excel_ready_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize dataframe values in column batches before streaming to XLSX."""
    out = df.copy(deep=False)
    datetime_cols: list[str] = []
    for col in out.columns:
        dtype = out[col].dtype
        if isinstance(dtype, pd.DatetimeTZDtype):
            out[col] = out[col].dt.tz_localize(None)
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            datetime_cols.append(col)
    if datetime_cols:
        out.loc[:, datetime_cols] = out.loc[:, datetime_cols].where(out.loc[:, datetime_cols].notna(), None)
    # openpyxl writes None as blank cells.  Replace dataframe missing values once
    # instead of invoking a Python normalizer for every cell during append.
    return out.astype("object").where(pd.notna(out), None)


def _write_dataframe_to_xlsx(path: Path, df: pd.DataFrame, sheet_name: str = "Sheet1") -> None:
    workbook = Workbook(write_only=True)
    worksheet = workbook.create_sheet(title=(sheet_name or "Sheet1")[:31])
    excel_df = _excel_ready_dataframe(df)
    for row in dataframe_to_rows(excel_df, index=False, header=True):
        worksheet.append(row)
    workbook.save(path)

def _coerce_json_record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _extract_first_coordinate_pair(raw_value: Any) -> tuple[float | None, float | None]:
    if raw_value in {None, ""}:
        return None, None
    if isinstance(raw_value, dict):
        for lat_key, lng_key in (
            ("Latitude", "Longitude"),
            ("latitude", "longitude"),
            ("lat", "lng"),
            ("lat", "lon"),
        ):
            lat = _safe_float(raw_value.get(lat_key))
            lng = _safe_float(raw_value.get(lng_key))
            if lat is not None and lng is not None:
                return lat, lng
        return None, None
    if isinstance(raw_value, (list, tuple)) and len(raw_value) >= 2:
        lat = _safe_float(raw_value[0])
        lng = _safe_float(raw_value[1])
        if lat is not None and lng is not None:
            return lat, lng
    matches = re.findall(r"-?\d+(?:\.\d+)?", str(raw_value))
    if len(matches) < 2:
        return None, None
    lat = _safe_float(matches[0])
    lng = _safe_float(matches[1])
    if lat is None or lng is None:
        return None, None
    return lat, lng


def _extract_main_case_gps(records: list[dict[str, Any]]) -> tuple[float | None, float | None, str | None]:
    for record in records:
        if not isinstance(record, dict):
            continue
        # Try pre-split columns first (populated by export layer if available)
        lat = _safe_float(record.get("hh_gps_Latitude"))
        lng = _safe_float(record.get("hh_gps_Longitude"))
        if lat is not None and lng is not None:
            return lat, lng, "hh_gps_Latitude/hh_gps_Longitude"
        # hh_gps is stored as a space-separated "lat lon alt acc" string in the raw record
        # Fall back to hh_gps_list if hh_gps is absent
        raw = record.get("hh_gps") or record.get("hh_gps_list")
        if raw and isinstance(raw, str):
            parts = raw.strip().split()
            if len(parts) >= 2:
                lat = _safe_float(parts[0])
                lng = _safe_float(parts[1])
                if lat is not None and lng is not None:
                    return lat, lng, "hh_gps"
    return None, None, None


def get_main_survey_ea_overview(settings: Settings, user: AuthUser, ea_id: str) -> dict[str, Any]:
    normalized_ea_id = _normalize_boundary_key(ea_id)
    if not normalized_ea_id:
        raise HTTPException(status_code=400, detail="EA ID is required.")

    with db_connection(settings) as conn:
        postgis = _has_postgis(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    mc.submission_key,
                    mc.case_id,
                    mc.ea_id,
                    mc.approval_stage,
                    mc.submitted_at,
                    mc.interviewer_id,
                    mc.supervisor_id,
                    COALESCE(
                        NULLIF(TRIM(mc.record->>'ea_name'), ''),
                        NULLIF(TRIM(g.properties->>'sd_EA_NAME'), ''),
                        NULLIF(TRIM(mc.record->>'sd_EA_NAME'), ''),
                        NULLIF(TRIM(mc.record->>'name'), ''),
                        COALESCE(NULLIF(TRIM(mc.ea_id), ''), NULLIF(TRIM(mc.record->>'ea_id'), ''), 'Unknown')
                    ) AS ea_name,
                    COALESCE(
                        NULLIF(TRIM(mc.record->>'state_name'), ''),
                        NULLIF(TRIM(g.state_name), ''),
                        NULLIF(TRIM(g.properties->>'sd_STATE_NAME'), ''),
                        'Unknown'
                    ) AS state_name,
                    COALESCE(
                        NULLIF(TRIM(mc.record->>'lga_name'), ''),
                        NULLIF(TRIM(g.lga_name), ''),
                        NULLIF(TRIM(g.properties->>'sd_LGA_NAME'), ''),
                        NULL
                    ) AS lga_name,
                    mc.record
                FROM clean.main_case mc
                LEFT JOIN reference.geo_boundaries_ea g
                    ON g.ea_id = COALESCE(NULLIF(TRIM(mc.ea_id), ''), NULLIF(TRIM(mc.record->>'ea_id'), ''))
                WHERE REGEXP_REPLACE(COALESCE(NULLIF(TRIM(mc.ea_id), ''), NULLIF(TRIM(mc.record->>'ea_id'), ''), ''), '\\.0+$', '') =
                      REGEXP_REPLACE(%s, '\\.0+$', '')
                  AND TRIM(LOWER(COALESCE(mc.record->>'final_outcome_code', ''))) = 'successful'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM clean.deleted_main_cases dmc
                      WHERE dmc.submission_key = mc.submission_key
                  )
                ORDER BY mc.submitted_at DESC NULLS LAST, mc.submission_key
                """,
                (normalized_ea_id,),
            )
            rows = [dict(row) for row in cur.fetchall()]
            if not rows:
                raise HTTPException(status_code=404, detail="No main survey cases found for this EA.")

            case_ids = [str(row.get("case_id") or "").strip() for row in rows if str(row.get("case_id") or "").strip()]
            section_records_by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
            if case_ids:
                cur.execute(
                    """
                    SELECT case_id, record
                    FROM clean.main_case_section
                    WHERE case_id = ANY(%s)
                    """,
                    (case_ids,),
                )
                for section_row in cur.fetchall():
                    case_key = str(section_row.get("case_id") or "").strip()
                    if not case_key:
                        continue
                    section_records_by_case[case_key].append(_coerce_json_record(section_row.get("record")))

            boundary_row = None
            if postgis:
                cur.execute(
                    """
                    SELECT
                        ea_id,
                        boundary_id,
                        state_name,
                        lga_name,
                        ward_name,
                        properties,
                        ST_AsGeoJSON(geom)::jsonb AS geometry
                    FROM reference.geo_boundaries_ea
                    WHERE REGEXP_REPLACE(COALESCE(ea_id, ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '')
                       OR REGEXP_REPLACE(COALESCE(boundary_id, ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '')
                    ORDER BY CASE
                        WHEN REGEXP_REPLACE(COALESCE(ea_id, ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '') THEN 0
                        WHEN REGEXP_REPLACE(COALESCE(boundary_id, ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '') THEN 1
                        ELSE 2
                    END
                    LIMIT 1
                    """,
                    (normalized_ea_id, normalized_ea_id, normalized_ea_id, normalized_ea_id),
                )
            else:
                cur.execute(
                    """
                    SELECT
                        ea_id,
                        boundary_id,
                        state_name,
                        lga_name,
                        ward_name,
                        properties,
                        geom_geojson AS geometry
                    FROM reference.geo_boundaries_ea
                    WHERE REGEXP_REPLACE(COALESCE(ea_id, ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '')
                       OR REGEXP_REPLACE(COALESCE(boundary_id, ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '')
                    ORDER BY CASE
                        WHEN REGEXP_REPLACE(COALESCE(ea_id, ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '') THEN 0
                        WHEN REGEXP_REPLACE(COALESCE(boundary_id, ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '') THEN 1
                        ELSE 2
                    END
                    LIMIT 1
                    """,
                    (normalized_ea_id, normalized_ea_id, normalized_ea_id, normalized_ea_id),
                )
            boundary_row = cur.fetchone()

            target_ea_name = rows[0].get("ea_name") or normalized_ea_id
            target_state_name = rows[0].get("state_name") or "Unknown"
            normalized_ea_name = _normalize_boundary_text(target_ea_name)
            cur.execute(
                """
                SELECT
                    l.listing_row_id::text AS point_id,
                    l.submission_key,
                    l.ea_id,
                    l.row_type,
                    COALESCE(l.sample_flag, false) AS sample_flag,
                    l.gps_lat,
                    l.gps_long,
                    l.record,
                    s.approval_status,
                    COALESCE(NULLIF(TRIM(s.record->>'ea_name'), ''), l.ea_id, %s) AS ea_name,
                    COALESCE(NULLIF(TRIM(s.record->>'state_name'), ''), %s) AS state_name
                FROM clean.hh_listing_long l
                LEFT JOIN clean.hh_sampling_ea s
                    ON s.submission_key = l.submission_key
                WHERE (
                    REGEXP_REPLACE(COALESCE(l.ea_id, ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '')
                    OR REGEXP_REPLACE(COALESCE(l.boundary_id, ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '')
                    OR REGEXP_REPLACE(COALESCE(l.record->>'ea_id', ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '')
                    OR REGEXP_REPLACE(COALESCE(l.record->>'sd_EA_ID', ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '')
                    OR REGEXP_REPLACE(COALESCE(l.record->>'EA_ID', ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '')
                    OR REGEXP_REPLACE(COALESCE(s.ea_id, ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '')
                    OR REGEXP_REPLACE(COALESCE(s.boundary_id, ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '')
                    OR REGEXP_REPLACE(COALESCE(s.record->>'ea_id', ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '')
                    OR REGEXP_REPLACE(COALESCE(s.record->>'sd_EA_ID', ''), '\\.0+$', '') = REGEXP_REPLACE(%s, '\\.0+$', '')
                    OR REGEXP_REPLACE(
                        UPPER(REGEXP_REPLACE(COALESCE(
                            NULLIF(TRIM(l.record->>'ea_name'), ''),
                            NULLIF(TRIM(l.record->>'sd_EA_NAME'), ''),
                            NULLIF(TRIM(l.record->>'name'), ''),
                            NULLIF(TRIM(s.record->>'ea_name'), ''),
                            NULLIF(TRIM(s.record->>'sd_EA_NAME'), '')
                        ), '[^A-Za-z0-9]+', ' ', 'g')),
                        '\\s+', ' ', 'g'
                    ) = %s
                )
                  AND l.gps_lat IS NOT NULL
                  AND l.gps_long IS NOT NULL
                ORDER BY l.listing_row_id
                """,
                (
                    target_ea_name,
                    target_state_name,
                    normalized_ea_id,
                    normalized_ea_id,  # l.boundary_id
                    normalized_ea_id,
                    normalized_ea_id,
                    normalized_ea_id,
                    normalized_ea_id,
                    normalized_ea_id,  # s.boundary_id
                    normalized_ea_id,
                    normalized_ea_id,
                    normalized_ea_name,
                ),
            )
            listing_gps_points = []
            for point_row in cur.fetchall():
                record = _coerce_json_record(point_row.get("record"))
                listing_gps_points.append(
                    {
                        "point_id": point_row.get("point_id"),
                        "submission_key": point_row.get("submission_key"),
                        "ea_id": point_row.get("ea_id"),
                        "row_type": point_row.get("row_type"),
                        "sample_flag": bool(point_row.get("sample_flag")),
                        "gps_lat": point_row.get("gps_lat"),
                        "gps_long": point_row.get("gps_long"),
                        "approval_status": point_row.get("approval_status"),
                        "ea_name": point_row.get("ea_name"),
                        "state_name": point_row.get("state_name"),
                        "sample_status": str(record.get("sample_status") or "").strip() or None,
                    }
                )

    first_row = rows[0]
    ea_name = str(first_row.get("ea_name") or normalized_ea_id)
    state_name = str(first_row.get("state_name") or "Unknown")
    lga_name = first_row.get("lga_name")

    ea_feature: dict[str, Any] | None = None
    if boundary_row and isinstance(boundary_row.get("geometry"), dict):
        boundary_properties = dict(boundary_row.get("properties") or {})
        boundary_properties.setdefault("sd_EA_ID", boundary_row.get("ea_id") or normalized_ea_id)
        boundary_properties.setdefault("boundary_id", boundary_row.get("boundary_id"))
        boundary_properties.setdefault("sd_EA_NAME", ea_name)
        boundary_properties.setdefault("sd_STATE_NAME", boundary_row.get("state_name") or state_name)
        boundary_properties.setdefault("sd_LGA_NAME", boundary_row.get("lga_name") or lga_name)
        boundary_properties.setdefault("sd_WARD_NAME", boundary_row.get("ward_name"))
        ea_feature = {
            "type": "Feature",
            "geometry": boundary_row.get("geometry"),
            "properties": boundary_properties,
        }
    if ea_feature is None:
        ea_feature = _load_boundary_feature_from_zip(
            str(settings.boundary_zip_path),
            normalized_ea_id,
            normalized_ea_id,
            ea_name,
            state_name,
            str(lga_name or ""),
        )

    gps_points: list[dict[str, Any]] = []
    case_summaries: list[dict[str, Any]] = []
    approved_cases = 0
    rejected_cases = 0
    pending_cases = 0

    for row in rows:
        case_id = str(row.get("case_id") or "").strip() or None
        approval_stage = str(row.get("approval_stage") or "").strip() or None
        if approval_stage == "approved":
            approved_cases += 1
        elif approval_stage == "rejected":
            rejected_cases += 1
        else:
            pending_cases += 1

        case_summaries.append(
            {
                "submission_key": str(row.get("submission_key") or ""),
                "case_id": case_id,
                "approval_stage": approval_stage,
                "submitted_at": str(row.get("submitted_at")) if row.get("submitted_at") else None,
                "interviewer_id": row.get("interviewer_id"),
                "supervisor_id": row.get("supervisor_id"),
            }
        )

        main_record = _coerce_json_record(row.get("record"))
        lat, lng, source = _extract_main_case_gps(
            [main_record, *section_records_by_case.get(str(case_id or ""), [])]
        )
        if lat is None or lng is None:
            continue
        gps_points.append(
            {
                "submission_key": str(row.get("submission_key") or ""),
                "case_id": case_id,
                "approval_stage": approval_stage,
                "lat": lat,
                "lng": lng,
                "gps_source": source,
            }
        )

    return {
        "eaId": normalized_ea_id,
        "eaName": ea_name,
        "stateName": state_name,
        "lgaName": lga_name,
        "totalCases": len(case_summaries),
        "approvedCases": approved_cases,
        "rejectedCases": rejected_cases,
        "pendingCases": pending_cases,
        "gpsPoints": gps_points,
        "listingGpsPoints": listing_gps_points,
        "cases": case_summaries,
        "eaFeature": ea_feature,
    }

def _main_export_run_scope(statuses: list[str] | None, final_outcome_codes: list[str] | None = None) -> str:
    """Stable scope string for duplicate detection and catalog traceability."""
    clean_statuses = [str(v).strip() for v in (statuses or []) if str(v).strip()]
    clean_outcomes = [str(v).strip() for v in (final_outcome_codes or []) if str(v).strip()]
    parts = ["statuses=" + ",".join(clean_statuses)]
    if clean_outcomes:
        parts.append("outcomes=" + ",".join(clean_outcomes))
    return ";".join(parts)


class MainExportCancelled(RuntimeError):
    """Raised inside the export worker when a newer request cancels this job."""


def _main_export_job_status(settings: Settings, export_job_id: str | None) -> str | None:
    if not export_job_id:
        return None
    try:
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT job_status
                    FROM export.export_job
                    WHERE export_job_id = %s
                    """,
                    (export_job_id,),
                )
                row = cur.fetchone() or {}
        value = row.get("job_status")
        return str(value) if value is not None else None
    except Exception:
        logger.debug("Could not read main export job status for %s", export_job_id, exc_info=True)
        return None


def _raise_if_main_export_cancelled(settings: Settings, export_job_id: str | None) -> None:
    if not export_job_id:
        return
    status = _main_export_job_status(settings, export_job_id)
    if status == "cancelled":
        raise MainExportCancelled(f"Main survey export job {export_job_id} was cancelled by a newer request.")


def _update_main_export_job_progress(settings: Settings, export_job_id: str | None, message: str) -> None:
    if not export_job_id:
        return
    _raise_if_main_export_cancelled(settings, export_job_id)
    try:
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE export.export_job
                    SET job_message = %s
                    WHERE export_job_id = %s
                      AND job_status = 'running'
                    """,
                    (message[:1000], export_job_id),
                )
            conn.commit()
    except MainExportCancelled:
        raise
    except Exception:
        logger.debug("Could not update main export job progress for %s", export_job_id, exc_info=True)



def _main_export_filter_sql(statuses: list[str], final_outcome_codes: list[str] | None) -> tuple[str, list[Any]]:
    """SQL WHERE clause and params shared by export-cache freshness checks."""
    clauses = ["m.approval_stage = ANY(%s)"]
    params: list[Any] = [statuses]
    final_outcomes = [str(item).strip() for item in (final_outcome_codes or []) if str(item).strip()]
    if final_outcomes:
        clauses.append("TRIM(COALESCE(m.record->>'final_outcome_code', '')) = ANY(%s)")
        params.append(final_outcomes)
    return " AND ".join(clauses), params


def _latest_main_export_data_activity(
    settings: Settings,
    statuses: list[str],
    final_outcome_codes: list[str] | None,
) -> datetime | None:
    """Newest source-table timestamp for the requested main export scope.

    This lets us instantly reuse a completed export when no matching case or
    section row has changed since the file was generated.
    """
    where_sql, params = _main_export_filter_sql(statuses, final_outcome_codes)
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH scoped_cases AS (
                    SELECT m.case_id, m.updated_at
                    FROM clean.main_case m
                    WHERE {where_sql}
                ), activity AS (
                    SELECT MAX(updated_at) AS latest_at FROM scoped_cases
                    UNION ALL
                    SELECT MAX(s.updated_at) AS latest_at
                    FROM clean.main_case_section s
                    JOIN scoped_cases c
                      ON c.case_id = s.case_id
                )
                SELECT MAX(latest_at) AS latest_at
                FROM activity
                """,
                tuple(params),
            )
            row = cur.fetchone() or {}
    latest_at = row.get("latest_at")
    return latest_at if isinstance(latest_at, datetime) else None


def _reusable_main_export_file(
    settings: Settings,
    user: AuthUser,
    profile: str,
    export_format: str,
    statuses: list[str],
    final_outcome_codes: list[str] | None,
) -> dict[str, Any] | None:
    """Return the newest matching completed file if the underlying data is unchanged."""
    run_scope = _main_export_run_scope(statuses, final_outcome_codes)
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    fc.file_id::text AS file_id,
                    fc.export_job_id::text AS export_job_id,
                    fc.file_name,
                    fc.file_path,
                    fc.generated_at,
                    fc.row_count,
                    fc.byte_size
                FROM export.file_catalog fc
                JOIN export.export_job ej
                  ON ej.export_job_id = fc.export_job_id
                WHERE fc.instrument_code = 'main'
                  AND fc.export_profile = %s
                  AND fc.export_format = %s
                  AND fc.is_active = true
                  AND ej.instrument_code = 'main'
                  AND ej.export_profile = %s
                  AND ej.export_format = %s
                  AND ej.run_scope = %s
                  AND ej.requested_by_user_id = %s
                  AND ej.job_status = 'completed'
                ORDER BY fc.generated_at DESC NULLS LAST
                LIMIT 1
                """,
                (profile, export_format, profile, export_format, run_scope, user.id),
            )
            row = cur.fetchone()
    if not row:
        return None

    file_path = Path(str(row.get("file_path") or ""))
    if not file_path.exists():
        return None

    latest_activity = _latest_main_export_data_activity(settings, statuses, final_outcome_codes)
    generated_at = row.get("generated_at")
    if latest_activity and generated_at and latest_activity > generated_at:
        return None

    return {
        "queued": False,
        "cached": True,
        "exportJobId": row.get("export_job_id"),
        "fileId": row.get("file_id"),
        "fileName": row.get("file_name"),
        "message": "No matching main survey records changed since the last export, so the existing file is ready immediately.",
        "statuses": statuses,
        "finalOutcomeCodes": final_outcome_codes or [],
    }


def _normalize_main_export_statuses(user: AuthUser, statuses: list[str] | None) -> list[str]:
    if user.role == "client":
        return ["approved"]
    return statuses or ["approved"]


def queue_main_export(
    settings: Settings,
    user: AuthUser,
    profile: str,
    export_format: str,
    statuses: list[str] | None = None,
    final_outcome_codes: list[str] | None = None,
) -> dict[str, Any]:
    if profile not in {"wide"}:
        raise HTTPException(status_code=400, detail="Unsupported export profile.")
    if export_format not in {"csv", "xlsx", "sav"}:
        raise HTTPException(status_code=400, detail="Unsupported export format.")
    allowed_statuses = _normalize_main_export_statuses(user, statuses)
    run_scope = _main_export_run_scope(allowed_statuses, final_outcome_codes)

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            # Do not do any expensive cache/data-freshness checks inside the
            # request/response path. On Render/Cloudflare that can make
            # POST /api/main-survey/exports exceed the gateway timeout and show
            # a 502 even though the export job row was created. Keep this route
            # lightweight: create/reuse a job immediately, then let the
            # background task decide whether it can reuse a completed file.

            # Stale running jobs can be left behind when the web worker restarts
            # or an export crashes. Mark them failed so they do not block a new
            # export forever.
            cur.execute(
                """
                UPDATE export.export_job
                SET job_status = 'failed',
                    finished_at = COALESCE(finished_at, now()),
                    job_message = COALESCE(job_message, 'Export did not finish.') || ' Marked failed after becoming stale.'
                WHERE instrument_code = 'main'
                  AND job_status = 'running'
                  AND COALESCE(started_at, created_at) < now() - interval '2 hours'
                """
            )

            # A new Main Survey export request supersedes any previous
            # running Main Survey export for this user. We cannot forcibly kill a
            # Python thread safely, so we mark the older job as cancelled in the
            # generated catalog. The worker checks this status between heavy
            # phases and will stop without publishing an old file.
            cur.execute(
                """
                UPDATE export.export_job
                SET job_status = 'cancelled',
                    finished_at = now(),
                    job_message = 'Cancelled because a newer main survey export was requested.'
                WHERE instrument_code = 'main'
                  AND requested_by_user_id = %s
                  AND job_status = 'running'
                """,
                (user.id,),
            )

            export_job_id = str(uuid4())
            cur.execute(
                """
                INSERT INTO export.export_job (
                    export_job_id,
                    instrument_code,
                    export_profile,
                    export_format,
                    run_scope,
                    job_status,
                    requested_by_user_id,
                    started_at,
                    job_message
                )
                VALUES (%s, 'main', %s, %s, %s, 'running', %s, now(), %s)
                """,
                (
                    export_job_id,
                    profile,
                    export_format,
                    run_scope,
                    user.id,
                    "Export generation started. Preparing data…",
                ),
            )
        conn.commit()

    return {
        "queued": True,
        "exportJobId": export_job_id,
        "fileId": export_job_id,
        "fileName": f"main_{profile}_{export_format}_running",
        "message": "Export generation started. The download will appear when the file is ready.",
        "alreadyRunning": False,
        "statuses": allowed_statuses,
        "finalOutcomeCodes": final_outcome_codes or [],
    }


def run_queued_main_export(
    settings: Settings,
    export_job_id: str,
    requested_by_user_id: str,
    user: AuthUser,
    profile: str,
    export_format: str,
    statuses: list[str],
    final_outcome_codes: list[str] | None = None,
) -> None:
    try:
        _create_main_export_artifact(
            settings,
            user,
            profile,
            export_format,
            statuses,
            final_outcome_codes,
            requested_by_user_id=requested_by_user_id,
            export_job_id=export_job_id,
        )
    except MainExportCancelled:
        logger.info("Queued main survey export %s stopped after cancellation.", export_job_id)
    except Exception as exc:
        logger.exception("Queued main survey export failed for job %s.", export_job_id)
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE export.export_job
                    SET job_status = 'failed',
                        finished_at = now(),
                        job_message = %s
                    WHERE export_job_id = %s
                      AND job_status = 'running'
                    """,
                    (str(exc)[:1000], export_job_id),
                )
            conn.commit()


def create_main_export(
    settings: Settings,
    user: AuthUser,
    profile: str,
    export_format: str,
    statuses: list[str] | None = None,
    final_outcome_codes: list[str] | None = None,
) -> dict[str, Any]:
    return _create_main_export_artifact(
        settings,
        user,
        profile,
        export_format,
        _normalize_main_export_statuses(user, statuses),
        final_outcome_codes,
        requested_by_user_id=user.id,
    )


def _create_main_export_artifact(
    settings: Settings,
    user: AuthUser,
    profile: str,
    export_format: str,
    statuses: list[str] | None = None,
    final_outcome_codes: list[str] | None = None,
    requested_by_user_id: str | None = None,
    export_job_id: str | None = None,
) -> dict[str, Any]:
    if profile not in {"wide"}:
        raise HTTPException(status_code=400, detail="Unsupported export profile.")
    if export_format not in {"csv", "xlsx", "sav"}:
        raise HTTPException(status_code=400, detail="Unsupported export format.")
    allowed_statuses = statuses if statuses else ["approved"]
    _update_main_export_job_progress(settings, export_job_id, "Loading main survey records…")
    raw_df = build_main_survey_wide_export_dataframe(settings, user, allowed_statuses, final_outcome_codes)
    _raise_if_main_export_cancelled(settings, export_job_id)
    _update_main_export_job_progress(settings, export_job_id, f"Preparing {len(raw_df):,} rows for export…")
    df = _prepare_main_delivery_dataframe(settings, raw_df)
    _raise_if_main_export_cancelled(settings, export_job_id)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    export_job_id = export_job_id or str(uuid4())
    requested_by_user_id = requested_by_user_id or user.id
    outcome_slug = "_".join(str(v).strip().replace(" ", "-") for v in (final_outcome_codes or []) if str(v).strip())
    stem_parts = ["main", profile, "_".join(allowed_statuses)]
    if outcome_slug:
        stem_parts.append(outcome_slug)
    stem_parts.append(timestamp)
    stem = "_".join(stem_parts)

    if export_format == "csv":
        _update_main_export_job_progress(settings, export_job_id, f"Writing CSV file for {len(df):,} rows…")
        path = settings.export_dir / f"{stem}.csv"
        df.to_csv(path, index=False, encoding="utf-8")
        _raise_if_main_export_cancelled(settings, export_job_id)
    elif export_format == "xlsx":
        _update_main_export_job_progress(settings, export_job_id, f"Writing Excel workbook for {len(df):,} rows…")
        path = settings.export_dir / f"{stem}.xlsx"
        _write_dataframe_to_xlsx(path, df)
        _raise_if_main_export_cancelled(settings, export_job_id)
    else:
        try:
            _update_main_export_job_progress(settings, export_job_id, f"Writing SPSS SAV file for {len(df):,} rows. This is the slowest export format…")
            spss_started = time.perf_counter()
            # Load full metadata from MAIN_data_dictionary.xlsx
            _dict_order, dict_labels, dict_measures, dict_display_widths, dict_storage_widths, dict_value_labels = (
                _main_export_column_meta(str(settings.root_dir))
            )

            template_meta = _main_export_template_meta(str(settings.root_dir))
            if template_meta is not None:
                template_cols: list[str] = list(template_meta["column_names"])
                out = df.reindex(columns=template_cols).copy()
                extra_cols: list[str] = []
                if "approval_stage" in df.columns and "approval_stage" not in out.columns:
                    extra_cols.append("approval_stage")
                for _gps_col in ["hh_gps_Latitude", "hh_gps_Longitude", "hh_gps_Altitude", "hh_gps_Accuracy"]:
                    if _gps_col in df.columns and _gps_col not in out.columns:
                        extra_cols.append(_gps_col)
                for _extra_col in extra_cols:
                    out[_extra_col] = df[_extra_col]

                raw_labels = list(template_meta["column_labels"])
                col_labels: list[str] = [
                    (raw_labels[i] if i < len(raw_labels) else dict_labels.get(col, col))
                    for i, col in enumerate(template_cols)
                ]
                col_labels.extend(dict_labels.get(col, col.replace("_", " ").title()) for col in extra_cols)
                raw_vvl: dict = dict(template_meta["variable_value_labels"])
                var_measure = dict(template_meta["variable_measure"])
                var_display_width = dict(template_meta["variable_display_width"])
                original_types = dict(template_meta["original_variable_types"])
                var_format = dict(original_types)
                missing_ranges = dict(template_meta["missing_ranges"])
                if "approval_stage" in out.columns:
                    var_measure.setdefault("approval_stage", "nominal")
                    var_display_width.setdefault("approval_stage", 16)
                    raw_vvl.setdefault(
                        "approval_stage",
                        {
                            "approved": "Approved",
                            "pending_review": "Pending Review",
                            "in_review": "In Review",
                            "corrected": "Corrected",
                            "rejected": "Rejected",
                            "submitted": "Submitted",
                        },
                    )
                out, vvl = _coerce_template_dataframe_for_sav(out, list(out.columns), original_types, raw_vvl)
            else:
                out = df.copy()
                col_labels = [dict_labels.get(col, col) for col in out.columns]
                raw_vvl = {k: v for k, v in dict_value_labels.items() if k in out.columns}
                var_measure = {col: dict_measures[col] for col in out.columns if col in dict_measures}
                var_display_width = {col: dict_display_widths[col] for col in out.columns if col in dict_display_widths}
                var_format = {}
                missing_ranges = {}
                out, vvl = _coerce_template_dataframe_for_sav(out, list(out.columns), {}, raw_vvl)

            bool_cols = [col for col in out.columns if pd.api.types.is_bool_dtype(out[col])]
            if bool_cols:
                for col in bool_cols:
                    out[col] = out[col].astype("int64")
            object_cols = [col for col in out.columns if pd.api.types.is_object_dtype(out[col])]
            if object_cols:
                for col in object_cols:
                    out[col] = (
                        out[col]
                        .astype("string")
                        .fillna("")
                        .replace({"nan": "", "None": "", "NaT": ""})
                        .astype(object)
                    )
            logger.info(
                "Main survey SPSS export prepared dataframe rows=%s cols=%s in %.2fs",
                len(out),
                len(out.columns),
                time.perf_counter() - spss_started,
            )

            with tempfile.NamedTemporaryFile(suffix=".sav", delete=False) as tmp:
                sav_path = Path(tmp.name)

            write_kwargs: dict = {
                "column_labels": col_labels,
                "variable_value_labels": vvl,
            }
            if var_measure:
                write_kwargs["variable_measure"] = {col: value for col, value in var_measure.items() if col in out.columns}
            if var_display_width:
                write_kwargs["variable_display_width"] = {col: value for col, value in var_display_width.items() if col in out.columns}
            if var_format:
                write_kwargs["variable_format"] = {col: value for col, value in var_format.items() if col in out.columns}
            if missing_ranges:
                write_kwargs["missing_ranges"] = {col: value for col, value in missing_ranges.items() if col in out.columns and value}

            try:
                write_started = time.perf_counter()
                _write_sav_with_fallback(out, sav_path, write_kwargs)
                logger.info(
                    "Main survey SPSS SAV write completed rows=%s cols=%s size_mb=%.1f in %.2fs",
                    len(out),
                    len(out.columns),
                    sav_path.stat().st_size / (1024 * 1024),
                    time.perf_counter() - write_started,
                )
                _raise_if_main_export_cancelled(settings, export_job_id)
            except MainExportCancelled:
                sav_path.unlink(missing_ok=True)
                raise
            except Exception:
                # Last-resort minimal SAV so export generation still succeeds.
                fallback_df = out.copy()
                for col in fallback_df.columns:
                    series = fallback_df[col]
                    if pd.api.types.is_datetime64_any_dtype(series):
                        fallback_df[col] = series.astype(str).replace({"NaT": ""})
                    elif pd.api.types.is_object_dtype(series):
                        fallback_df[col] = series.astype(str).replace({"nan": "", "None": "", "NaT": ""})
                write_started = time.perf_counter()
                pyreadstat.write_sav(fallback_df, str(sav_path), column_labels=col_labels[: len(fallback_df.columns)])
                logger.info(
                    "Main survey SPSS minimal fallback SAV write completed rows=%s cols=%s size_mb=%.1f in %.2fs",
                    len(fallback_df),
                    len(fallback_df.columns),
                    sav_path.stat().st_size / (1024 * 1024),
                    time.perf_counter() - write_started,
                )
                _raise_if_main_export_cancelled(settings, export_job_id)

            _update_main_export_job_progress(settings, export_job_id, "Compressing SPSS ZIP package for a smaller download…")
            path = settings.export_dir / f"{stem}.zip"
            zip_started = time.perf_counter()
            with zipfile.ZipFile(
                path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=MAIN_SURVEY_ZIP_COMPRESSION_LEVEL,
            ) as zf:
                zf.write(sav_path, "main_survey_cases.sav")
            logger.info(
                "Main survey SPSS ZIP compression completed sav_mb=%.1f zip_mb=%.1f level=%s in %.2fs total_spss_seconds=%.2f",
                sav_path.stat().st_size / (1024 * 1024),
                path.stat().st_size / (1024 * 1024),
                MAIN_SURVEY_ZIP_COMPRESSION_LEVEL,
                time.perf_counter() - zip_started,
                time.perf_counter() - spss_started,
            )
            sav_path.unlink(missing_ok=True)
            _raise_if_main_export_cancelled(settings, export_job_id)
        except MainExportCancelled:
            logger.info("Main survey SAV export cancelled for job %s", export_job_id)
            raise
        except Exception as exc:
            logger.exception("Main survey SAV export failed")
            raise HTTPException(status_code=500, detail=f"SPSS export failed: {exc}") from exc

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO export.export_job (
                    export_job_id,
                    instrument_code,
                    export_profile,
                    export_format,
                    run_scope,
                    job_status,
                    requested_by_user_id,
                    started_at,
                    finished_at,
                    job_message
                )
                VALUES (%s, 'main', %s, %s, %s, 'completed', %s, now(), now(), %s)
                ON CONFLICT (export_job_id) DO UPDATE SET
                    job_status = 'completed',
                    started_at = COALESCE(export.export_job.started_at, EXCLUDED.started_at),
                    finished_at = EXCLUDED.finished_at,
                    job_message = EXCLUDED.job_message
                WHERE export.export_job.job_status = 'running'
                RETURNING export_job_id::text
                """,
                (
                    export_job_id,
                    profile,
                    export_format,
                    _main_export_run_scope(allowed_statuses, final_outcome_codes),
                    requested_by_user_id,
                    f"Generated {len(df)} rows. Ready to download.",
                ),
            )
            completed_job = cur.fetchone()
            if not completed_job:
                # A newer request cancelled this job while the file was being
                # written. Do not publish the old artifact into the catalog.
                conn.rollback()
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    logger.debug("Could not remove cancelled export artifact %s", path, exc_info=True)
                raise MainExportCancelled(f"Main survey export job {export_job_id} was cancelled before catalog publish.")

            cur.execute(
                """
                UPDATE export.file_catalog
                SET is_active = false
                WHERE instrument_code = 'main'
                  AND export_profile = %s
                  AND export_format = %s
                """,
                (profile, export_format),
            )
            file_id = str(uuid4())
            cur.execute(
                """
                INSERT INTO export.file_catalog (
                    file_id,
                    export_job_id,
                    instrument_code,
                    export_profile,
                    export_format,
                    file_name,
                    file_path,
                    row_count,
                    byte_size,
                    generated_at,
                    is_active
                )
                VALUES (%s, %s, 'main', %s, %s, %s, %s, %s, %s, now(), true)
                """,
                (
                    file_id,
                    export_job_id,
                    profile,
                    export_format,
                    path.name,
                    str(path),
                    len(df),
                    path.stat().st_size,
                ),
            )
        conn.commit()

    return {"fileId": file_id, "exportJobId": export_job_id, "fileName": path.name}


def get_main_export_file(settings: Settings, user: AuthUser, file_id: str) -> dict[str, Any]:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    fc.file_id::text AS file_id,
                    fc.file_name,
                    fc.file_path,
                    fc.export_format
                FROM export.file_catalog fc
                JOIN export.export_job ej
                    ON ej.export_job_id = fc.export_job_id
                WHERE fc.file_id = %s
                  AND fc.instrument_code = 'main'
                  AND ej.requested_by_user_id = %s
                """,
                (file_id, user.id),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Export file not found.")
    return row
