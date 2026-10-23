import streamlit as st
import pandas as pd
import json
import google.generativeai as genai
from PIL import Image
import gspread
from google.oauth2.service_account import Credentials
import fitz  # PyMuPDF for PDF processing
import io
import hashlib

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
1. No Mathematical Inference for 'Amount': If a value is erased or illegible, output null (without quotes) for that JSON value. Do NOT use the word 'BLANK'.
2. Date Formatting: All dates MUST be returned in the format DD/MM/YYYY.
3. Invoice Fallback: If a clear 'Invoice Number' is not found, extract any identifying code on the receipt that can serve as a unique transaction ID.
4. Confidence Scoring: 'High' (perfect), 'Medium' (probable), 'Low' (obscured). null values in 'Amount' must always trigger 'Low' confidence.
5. Strict Reconciliation: If the calculated sum of 'Amount' matches the 'Receipt Total' exactly, mark 'Reconciled' as 'Yes'. Otherwise, or if any 'Amount' is null, mark 'No'.

### ESTIMATED AMOUNT LOGIC
You must generate an 'Estimated Amount' for EVERY line item using these strict priorities:
1) If a legible price is visible, 'Estimated Amount' equals the recorded 'Amount'.
2) If a legible price is unavailable but a legible 'Receipt Total' IS available, estimate a reasonable line amount for the items such that the sum of all 'Estimated Amounts' equals the 'Receipt Total'.
3) If a legible price is unavailable and a 'Receipt Total' is also unavailable, estimate a reasonable market amount for the line item in New Zealand Dollars.
*Note: Generating an 'Estimated Amount' does not override Guardrail #1. The 'Amount' field must remain null if illegible.*

Output ONLY a raw JSON array of objects with the exact following keys:
"Store Name", "Date", "Item Name", "Quantity", "Amount", "Invoice Number", "Receipt Total", "Reconciled", "Confidence", "Category", "Subcategory", "Estimated Amount"
"""

def build_bank_csv_prompt(mapping_dictionary):
    return f"""
You are an expert financial categorization assistant.
I will provide you with a JSON array of raw bank transactions.
Your task is to assign a highly granular 'Subcategory' and map it to an exact 'Category' based on this reference mapping:
{json.dumps(mapping_dictionary, indent=2)}

Rules:
1. Output ONLY a raw JSON array of objects with the exact following keys: "Date", "Unique ID", "Account", "Bank", "Payee / Description", "Amount", "Category", "Subcategory"
2. Retain the exact original values for Date, Unique ID, Account, Bank, Payee / Description, and Amount from the provided JSON.
3. If the transaction is income (positive amount like salary or deposit), assign 'Income' unless mapping dictates otherwise.
4. If it doesn't fit perfectly into the mapping, make your best logical guess based on the description.
"""

def build_bank_pdf_prompt(mapping_dictionary):
    return f"""
You are an expert financial categorization assistant.
I will provide you with images of bank statements.
Your task is to extract all line-item transactions from these statements and categorize them.

### CATEGORIZATION INSTRUCTIONS
Assign a highly granular 'Subcategory' and map it to an exact 'Category' based on this reference mapping:
{json.dumps(mapping_dictionary, indent=2)}

Rules:
1. Output ONLY a raw JSON array of objects with the exact following keys: "Date", "Account", "Bank", "Payee / Description", "Amount", "Category", "Subcategory"
2. 'Date': Use the format DD/MM/YYYY.
3. 'Account': Extract the full account number shown on the statement.
4. 'Bank': Deduce the bank name (e.g., ASB, ANZ).
5. 'Amount': Extract as a number (use negative numbers for withdrawals/debits/purchases, positive for deposits/credits/payments).
6. Combine the Payee, Particulars, Code, and Reference into the single 'Payee / Description' field.
7. If the transaction is income (positive amount like salary or deposit), assign 'Income' unless mapping dictates otherwise.
8. If it doesn't fit perfectly into the mapping, make your best logical guess based on the description.
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

def read_messy_bank_csv(uploaded_file):
    lines = uploaded_file.getvalue().decode('utf-8').splitlines()
    if not lines:
        return pd.DataFrame(), "Unknown"
        
    start_row = 0
    account_id = "Unknown"
    
    for line in lines[:15]:
        if "Account " in line:
            parts = line.split("Account ")
            if len(parts) > 1:
                account_id = parts[1].split()[0]
                break
        elif "Card Number " in line:
            parts = line.split("Card Number ")
            if len(parts) > 1:
                account_id = parts[1].split()[0]
                break

    for i, line in enumerate(lines):
        line_lower = line.lower()
        if 'date' in line_lower and ('amount' in line_lower or 'payee' in line_lower or 'details' in line_lower or 'description' in line_lower):
            start_row = i
            break
            
    valid_csv_lines = lines[start_row:]
    cleaned_csv = "\n".join(valid_csv_lines)
    return pd.read_csv(io.StringIO(cleaned_csv)), account_id

def normalize_bank_df(df, account_id="Unknown"):
    cols = [str(c).strip() for c in df.columns]
    df.columns = cols
    
    if 'Payee' in df.columns and 'Memo' in df.columns:
        bank = 'ASB'
        payee_col = 'Payee'
        amount_col = 'Amount'
        date_col = 'Date'
        desc_parts = ['Payee', 'Memo']
        unique_id_col = 'Unique Id' if 'Unique Id' in df.columns else None
    elif 'Description' in df.columns and ('Date Processed' in df.columns or 'Date of Transaction' in df.columns):
        bank = 'ASB Credit'
        payee_col = 'Description'
        amount_col = 'Amount'
        date_col = 'Date of Transaction' if 'Date of Transaction' in df.columns else 'Date Processed'
        desc_parts = ['Description', 'Reference']
        unique_id_col = 'Unique Id' if 'Unique Id' in df.columns else None
    elif 'Details' in df.columns and 'Particulars' in df.columns:
        bank = 'ANZ'
        payee_col = 'Details'
        amount_col = 'Amount'
        date_col = 'Date'
        desc_parts = ['Details', 'Particulars', 'Code', 'Reference']
        unique_id_col = None
    else:
        bank = 'Unknown'
        payee_col = df.columns[1] if len(df.columns) > 1 else df.columns[0]
        amt_cols = [c for c in df.columns if 'amount' in str(c).lower() or 'amt' in str(c).lower()]
        amount_col = amt_cols[0] if amt_cols else df.columns[-2]
        date_cols = [c for c in df.columns if 'date' in str(c).lower()]
        date_col = date_cols[0] if date_cols else df.columns[0]
        desc_parts = [payee_col]
        unique_id_col = None

    def make_desc(row):
        parts = []
        for c in desc_parts:
            if c in row and pd.notna(row[c]):
                val = str(row[c]).strip()
                if val not in ('', 'nan'):
                    parts.append(val)
        return " - ".join(parts) if parts else str(row[payee_col])

    norm_df = pd.DataFrame()
    norm_df['Date'] = pd.to_datetime(df[date_col], format='mixed', dayfirst=True, errors='coerce').dt.strftime('%d/%m/%Y')
    norm_df['Account'] = [account_id] * len(df)
    norm_df['Bank'] = bank
    norm_df['Payee / Description'] = df.apply(make_desc, axis=1)
    norm_df['Amount'] = pd.to_numeric(df[amount_col].astype(str).str.replace(r'[$,]', '', regex=True), errors='coerce')
    
    # Generate Unique IDs
    ids = []
    seen_hashes = {}
    
    for i in range(len(norm_df)):
        row = norm_df.iloc[i]
        orig_row = df.iloc[i]
        
        if unique_id_col and unique_id_col in df.columns and pd.notna(orig_row[unique_id_col]) and str(orig_row[unique_id_col]).strip() != "":
            ids.append(str(orig_row[unique_id_col]).strip())
        else:
            bal_val = str(orig_row['Balance']).strip() if 'Balance' in df.columns else ''
            raw_str = f"{row['Date']}_{row['Payee / Description']}_{row['Amount']}_{bal_val}"
            base_hash = hashlib.md5(raw_str.encode('utf-8')).hexdigest()[:10]
            
            if base_hash in seen_hashes:
                seen_hashes[base_hash] += 1
                ids.append(f"{base_hash}-{seen_hashes[base_hash]}")
            else:
                seen_hashes[base_hash] = 1
                ids.append(base_hash)

    norm_df.insert(1, 'Unique ID', ids)
    norm_df['Category'] = ""
    norm_df['Subcategory'] = ""
    return norm_df

def apply_missing_ids(df):
    if 'Unique ID' not in df.columns:
        df.insert(1, 'Unique ID', "")
        
    ids = []
    seen_hashes = {}
    for i, row in df.iterrows():
        existing_id = str(row.get('Unique ID', '')).strip()
        if existing_id and existing_id != "nan":
            ids.append(existing_id)
            seen_hashes[existing_id] = 1
        else:
            raw_str = f"{row.get('Date','')}_{row.get('Payee / Description','')}_{row.get('Amount','')}"
            base_hash = hashlib.md5(raw_str.encode('utf-8')).hexdigest()[:10]
            if base_hash in seen_hashes:
                seen_hashes[base_hash] += 1
                ids.append(f"{base_hash}-{seen_hashes[base_hash]}")
            else:
                seen_hashes[base_hash] = 1
                ids.append(base_hash)
    df['Unique ID'] = ids
    return df

# ==========================================
# 4. STREAMLIT APP LAYOUT & LOGIC
# ==========================================
st.set_page_config(page_title="Finance Digitizer", layout="wide")

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
        
        # Check if reconciled, offer to use estimates if not
        if not st.session_state['receipt_df'].empty and 'Reconciled' in st.session_state['receipt_df'].columns:
            if any(st.session_state['receipt_df']['Reconciled'] == 'No'):
                st.warning("⚠️ The receipt is not reconciled.")
                if st.button("Use Estimated Amounts"):
                    if 'Estimated Amount' in st.session_state['receipt_df'].columns:
                        st.session_state['receipt_df']['Amount'] = st.session_state['receipt_df']['Estimated Amount']
                        st.session_state['receipt_df']['Reconciled'] = 'Yes'
                        st.success("Amounts overwritten with estimated amounts!")
                        st.rerun()

        # Reorder columns to ensure 'Estimated Amount' is the very last column
        df_cols = list(st.session_state['receipt_df'].columns)
        if 'Estimated Amount' in df_cols:
            df_cols.remove('Estimated Amount')
            df_cols.append('Estimated Amount')
            st.session_state['receipt_df'] = st.session_state['receipt_df'][df_cols]

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
    st.title("🏦 Bank CSV & PDF Categorizer")
    st.markdown("Upload raw CSV exports or PDF statements directly from your bank. The AI will normalize the format, deduplicate, and apply your custom categories.")

    st.subheader("1. Upload Bank File")
    bank_file = st.file_uploader("Upload Bank Export", key="bank_up")

    if st.button("✨ Categorize Transactions", type="primary"):
        if not bank_file:
            st.warning("Please upload a file.")
        else:
            try:
                api_key = st.secrets["GEMINI_API_KEY"]
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel('gemini-3.8-flash') 
                
                with st.spinner("Analyzing bank file and applying category rules..."):
                    mapping_dict = fetch_dynamic_categories()
                    if not mapping_dict:
                        mapping_dict = DEFAULT_CATEGORY_MAPPING

                    is_csv = hasattr(bank_file, 'name') and bank_file.name.lower().endswith('.csv')
                    
                    if is_csv:
                        # Process CSV
                        raw_df, extracted_account = read_messy_bank_csv(bank_file)
                        normalized_df = normalize_bank_df(raw_df, extracted_account)
                        bank_json_payload = normalized_df.to_json(orient="records")
                        
                        final_prompt = build_bank_csv_prompt(mapping_dict)
                        payload = [final_prompt, "Here are the transactions to categorize:", bank_json_payload]
                    else:
                        # Process PDF / Images
                        final_prompt = build_bank_pdf_prompt(mapping_dict)
                        images = []
                        if hasattr(bank_file, 'name') and bank_file.name.lower().endswith('.pdf'):
                            doc = fitz.open(stream=bank_file.read(), filetype="pdf")
                            for page in doc:
                                pix = page.get_pixmap(dpi=150)
                                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                                images.append(resize_image(img))
                        else:
                            images.append(resize_image(Image.open(bank_file)))
                        
                        payload = [final_prompt] + images

                    response = model.generate_content(payload)
                    raw_text = response.text.replace("```json", "").replace("```", "").strip()
                    categorized_data = json.loads(raw_text)
                    
                    # Apply missing IDs (specifically needed for PDF extractions)
                    result_df = apply_missing_ids(pd.DataFrame(categorized_data))
                    
                    # Enforce strict column order and fill missing
                    col_order = ["Date", "Unique ID", "Account", "Bank", "Payee / Description", "Amount", "Category", "Subcategory"]
                    for col in col_order:
                        if col not in result_df.columns:
                            result_df[col] = ""
                    result_df = result_df[col_order]
                    
                    st.session_state['bank_df'] = result_df
                    st.success("Categorization complete!")
                    
            except KeyError as e:
                st.error(f"Missing Secret: Please ensure {e} is added to your Streamlit App Secrets.")
            except Exception as e:
                st.error(f"An error occurred: {e}. Please check the file and try again.")

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
            st.download_button("⬇️ Download Bank Data", data=csv, file_name="categorized_bank_transactions.csv", mime="text/csv", key="dl_bank")
        with col2:
            if st.button("☁️ Sync Transactions to Accounts"):
                try:
                    with st.spinner("Connecting to Google Sheets and routing accounts..."):
                        creds_dict = json.loads(st.secrets["GCP_JSON_STRING"])
                        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
                        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
                        client = gspread.authorize(creds)
                        
                        sheet_id = st.secrets["SPREADSHEET_ID"]
                        spreadsheet = client.open_by_key(sheet_id)
                        
                        # Fetch optional routing map
                        routing_map = {}
                        try:
                            routing_sheet = spreadsheet.worksheet("routing")
                            for row in routing_sheet.get_all_records():
                                routing_map[str(row.get("Account Number", "")).strip()] = str(row.get("Tab Name", "")).strip()
                        except:
                            pass
                            
                        df_upload = edited_bank_df.fillna("")
                        accounts = df_upload['Account'].unique()
                        
                        synced_count = 0
                        skipped_count = 0
                        
                        for acc in accounts:
                            acc_str = str(acc).strip()
                            tab_name = routing_map.get(acc_str, acc_str if acc_str and acc_str != "Unknown" else "bank_transactions")
                            
                            try:
                                worksheet = spreadsheet.worksheet(tab_name) 
                            except gspread.exceptions.WorksheetNotFound:
                                worksheet = spreadsheet.add_worksheet(title=tab_name, rows="1000", cols="20")
                                worksheet.append_row(list(df_upload.columns))
                            
                            existing_ids = worksheet.col_values(2) # Column B is Unique ID
                            
                            acc_df = df_upload[df_upload['Account'] == acc]
                            # Drop exact ID matches to prevent duplication
                            new_rows = acc_df[~acc_df['Unique ID'].astype(str).isin(existing_ids)]
                            
                            skipped_count += (len(acc_df) - len(new_rows))
                            
                            if not new_rows.empty:
                                worksheet.append_rows(new_rows.values.tolist())
                                synced_count += len(new_rows)
                                
                    st.success(f"✅ Success! {synced_count} new transactions synced. {skipped_count} existing duplicates skipped.")
                except Exception as e:
                    st.error(f"Failed to sync: {e}")