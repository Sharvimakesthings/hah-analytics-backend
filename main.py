from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
import io
from supabase import create_client
from dotenv import load_dotenv
import os
from datetime import datetime

load_dotenv()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
    expose_headers=["*"]
)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


# ─── HELPERS ────────────────────────────────────────────────────

def normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase, strip, replace spaces with underscores."""
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    return df

def clean_str_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Strip whitespace and title-case all string columns."""
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].astype(str).str.strip()
        df[col] = df[col].replace({"nan": None, "None": None, "": None})
    return df

def to_null(df: pd.DataFrame) -> pd.DataFrame:
    """Replace NaN/blanks with None for JSON serialization."""
    df = df.where(pd.notnull(df), None)
    # Extra safety: convert any remaining nan in numeric cols to None
    for col in df.columns:
        df[col] = df[col].apply(lambda x: None if isinstance(x, float) and (x != x) else x)
    return df

def chunk_insert(table: str, records: list, chunk_size: int = 100) -> int:
    inserted = 0
    for i in range(0, len(records), chunk_size):
        chunk = records[i:i + chunk_size]
        supabase.table(table).insert(chunk).execute()
        inserted += len(chunk)
    return inserted


# ─── SURVEY UPLOAD ───────────────────────────────────────────────

@app.post("/upload/survey")
async def upload_survey(file: UploadFile = File(...)):
    contents = await file.read()
    ext = file.filename.split(".")[-1].lower()

    if ext not in ["xlsx", "xls", "csv"]:
        raise HTTPException(status_code=400, detail="Only .xlsx, .xls, or .csv files are supported.")

    try:
        if ext == "csv":
            members_df = pd.read_csv(io.BytesIO(contents))
            household_df = None
        else:
            xl = pd.ExcelFile(io.BytesIO(contents))
            sheet_names = xl.sheet_names

            # Find the members sheet and household sheet by name (case-insensitive)
            members_sheet = None
            household_sheet = None
            for s in sheet_names:
                sl = s.strip().lower()
                if "member" in sl:
                    members_sheet = s
                elif "household" in sl or "house" in sl:
                    household_sheet = s

            if members_sheet is None:
                # Fall back: first sheet is members
                members_sheet = sheet_names[0]
            if household_sheet is None and len(sheet_names) > 1:
                household_sheet = sheet_names[1]

            members_df = xl.parse(members_sheet)
            household_df = xl.parse(household_sheet) if household_sheet else None

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file: {str(e)}")

    # ── Normalize column names ──
    members_df = normalize_cols(members_df)
    if household_df is not None:
        household_df = normalize_cols(household_df)

    # ── Map members columns to standard names ──
    col_map = {}
    for col in members_df.columns:
        cl = col.lower()
        if "male or female" in cl or cl == "gender":
            col_map[col] = "gender"
        elif "age" in cl and "completed" in cl:
            col_map[col] = "age"
        elif "marital" in cl:
            col_map[col] = "marital_status"
        elif "disability" in cl:
            col_map[col] = "disability"
        elif "access to any social security" in cl or "awareness_scheme1" in cl:
            col_map[col] = "awareness_scheme1"
        elif "which schemes" in cl and "others" not in cl:
            col_map[col] = "awareness_scheme2"
        elif "parent_index" in cl:
            col_map[col] = "parent_index"
        elif col == "sl_no.":
            col_map[col] = "sl_no"

    members_df = members_df.rename(columns=col_map)

    # ── Map household columns ──
    if household_df is not None:
        hh_col_map = {}
        for col in household_df.columns:
            cl = col.lower()
            if "name_of_state" in cl or cl == "state":
                hh_col_map[col] = "state"
            elif "block" in cl or "municipality" in cl or "district" in cl:
                hh_col_map[col] = "district"
            elif cl == "month":
                hh_col_map[col] = "month"
            elif "household_number" in cl:
                hh_col_map[col] = "household_number"
            elif col in ["sl_no.", "sl_no"]:
                hh_col_map[col] = "hh_sl_no"
            elif col == "_id":
                hh_col_map[col] = "hh_id"

        household_df = household_df.rename(columns=hh_col_map)

        # Join members with household on parent_index → hh_sl_no
        if "parent_index" in members_df.columns and "hh_sl_no" in household_df.columns:
            household_df["hh_sl_no"] = pd.to_numeric(household_df["hh_sl_no"], errors="coerce")
            members_df["parent_index"] = pd.to_numeric(members_df["parent_index"], errors="coerce")
            members_df = members_df.merge(
                household_df[["hh_sl_no", "state", "district", "month"]],
                left_on="parent_index",
                right_on="hh_sl_no",
                how="left"
            )

    # ── Validate required columns ──
    required = ["gender", "age"]
    missing = [r for r in required if r not in members_df.columns]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required columns after mapping: {missing}. Found columns: {list(members_df.columns)}")

    # ── Clean data ──
    members_df = clean_str_cols(members_df)
    members_df = to_null(members_df)
    members_df = members_df.drop_duplicates()

    # ── Normalize gender values ──
    if "gender" in members_df.columns:
        members_df["gender"] = members_df["gender"].apply(lambda x:
            "Male" if str(x).strip().lower() in ["male", "m", "1"] else
            "Female" if str(x).strip().lower() in ["female", "f", "2"] else x
        )

    # ── Normalize disability values ──
    if "disability" in members_df.columns:
        members_df["disability"] = members_df["disability"].apply(lambda x:
            "Yes" if str(x).strip().lower() in ["yes", "y", "1", "true"] else
            "No" if str(x).strip().lower() in ["no", "n", "0", "false", "none"] else x
        )

    # ── Derive age groups ──
    if "age" in members_df.columns:
        members_df["age"] = pd.to_numeric(members_df["age"], errors="coerce")
        def age_group(a):
            if pd.isna(a): return None
            if a < 18: return "Under 18"
            if a <= 25: return "18-25"
            if a <= 35: return "26-35"
            if a <= 50: return "36-50"
            return "51+"
        members_df["age_group"] = members_df["age"].apply(age_group)

    # ── Select only columns that exist in our DB schema ──
    keep = ["state", "district", "gender", "age", "age_group", "disability",
            "marital_status", "awareness_scheme1", "awareness_scheme2", "month", "quarter"]
    final_cols = [c for c in keep if c in members_df.columns]
    members_df = members_df[final_cols]

    records = members_df.to_dict(orient="records")
    inserted = chunk_insert("survey_data", records)

    supabase.table("upload_logs").insert({
        "filename": file.filename,
        "upload_type": "survey",
        "records_processed": inserted,
        "uploaded_at": datetime.utcnow().isoformat()
    }).execute()

    return {"status": "success", "records_inserted": inserted}


# ─── BENEFICIARY / SCHEME UPLOAD ─────────────────────────────────

@app.post("/upload/beneficiary")
async def upload_beneficiary(file: UploadFile = File(...)):
    contents = await file.read()
    ext = file.filename.split(".")[-1].lower()

    if ext not in ["xlsx", "xls", "csv"]:
        raise HTTPException(status_code=400, detail="Only .xlsx, .xls, or .csv files are supported.")

    try:
        if ext == "csv":
            df = pd.read_csv(io.BytesIO(contents))
        else:
            xl = pd.ExcelFile(io.BytesIO(contents))
            df = xl.parse(xl.sheet_names[0])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file: {str(e)}")

    df = normalize_cols(df)

    # ── Map scheme file columns to standard names ──
    col_map = {}
    for col in df.columns:
        cl = col.lower()
        if "applicant" in cl and "name" in cl:
            col_map[col] = "applicant_name"
        elif "applied_scheme" in cl or "scheme_name" in cl or ("scheme" in cl and "applied" in cl):
            col_map[col] = "scheme_name"
        elif "application_status" in cl or cl == "status":
            col_map[col] = "status"
        elif "location" in cl and "district" not in cl:
            col_map[col] = "district"
        elif "district" in cl:
            col_map[col] = "district"
        elif cl == "state":
            col_map[col] = "state"
        elif "mobile" in cl:
            col_map[col] = "mobile_number"
        elif cl == "month":
            col_map[col] = "month"
        elif "gender" in cl:
            col_map[col] = "gender"
        elif cl == "age":
            col_map[col] = "age"
        elif "ph/mh" in cl or "disability" in cl or cl == "ph_mh_none" or "ph" in cl:
            col_map[col] = "disability"
        elif "acknowledgment" in cl or "application_no" in cl or "acknowledgment_no" in cl:
            col_map[col] = "application_no"
        elif cl == "cat" or "category" in cl:
            col_map[col] = "category"
        elif cl == "month":
            col_map[col] = "month"
        elif "applied_date" in cl or "date" in cl:
            col_map[col] = "applied_date"

    df = df.rename(columns=col_map)

    # ── Validate required columns ──
    required = ["applicant_name", "scheme_name", "status", "district"]
    missing = [r for r in required if r not in df.columns]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required columns after mapping: {missing}. Found columns: {list(df.columns)}")

    # ── Clean data ──
    df = clean_str_cols(df)
    df = to_null(df)

    # ── Normalize status values ──
    if "status" in df.columns:
        df["status"] = df["status"].apply(lambda x:
            "Approved" if str(x).strip().lower() in ["approved", "approve", "sanctioned", "granted", "yes"] else
            "Rejected" if str(x).strip().lower() in ["rejected", "reject", "denied", "no"] else
            "Pending" if str(x).strip().lower() in ["pending", "in progress", "under review", "processing"] else
            str(x).strip().title() if x else "Pending"
        )

    # ── Deduplicate against existing DB records ──
    dupes_skipped = 0
    if "mobile_number" in df.columns:
        try:
            existing = supabase.table("beneficiary_data").select(
                "mobile_number,applicant_name,district,scheme_name"
            ).execute()

            existing_keys = set()
            for row in existing.data:
                key = (
                    str(row.get("mobile_number", "")).strip(),
                    str(row.get("applicant_name", "")).strip().lower(),
                    str(row.get("district", "")).strip().lower(),
                    str(row.get("scheme_name", "")).strip().lower(),
                )
                existing_keys.add(key)

            def is_duplicate(row):
                key = (
                    str(row.get("mobile_number", "")).strip(),
                    str(row.get("applicant_name", "")).strip().lower(),
                    str(row.get("district", "")).strip().lower(),
                    str(row.get("scheme_name", "")).strip().lower(),
                )
                return key in existing_keys

            records_all = df.to_dict(orient="records")
            records = [r for r in records_all if not is_duplicate(r)]
            dupes_skipped = len(records_all) - len(records)
        except Exception:
            records = df.to_dict(orient="records")
    else:
        df = df.drop_duplicates()
        records = df.to_dict(orient="records")

    # ── Select only DB schema columns ──
    keep = ["applicant_name", "mobile_number", "scheme_name", "status",
            "district", "state", "month", "quarter", "gender", "age",
            "disability", "category", "application_no", "applied_date", "remarks"]
    records = [{k: v for k, v in r.items() if k in keep} for r in records]

    inserted = chunk_insert("beneficiary_data", records)

    supabase.table("upload_logs").insert({
        "filename": file.filename,
        "upload_type": "beneficiary",
        "records_processed": inserted,
        "uploaded_at": datetime.utcnow().isoformat()
    }).execute()

    return {
        "status": "success",
        "records_inserted": inserted,
        "duplicates_skipped": dupes_skipped
    }


# ─── STATS ENDPOINTS ─────────────────────────────────────────────

@app.get("/stats/overview")
async def overview():
    surveys = supabase.table("survey_data").select("id", count="exact").execute()
    beneficiaries = supabase.table("beneficiary_data").select("id", count="exact").execute()
    uploads = supabase.table("upload_logs").select("*").order("uploaded_at", desc=True).limit(10).execute()
    return {
        "total_surveys": surveys.count,
        "total_beneficiaries": beneficiaries.count,
        "recent_uploads": uploads.data
    }


@app.get("/stats/survey")
async def survey_stats(state: str = None, district: str = None, gender: str = None):
    query = supabase.table("survey_data").select(
        "state,district,gender,age,age_group,disability,marital_status,awareness_scheme1,awareness_scheme2,month,quarter"
    )
    if state:
        query = query.eq("state", state)
    if district:
        query = query.eq("district", district)
    if gender:
        query = query.eq("gender", gender)
    result = query.execute()
    return result.data


@app.get("/stats/beneficiary")
async def beneficiary_stats(state: str = None, district: str = None, scheme: str = None, status: str = None):
    query = supabase.table("beneficiary_data").select(
        "applicant_name,scheme_name,status,district,state,month,quarter,gender,age,disability,category"
    )
    if state:
        query = query.eq("state", state)
    if district:
        query = query.eq("district", district)
    if scheme:
        query = query.eq("scheme_name", scheme)
    if status:
        query = query.eq("status", status)
    result = query.execute()
    return result.data


@app.get("/logs")
async def upload_logs():
    result = supabase.table("upload_logs").select("*").order("uploaded_at", desc=True).execute()
    return result.data