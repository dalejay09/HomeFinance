import streamlit as st
import pandas as pd
import json
import google.generativeai as genai
from PIL import Image
import gspread
from google.oauth2.service_account import Credentials
import fitz  # PyMuPDF for PDF processing

# ==========================================
# 1. DEFAULT FALLBACK CATEGORY DICTIONARY
# ==========================================
DEFAULT_CATEGORY_MAPPING = {
    "FruitVeg": ["cucumber", "apples", "grapes", "mandarins", "onions", "tomatoes (fresh)"],
    "Dinners": ["tinned beans", "tinned tomatoes", "salsa", "noodles", "rice", "pasta", "frozen wedges", "stirfry sauce"],
    "Lunches": ["crackers", "muesli bars", "cookies", "biscuits", "honey"],
    "Snacks": ["chips", "popcorn", "fruit strips", "nuts"],
    "Breakfasts": ["cereal", "oats", "cornflakes"],
    "Dairy": ["milk", "sour cream", "butter", "margarine", "cheese block"],
    "Bakery": ["toast bread", "rolls", "baguettes"],
    "Meat": ["pork mince", "beef mince", "sausages", "chicken drumsticks", "ham (deli)"],
    "Drinks": ["soda", "energy drinks", "juice"],
    "Treats": ["chocolate", "ice cream", "lollies"],
    "Personal Care": ["sanitary products", "nappies", "toilet paper", "wipes"],
    "Household": ["resealable bags", "batteries", "cleaning spray", "checkout bag"]
}

# ==========================================
# 2. DYNAMIC PROMPT BUILDERS
# ==========================================
def build_receipt_system_prompt(mapping_dictionary):
    return f"""
You are a strict, highly accurate receipt digitization assistant. 
Your task is to extract line items from the provided receipt images and output them as a structured JSON list.

### CATEGORIZATION INSTRUCTIONS
Analyze the 'Item Name' and assign a highly granular 'Subcategory'. Then, map it to the most appropriate broad 'Category' based on this reference mapping:
{json.dumps(mapping_dictionary, indent=2)}

**Discount Rule:** If a line item is a discount or promo reduction (e.g., a negative amount or labeled 'Discount'), you MUST assign it the exact same 'Category' and 'Subcategory' as the item it applies to.

### STRICT PROCESSING GUARDRAILS
1. No Mathematical Inference: Never auto-fill, infer, or guess a missing price.
2. Blanking: If a value is erased or illegible, output null (without quotes) for that JSON value. Do NOT use the word 'BLANK'.
3. Date Formatting: All dates MUST be returned in the format DD/MM/YYYY.
4. Invoice Fallback: If a clear 'Invoice Number' is not found, extract any identifying code on the receipt that can serve as a unique transaction ID.
5. Confidence Scoring: 'High' (perfect), 'Medium' (probable), 'Low' (obscured). null values must always trigger 'Low'.
6. Strict Reconciliation: If the calculated sum of items matches the Receipt Total exactly, mark Reconciled as 'Yes'. Otherwise, or if any value is null, mark 'No'.

Output ONLY a raw JSON array of objects with keys:
"Store Name", "Date", "Item Name", "Quantity", "Amount", "Invoice Number", "Receipt Total", "Reconciled", "Confidence", "Category", "Subcategory"
"""

def build_bank_system_prompt(mapping_dictionary):
    return f"""
You are an expert financial categorization assistant.
I will provide you with a JSON array of raw bank transactions.
Your task is to assign a highly granular 'Subcategory' and map it to an exact 'Category' based on this reference mapping:
{json.dumps(mapping_dictionary, indent=2)}

Rules:
1. Output ONLY a raw JSON array of objects with the exact following keys: "Date", "Bank", "Payee / Description", "Amount", "Category", "Subcategory"
2. Retain the exact original values for Date, Bank, Payee / Description, and Amount from the provided JSON.
3. If the transaction is income (positive amount like salary or deposit), assign 'Income' unless mapping dictates otherwise.
4. If it doesn't fit perfectly into the mapping, make your best logical guess based on the description.
"""

# ==========================================
# 3. HELPER FUNCTIONS
# ==========================================
def resize_image(image, max_size=(1024, 1024)):
    image.thumbnail(max_size, Image.Resampling.LANCZOS)
    return image

def fetch_dynamic_categories():
    try:
        creds_dict = json.loads(st.secrets["GCP_JSON_STRING"])
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        
        sheet_id = st.secrets["SPREADSHEET_ID"]
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("coding")
        
        records = worksheet.get_all_records()
        
        dynamic_mapping = {}
        for row in records:
            cat = str(row.get("Category", "")).strip()
            examples = str(row.get("Subcategory Examples", "")).strip()
            if cat: 
                dynamic_mapping[cat] = examples
                
        if dynamic_mapping:
            return dynamic_mapping
    except Exception as e:
        print(f"Could not load dynamic categories, falling back to default. {e}")
        pass
    return None

def normalize_bank_df(df):
    cols = [c.strip() for c in df.columns]
    df.columns = cols
    
    if 'Payee' in df.columns:
        bank = 'ASB'
        payee_col = 'Payee'
        amount_col = 'Amount'
        date_col = 'Date'
        desc_parts = ['Payee', 'Particulars', 'Code', 'Reference']
    elif 'Details' in df.columns:
        bank = 'ANZ'
        payee_col = 'Details'
        amount_col = 'Amount'
        date_col = 'Date'
        desc_parts = ['Details', 'Particulars', 'Code', 'Reference']
    else:
        bank = 'Unknown'
        payee_col = df.columns[1] if len(df.columns) > 1 else df.columns[0]
        amount_col =