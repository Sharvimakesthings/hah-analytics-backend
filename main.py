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

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def clean_df(df: pd.DataFrame) -> pd.DataFrame:
    # Strip whitespace from all string columns
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].str.strip().str.title()
    # Replace blanks with None
    df = df.where(pd.notnull(df), None)
    # Remove exact duplicate rows
    df = df.drop_duplicates()
    return df

@app.post("/upload/survey")
async def upload_survey(file: UploadFile = File(...)):
    contents = await file.read()
    ext = file.filename.split(".")[-1].lower()
    
    try:
        if ext == "csv":
            df = pd.read_csv(io.BytesIO(contents))
        else:
            df = pd.read_excel(io.BytesIO(contents))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file: {str(e)}")

    # Normalize column names
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    
    required = ["state", "district", "gender", "age"]
    missing = [r for r in required if r not in df.columns]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required columns: {missing}")

    df = clean_df(df)
    
    # Chunk insert — handles 100k rows without timeout
    records = df.to_dict(orient="records")
    chunk_size = 500
    inserted = 0
    for i in range(0, len(records), chunk_size):
        chunk = records[i:i+chunk_size]
        supabase.table("survey_data").insert(chunk).execute()
        inserted += len(chunk)

    # Log the upload
    supabase.table("upload_logs").insert({
        "filename": file.filename,
        "upload_type": "survey",
        "records_processed": inserted,
        "uploaded_at": datetime.utcnow().isoformat()
    }).execute()

    return {"status": "success", "records_inserted": inserted}


@app.post("/upload/beneficiary")
async def upload_beneficiary(file: UploadFile = File(...)):
    contents = await file.read()
    ext = file.filename.split(".")[-1].lower()
    
    try:
        if ext == "csv":
            df = pd.read_csv(io.BytesIO(contents))
        else:
            df = pd.read_excel(io.BytesIO(contents))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file: {str(e)}")

    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    
    required = ["applicant_name", "scheme_name", "status", "district"]
    missing = [r for r in required if r not in df.columns]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required columns: {missing}")

    df = clean_df(df)

    # Deduplicate against what's already in the DB using mobile+name+district+scheme
    if "mobile_number" in df.columns:
        existing = supabase.table("beneficiary_data").select("mobile_number,applicant_name,district,scheme_name").execute()
        existing_keys = set()
        for row in existing.data:
            key = (
                str(row.get("mobile_number","")).strip(),
                str(row.get("applicant_name","")).strip().lower(),
                str(row.get("district","")).strip().lower(),
                str(row.get("scheme_name","")).strip().lower(),
            )
            existing_keys.add(key)
        
        def is_duplicate(row):
            key = (
                str(row.get("mobile_number","")).strip(),
                str(row.get("applicant_name","")).strip().lower(),
                str(row.get("district","")).strip().lower(),
                str(row.get("scheme_name","")).strip().lower(),
            )
            return key in existing_keys
        
        records_all = df.to_dict(orient="records")
        records = [r for r in records_all if not is_duplicate(r)]
        dupes_skipped = len(records_all) - len(records)
    else:
        records = df.to_dict(orient="records")
        dupes_skipped = 0

    chunk_size = 500
    inserted = 0
    for i in range(0, len(records), chunk_size):
        chunk = records[i:i+chunk_size]
        supabase.table("beneficiary_data").insert(chunk).execute()
        inserted += len(chunk)

    supabase.table("upload_logs").insert({
        "filename": file.filename,
        "upload_type": "beneficiary",
        "records_processed": inserted,
        "uploaded_at": datetime.utcnow().isoformat()
    }).execute()

    return {"status": "success", "records_inserted": inserted, "duplicates_skipped": dupes_skipped}


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
    query = supabase.table("survey_data").select("state,district,gender,age,disability,marital_status")
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
    query = supabase.table("beneficiary_data").select("applicant_name,scheme_name,status,district,state,month,quarter")
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