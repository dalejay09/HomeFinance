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
        
        # Safely find amount column for mobile copy
        amt_cols = [c for c in df.columns if 'amount' in str(c).lower() or 'amt' in str(c).lower()]
        amount_col = amt_cols[0] if amt_cols else df.columns[-2]
        
        # Safely find date column for mobile copy
        date_cols = [c for c in df.columns if 'date' in str(c).lower()]
        date_col = date_cols[0] if date_cols else df.columns[0]
        
        desc_parts = [payee_col]

    def make_desc(row):
        parts = []
        for c in desc_parts:
            if c in row and pd.notna(row[c]):
                val = str(row[c]).strip()
                if val not in ('', 'nan'):
                    parts.append(val)
        return " - ".join(parts) if parts else str(row[payee_col])

    norm_df = pd.DataFrame()
    
    # Formatting date safely
    norm_df['Date'] = pd.to_datetime(
        df[date_col], 
        format='mixed', 
        dayfirst=True, 
        errors='coerce'
    ).dt.strftime('%d/%m/%Y')
    
    norm_df['Bank'] = bank
    norm_df['Payee / Description'] = df.apply(make_desc, axis=1)
    
    # Formatting amount safely
    norm_df['Amount'] = pd.to_numeric(
        df[amount_col].astype(str).str.replace(r'[$,]', '', regex=True), 
        errors='coerce'
    )
    
    norm_df['Category'] = ""
    norm_df['Subcategory'] = ""
    return norm_df

# ==========================================
# 4. STREAMLIT APP LAYOUT & LOGIC
# ==========================================
st.set_page_config(page_title="Finance Digitizer", layout="wide")

# TOP LEVEL TABS
main_tab1, main_tab2 = st.tabs(["🧾 Receipt Digitizer", "🏦 Bank Statements"])

# ---------------------------------------------------------
# TAB 1: RECEIPT DIGITIZER
# ---------------------------------------------------------
with main_tab1:
    st.title("🧾 AI Receipt Digitizer")
    
    st.markdown("Upload close-up photos, PDFs, or snap live pictures using the camera. The AI will stitch them logically if they belong to the same transaction.")

    # --- IMAGE INPUT TABS ---
    st.subheader("1. Add Receipts")
    r_tab1, r_tab2 = st.tabs(["📁 Upload Files", "📸 Take Photo"])

    with r_tab1:
        uploaded_files = st.file_uploader("Upload Receipts (JPG/PNG/PDF)", accept_multiple_files=True, type=['jpg', 'jpeg', 'png', 'pdf'], key="receipt_up")

    with r_tab2:
        st.info("📸 Snap as many parts of the receipt as you need. They will compile below.")
        if 'camera_snaps' not in st.session_state:
            st.session_state.camera_snaps = []
            
        new_snap = st.camera_input("Snap a live photo", key="cam_in")
        
        if new_snap is not None:
            if new_snap not in st.session_state.camera_snaps:
                st.session_state.camera_snaps.append(new_snap)
                
        if st.session_state.camera_snaps:
            st.success(f"{len(st.session_state.camera_snaps)} camera photo(s) queued!")
            cols = st.columns(min(len(st.session_state.camera_snaps), 5))
            for i, snap in enumerate(st.session_state.camera_snaps):
                with cols[i % 5]:
                    st.image(snap, use_container_width=True)
                    
            if st.button("🗑️ Clear Camera Batch"):
                st.session_state.camera_snaps = []
                st.rerun()

    all_image_files = []
    if uploaded_files:
        all_image_files.extend(uploaded_files)
    if 'camera_snaps' in st.session_state and st.session_state.camera_snaps:
        all_image_files.extend(st.session_state.camera_snaps)

    # --- PROCESS BUTTON ---
    st.write("---")
    if st.button("✨ Process Receipts", type="primary"):
        if not all_image_files:
            st.warning("Please upload or snap at least one image/PDF.")
        else:
            try:
                api_key = st.secrets["GEMINI_API_KEY"]
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel('gemini-3.8-flash') 
                
                with st.spinner("Fetching category rules and analyzing files..."):
                    mapping_dict = fetch_dynamic_categories()
                    if not mapping_dict:
                        mapping_dict = DEFAULT_CATEGORY_MAPPING
                    
                    final_prompt = build_receipt_system_prompt(mapping_dict)
                    
                    images = []
                    for file in all_image_files:
                        if hasattr(file, 'name') and file.name.lower().endswith('.pdf'):
                            doc = fitz.open(stream=file.read(), filetype="pdf")
                            for page in doc:
                                pix = page.get_pixmap(dpi=150)
                                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                                images.append(resize_image(img))
                        else:
                            images.append(resize_image(Image.open(file)))
                    
                    payload = [final_prompt] + images
                    response = model.generate_content(payload)
                    
                    raw_text = response.text.replace("```json", "").replace("```", "").strip()
                    data = json.loads(raw_text)
                    
                    st.session_state['receipt_df'] = pd.DataFrame(data)
                    st.success("Extraction complete!")
                    
            except KeyError as e:
                st.error(f"Missing Secret: Please ensure {e} is added to your Streamlit App Secrets.")
            except Exception as e:
                st.error(f"An error occurred: {e}")

    # --- DISPLAY AND EDIT DATA ---
    if 'receipt_df' in st.session_state:
        st.subheader("2. Review & Edit Data")
        
        def highlight_flags(row):
            if row.get('Reconciled') == 'No' or row.get('Confidence') == 'Low':
                return ['background-color: #ffcccc'] * len(row)
            elif row.get('Confidence') == 'Medium':
                return ['background-color: #fff3cd'] * len(row)
            return [''] * len(row)

        styled_receipt_df = st.session_state['receipt_df'].style.apply(highlight_flags, axis=1)
        
        edited_receipt_df = st.data_editor(
            styled_receipt_df, 
            use_container_width=True, 
            hide_index=True,
            num_rows="dynamic",
            key="receipt_editor"
        )
        
        st.write("---")
        col1, col2 = st.columns(2)
        with col1:
            csv = edited_receipt_df.to_csv(index=False).encode('utf-8')
            st.download_button("⬇️ Download CSV", data=csv, file_name="reconciled_receipts.csv", mime="text/csv", key="dl_rect")
        with col2:
            if st.button("☁️ Sync Receipts to Google Sheets"):
                try:
                    with st.spinner("Connecting to Google Sheets..."):
                        creds_dict = json.loads(st.secrets["GCP_JSON_STRING"])
                        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
                        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
                        client = gspread.authorize(creds)
                        
                        sheet_id = st.secrets["SPREADSHEET_ID"]
                        spreadsheet = client.open_by_key(sheet_id)
                        worksheet = spreadsheet.worksheet("receipts") 
                        
                        df_upload = edited_receipt_df.fillna("")
                        worksheet.append_rows(df_upload.values.tolist())
                    st.success("✅ Receipts synced securely!")
                except Exception as e:
                    st.error(f"Failed to sync: {e}")

# ---------------------------------------------------------
# TAB 2: BANK STATEMENTS
# ---------------------------------------------------------
with main_tab2:
    st.title("🏦 Bank CSV Categorizer (ASB/ANZ)")
    st.markdown("Upload raw CSV exports directly from your ASB or ANZ customer portal. The AI will normalize the format and apply your custom categories automatically.")

    st.subheader("1. Upload Bank CSV")
    bank_csv_file = st.file_uploader("Upload Bank Export (.csv)", type=['csv'], key="bank_up")

    if st.button("✨ Categorize Transactions", type="primary"):
        if not bank_csv_file:
            st.warning("Please upload a CSV file.")
        else:
            try:
                # 1. Normalize CSV
                raw_df = pd.read_csv(bank_csv_file)
                normalized_df = normalize_bank_df(raw_df)
                
                # 2. Convert to JSON payload for AI
                bank_json_payload = normalized_df.to_json(orient="records")
                
                # 3. Call Gemini
                api_key = st.secrets["GEMINI_API_KEY"]
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel('gemini-3.8-flash') 
                
                with st.spinner("Normalizing CSV and applying category rules..."):
                    mapping_dict = fetch_dynamic_categories()
                    if not mapping_dict:
                        mapping_dict = DEFAULT_CATEGORY_MAPPING
                    
                    final_prompt = build_bank_system_prompt(mapping_dict)
                    payload = [final_prompt, "Here are the transactions to categorize:", bank_json_payload]
                    
                    response = model.generate_content(payload)
                    raw_text = response.text.replace("```json", "").replace("```", "").strip()
                    categorized_data = json.loads(raw_text)
                    
                    st.session_state['bank_df'] = pd.DataFrame(categorized_data)
                    st.success("Categorization complete!")
                    
            except KeyError as e:
                st.error(f"Missing Secret: Please ensure {e} is added to your Streamlit App Secrets.")
            except Exception as e:
                st.error(f"An error occurred: {e}")

    # --- DISPLAY AND EDIT DATA ---
    if 'bank_df' in st.session_state:
        st.subheader("2. Review & Edit Transactions")
        
        edited_bank_df = st.data_editor(
            st.session_state['bank_df'], 
            use_container_width=True, 
            hide_index=True,
            num_rows="dynamic",
            key="bank_editor"
        )
        
        st.write("---")
        col1, col2 = st.columns(2)
        with col1:
            csv = edited_bank_df.to_csv(index=False).encode('utf-8')
            st.download_button("⬇️ Download Bank CSV", data=csv, file_name="categorized_bank_transactions.csv", mime="text/csv", key="dl_bank")
        with col2:
            if st.button("☁️ Sync Transactions to Google Sheets"):
                try:
                    with st.spinner("Connecting to Google Sheets..."):
                        creds_dict = json.loads(st.secrets["GCP_JSON_STRING"])
                        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
                        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
                        client = gspread.authorize(creds)
                        
                        sheet_id = st.secrets["SPREADSHEET_ID"]
                        spreadsheet = client.open_by_key(sheet_id)
                        
                        # Assuming the user creates a tab called "bank_transactions"
                        try:
                            worksheet = spreadsheet.worksheet("bank_transactions") 
                        except gspread.exceptions.WorksheetNotFound:
                            # Auto-create if missing
                            worksheet = spreadsheet.add_worksheet(title="bank_transactions", rows="1000", cols="20")
                            worksheet.append_row(["Date", "Bank", "Payee / Description", "Amount", "Category", "Subcategory"])
                            
                        df_upload = edited_bank_df.fillna("")
                        worksheet.append_rows(df_upload.values.tolist())
                    st.success("✅ Transactions synced securely!")
                except Exception as e:
                    st.error(f"Failed to sync: {e}")