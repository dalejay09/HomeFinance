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
from datetime import datetime, timedelta

# ==========================================
# 1. DEFAULT FALLBACK CATEGORY DICTIONARY
# ==========================================
DEFAULT_CATEGORY_MAPPING = {
    "FoodAndDrinks": {
        "Dinners": "mince beef pork sausages canned beans canned tomatoes udon noodles pasta cheese",
        "Lunches": "crackers muesli bars cookies biscuits honey",
        "Snacks": "chips popcorn fruit strips nuts",
        "Breakfasts": "cereal oats cornflakes",
    },
    "Household": {
        "Cleaning": "cleaning spray dish liquid washing powder",
        "HouseholdOther": "batteries checkout bag resealable bags"
    }
}
DEFAULT_CATCH_CODES = ["Category: FoodAndDrinks -> Code: FoodAndDrinksOther", "Category: Unknown -> Code: Unknown"]
DEFAULT_ALL_CODES_MAP = {
    "Dinners": "FoodAndDrinks",
    "Lunches": "FoodAndDrinks",
    "Snacks": "FoodAndDrinks",
    "Breakfasts": "FoodAndDrinks",
    "FoodAndDrinksOther": "FoodAndDrinks",
    "Cleaning": "Household",
    "HouseholdOther": "Household",
    "Unknown": "Unknown"
}

# ==========================================
# 2. DYNAMIC PROMPT BUILDERS
# ==========================================
def build_receipt_system_prompt(mapping_dictionary, catch_codes):
    return f"""
You are a strict, highly accurate receipt digitization assistant. 
Your task is to extract line items from the provided receipt images or text files and output them as a structured JSON list.

### CATEGORIZATION & CODING INSTRUCTIONS
1. Analyze the 'Item Name'. 
2. Determine a 'Generic Item' name (e.g., 'cucumbers', 'soda', 'shampoo') that generalizes the specific product.
3. Assign the best 'Code' and its parent 'Category' based ONLY on this reference mapping:
{json.dumps(mapping_dictionary, indent=2)}

**Catch Code Rule:** If you are less than 90% confident about an item's mapping, or the item does not clearly fit into the provided dictionary, you MUST default to one of these designated Catch Codes:
{json.dumps(catch_codes, indent=2)}

**Discount Rule:** If a line item is a discount or promo reduction (e.g., a negative amount or labeled 'Discount'), assign it the exact same 'Category', 'Code', and 'Generic Item' as the item it applies to.

### STRICT PROCESSING GUARDRAILS
1. No Mathematical Inference for 'Amount': If a value is erased or illegible, output null (without quotes).
2. Date Formatting: All dates MUST be returned in the format DD/MM/YYYY.
3. Invoice Fallback: Extract any identifying code on the receipt that can serve as a unique transaction ID.
4. Confidence Scoring: 'High' (perfect), 'Medium' (probable), 'Low' (obscured). null values in 'Amount' must always trigger 'Low'.
5. Strict Reconciliation: If the calculated sum of 'Amount' matches 'Receipt Total' exactly, mark 'Reconciled' as 'Yes'. Otherwise, mark 'No'.

### ESTIMATED AMOUNT LOGIC
You must generate an 'Estimated Amount' for EVERY line item using these strict priorities:
1) Legible price visible = 'Amount'
2) Legible price unavailable but 'Receipt Total' available = estimate reasonable line amounts so the sum equals 'Receipt Total'.
3) No price, no total = estimate reasonable market amount in NZD.
*Note: Generating an 'Estimated Amount' does not override Guardrail #1. The 'Amount' field must remain null if illegible.*

Output ONLY a raw JSON array of objects with the exact following keys:
"Store Name", "Date", "Item Name", "Generic Item", "Quantity", "Amount", "Invoice Number", "Receipt Total", "Reconciled", "Confidence", "Category", "Code", "Estimated Amount"
"""

def build_bank_csv_prompt(mapping_dictionary, catch_codes):
    return f"""
You are an expert financial categorization assistant.
I will provide you with a JSON array of raw bank transactions.
Your task is to assign an exact 'Code' and map it to its 'Category' based ONLY on this reference mapping:
{json.dumps(mapping_dictionary, indent=2)}

**Catch Code Rule:** If you are less than 90% confident about a transaction's mapping, you MUST default to one of these Catch Codes:
{json.dumps(catch_codes, indent=2)}

Rules:
1. Output ONLY a raw JSON array of objects with the exact keys: "Date", "Unique ID", "Account", "Bank", "Payee / Description", "Amount", "Category", "Code"
2. Retain the exact original values for Date, Unique ID, Account, Bank, Payee / Description, and Amount.
3. If the transaction is income (positive amount), assign 'Income' unless mapping dictates otherwise.
"""

def build_bank_pdf_prompt(mapping_dictionary, catch_codes):
    return f"""
You are an expert financial categorization assistant.
I will provide you with images of bank statements.
Your task is to extract all line-item transactions and categorize them.

### CATEGORIZATION INSTRUCTIONS
Assign an exact 'Code' and map it to its 'Category' based ONLY on this reference mapping:
{json.dumps(mapping_dictionary, indent=2)}

**Catch Code Rule:** If you are less than 90% confident about a transaction's mapping, you MUST default to one of these Catch Codes:
{json.dumps(catch_codes, indent=2)}

Rules:
1. Output ONLY a raw JSON array of objects with the exact keys: "Date", "Account", "Bank", "Payee / Description", "Amount", "Category", "Code"
2. 'Date': Use the format DD/MM/YYYY.
3. 'Account': Extract the full account number.
4. 'Bank': Deduce the bank name.
5. 'Amount': Extract as a number (negative for debits, positive for credits).
6. Combine the Payee, Particulars, Code, and Reference into the single 'Payee / Description' field.
"""

# ==========================================
# 3. HELPER FUNCTIONS
# ==========================================
def resize_image(image, max_size=(1024, 1024)):
    image.thumbnail(max_size, Image.Resampling.LANCZOS)
    return image

@st.cache_resource
def get_gs_client():
    creds_dict = json.loads(st.secrets["GCP_JSON_STRING"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

@st.cache_data(ttl=300)
def fetch_dynamic_categories():
    try:
        client = get_gs_client()
        sheet_id = st.secrets["SPREADSHEET_ID"]
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("coding")
        
        records = worksheet.get_all_records()
        
        ai_mapping = {}
        catch_codes = []
        all_codes_map = {}
        
        for row in records:
            cat = str(row.get("Category", "")).strip()
            code = str(row.get("Code", "")).strip()
            examples = str(row.get("Description Examples", "")).strip()
            autocode = str(row.get("AutoCode", "Yes")).strip().lower()
            catchcode = str(row.get("CatchCode", "No")).strip().lower()
            
            if cat and code:
                all_codes_map[code] = cat
                if autocode == 'yes':
                    if cat not in ai_mapping:
                        ai_mapping[cat] = {}
                    ai_mapping[cat][code] = examples
                if catchcode == 'yes':
                    catch_codes.append(f"Category: {cat} -> Code: {code}")
                    
        if all_codes_map:
            return ai_mapping, catch_codes, all_codes_map
    except Exception as e:
        print(f"Could not load dynamic categories, falling back to default. {e}")
        pass
    return None, None, None

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
    norm_df['Code'] = ""
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

def assign_receipt_ids(df):
    if df.empty: return df
    
    # We want a deterministic hash based on Store, Date, and Receipt Total
    # so that multi-page receipts stitched by the AI get the exact same ID.
    ids = []
    for _, row in df.iterrows():
        store = str(row.get('Store Name', '')).strip().lower()
        date = str(row.get('Date', '')).strip()
        try:
            total = float(row.get('Receipt Total', 0))
            total_str = f"{total:.2f}"
        except:
            total_str = str(row.get('Receipt Total', ''))
            
        raw = f"{store}_{date}_{total_str}"
        ids.append("r-" + hashlib.md5(raw.encode()).hexdigest()[:8])
        
    if 'Receipt ID' in df.columns:
        df['Receipt ID'] = ids
    else:
        df.insert(0, 'Receipt ID', ids)
    return df

# ==========================================
# 4. STREAMLIT APP LAYOUT & LOGIC
# ==========================================
st.set_page_config(page_title="Finance Digitizer", layout="wide")

_, _, ALL_CODES_MAP = fetch_dynamic_categories()
if not ALL_CODES_MAP:
    ALL_CODES_MAP = DEFAULT_ALL_CODES_MAP
VALID_CODES = list(ALL_CODES_MAP.keys())

main_tab1, main_tab2, main_tab3 = st.tabs(["🧾 Receipt Digitizer", "🏦 Bank Statements", "🔄 Reconciliation"])

# ---------------------------------------------------------
# TAB 1: RECEIPT DIGITIZER
# ---------------------------------------------------------
with main_tab1:
    st.title("🧾 AI Receipt Digitizer")
    st.markdown("Upload close-up photos, PDFs, text, or spreadsheet files, or snap live pictures using the camera.")

    st.subheader("1. Add Receipts")
    r_tab1, r_tab2 = st.tabs(["📁 Upload Files", "📸 Take Photo"])

    with r_tab1:
        uploaded_files = st.file_uploader("Upload Receipts (JPG/PNG/PDF/TXT/CSV/XLSX)", accept_multiple_files=True, type=['jpg', 'jpeg', 'png', 'pdf', 'txt', 'csv', 'xlsx'], key="receipt_up")

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

    all_files = []
    if uploaded_files:
        all_files.extend(uploaded_files)
    if 'camera_snaps' in st.session_state and st.session_state.camera_snaps:
        all_files.extend(st.session_state.camera_snaps)

    st.write("---")
    if st.button("✨ Process Receipts", type="primary"):
        if not all_files:
            st.warning("Please upload or snap at least one file.")
        else:
            try:
                api_key = st.secrets["GEMINI_API_KEY"]
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel('gemini-3.8-flash') 
                
                with st.spinner("Fetching category rules and analyzing files..."):
                    ai_mapping, catch_codes, _ = fetch_dynamic_categories()
                    if not ai_mapping:
                        ai_mapping = DEFAULT_CATEGORY_MAPPING
                        catch_codes = DEFAULT_CATCH_CODES
                    
                    final_prompt = build_receipt_system_prompt(ai_mapping, catch_codes)
                    payload = [final_prompt]
                    
                    for file in all_files:
                        if hasattr(file, 'name') and file.name:
                            file_ext = file.name.lower().split('.')[-1]
                            if file_ext == 'pdf':
                                doc = fitz.open(stream=file.read(), filetype="pdf")
                                for page in doc:
                                    pix = page.get_pixmap(dpi=150)
                                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                                    payload.append(resize_image(img))
                            elif file_ext in ['csv', 'xlsx']:
                                try:
                                    if file_ext == 'csv':
                                        df_upload = pd.read_csv(file)
                                    else:
                                        df_upload = pd.read_excel(file)
                                    payload.append(f"Receipt data from '{file.name}':\n" + df_upload.to_csv(index=False))
                                except Exception as e:
                                    st.warning(f"Could not read {file.name} as a spreadsheet. Error: {e}")
                            elif file_ext == 'txt':
                                try:
                                    payload.append(f"Receipt text from '{file.name}':\n" + file.getvalue().decode('utf-8'))
                                except Exception as e:
                                    st.warning(f"Could not read {file.name}. Error: {e}")
                            else:
                                payload.append(resize_image(Image.open(file)))
                        else:
                            payload.append(resize_image(Image.open(file)))
                    
                    response = model.generate_content(payload)
                    raw_text = response.text.replace("```json", "").replace("```", "").strip()
                    data = json.loads(raw_text)
                    
                    # Generate logical UUIDs before presenting to user
                    raw_df = pd.DataFrame(data)
                    st.session_state['receipt_df'] = assign_receipt_ids(raw_df)
                    st.success("Extraction complete!")
            except KeyError as e:
                st.error(f"Missing Secret: Please ensure {e} is added to your Streamlit App Secrets.")
            except Exception as e:
                st.error(f"An error occurred: {e}")

    if 'receipt_df' in st.session_state:
        st.subheader("2. Review & Edit Data")
        if not st.session_state['receipt_df'].empty and 'Reconciled' in st.session_state['receipt_df'].columns:
            if any(st.session_state['receipt_df']['Reconciled'] == 'No'):
                st.warning("⚠️ The receipt is not reconciled.")
                if st.button("Use Estimated Amounts"):
                    if 'Estimated Amount' in st.session_state['receipt_df'].columns:
                        st.session_state['receipt_df']['Amount'] = st.session_state['receipt_df']['Estimated Amount']
                        st.session_state['receipt_df']['Reconciled'] = 'Yes'
                        st.success("Amounts overwritten with estimated amounts!")
                        st.rerun()

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
            key="receipt_editor",
            column_config={
                "Receipt ID": st.column_config.TextColumn("Receipt ID", disabled=True),
                "Code": st.column_config.SelectboxColumn("Code", options=VALID_CODES, required=True),
                "Category": st.column_config.TextColumn("Category", disabled=True),
                "Amount": st.column_config.NumberColumn("Amount", format="%.2f"),
                "Estimated Amount": st.column_config.NumberColumn("Estimated Amount", format="%.2f"),
                "Receipt Total": st.column_config.NumberColumn("Receipt Total", format="%.2f")
            }
        )
        
        if not edited_receipt_df.equals(st.session_state['receipt_df']):
            edited_receipt_df['Category'] = edited_receipt_df['Code'].map(ALL_CODES_MAP).fillna(edited_receipt_df['Category'])
            st.session_state['receipt_df'] = edited_receipt_df
            st.rerun()
            
        st.write("---")
        col1, col2 = st.columns(2)
        with col1:
            csv = edited_receipt_df.to_csv(index=False).encode('utf-8')
            st.download_button("⬇️ Download CSV", data=csv, file_name="reconciled_receipts.csv", mime="text/csv", key="dl_rect")
        with col2:
            if st.button("☁️ Sync Receipts to Google Sheets"):
                try:
                    with st.spinner("Connecting to Google Sheets..."):
                        client = get_gs_client()
                        sheet_id = st.secrets["SPREADSHEET_ID"]
                        spreadsheet = client.open_by_key(sheet_id)
                        
                        try:
                            worksheet = spreadsheet.worksheet("receipts") 
                        except gspread.exceptions.WorksheetNotFound:
                            worksheet = spreadsheet.add_worksheet(title="receipts", rows="1000", cols="20")
                            worksheet.append_row(list(edited_receipt_df.columns))
                            
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
                    ai_mapping, catch_codes, _ = fetch_dynamic_categories()
                    if not ai_mapping:
                        ai_mapping = DEFAULT_CATEGORY_MAPPING
                        catch_codes = DEFAULT_CATCH_CODES

                    is_csv = hasattr(bank_file, 'name') and bank_file.name.lower().endswith('.csv')
                    
                    if is_csv:
                        raw_df, extracted_account = read_messy_bank_csv(bank_file)
                        normalized_df = normalize_bank_df(raw_df, extracted_account)
                        bank_json_payload = normalized_df.to_json(orient="records")
                        
                        final_prompt = build_bank_csv_prompt(ai_mapping, catch_codes)
                        payload = [final_prompt, "Here are the transactions to categorize:", bank_json_payload]
                    else:
                        final_prompt = build_bank_pdf_prompt(ai_mapping, catch_codes)
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
                    
                    result_df = apply_missing_ids(pd.DataFrame(categorized_data))
                    
                    col_order = ["Date", "Unique ID", "Account", "Bank", "Payee / Description", "Amount", "Category", "Code"]
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

    if 'bank_df' in st.session_state:
        st.subheader("2. Review & Edit Transactions")
        
        display_df = st.session_state['bank_df'].copy()
        if 'Category' in display_df.columns:
            display_df = display_df.drop(columns=['Category'])

        edited_bank_df = st.data_editor(
            display_df, 
            use_container_width=True, 
            hide_index=True,
            num_rows="dynamic",
            key="bank_editor",
            column_config={
                "Code": st.column_config.SelectboxColumn("Code", options=VALID_CODES, required=True),
                "Amount": st.column_config.NumberColumn("Amount", format="%.2f")
            }
        )
        
        updated_state_df = edited_bank_df.copy()
        updated_state_df['Category'] = updated_state_df['Code'].map(ALL_CODES_MAP).fillna("")
        
        cols = list(updated_state_df.columns)
        if 'Category' in cols and 'Code' in cols:
            cols.remove('Category')
            code_index = cols.index('Code')
            cols.insert(code_index, 'Category')
            updated_state_df = updated_state_df[cols]

        if not updated_state_df.equals(st.session_state['bank_df']):
            st.session_state['bank_df'] = updated_state_df
            st.rerun()
        
        st.write("---")
        col1, col2 = st.columns(2)
        with col1:
            csv = st.session_state['bank_df'].to_csv(index=False).encode('utf-8')
            st.download_button("⬇️ Download Bank Data", data=csv, file_name="categorized_bank_transactions.csv", mime="text/csv", key="dl_bank")
        with col2:
            if st.button("☁️ Sync Transactions to Accounts"):
                try:
                    with st.spinner("Connecting to Google Sheets and routing accounts..."):
                        client = get_gs_client()
                        sheet_id = st.secrets["SPREADSHEET_ID"]
                        spreadsheet = client.open_by_key(sheet_id)
                        
                        routing_map = {}
                        try:
                            routing_sheet = spreadsheet.worksheet("routing")
                            for row in routing_sheet.get_all_records():
                                routing_map[str(row.get("Account Number", "")).strip()] = str(row.get("Tab Name", "")).strip()
                        except:
                            pass
                            
                        df_upload = st.session_state['bank_df'].fillna("")
                        
                        upload_payload = df_upload.copy()
                        upload_payload = upload_payload.rename(columns={'Code': 'Code1', 'Amount': 'Amount1'})
                        upload_payload.insert(5, 'Amount', upload_payload['Amount1'])
                        
                        new_cols = ['Code2', 'Amount2', 'Code3', 'Amount3', 'Code4', 'Amount4', 'Code5', 'Amount5']
                        for col in new_cols:
                            upload_payload[col] = ""
                            
                        if 'Category' in upload_payload.columns:
                            upload_payload = upload_payload.drop(columns=['Category'])
                            
                        final_order = ["Date", "Unique ID", "Account", "Bank", "Payee / Description", "Amount", "Code1", "Amount1", "Code2", "Amount2", "Code3", "Amount3", "Code4", "Amount4", "Code5", "Amount5", "Receipt ID"]
                        
                        for col in final_order:
                            if col not in upload_payload.columns:
                                upload_payload[col] = ""
                                
                        upload_payload = upload_payload[final_order]
                        
                        accounts = upload_payload['Account'].unique()
                        synced_count = 0
                        skipped_count = 0
                        
                        for acc in accounts:
                            acc_str = str(acc).strip()
                            tab_name = routing_map.get(acc_str, acc_str if acc_str and acc_str != "Unknown" else "bank_transactions")
                            
                            try:
                                worksheet = spreadsheet.worksheet(tab_name) 
                            except gspread.exceptions.WorksheetNotFound:
                                worksheet = spreadsheet.add_worksheet(title=tab_name, rows="1000", cols="20")
                                worksheet.append_row(list(upload_payload.columns) + ["Unallocated", "TotalCoded"])
                            
                            existing_ids = worksheet.col_values(2) # Col 2 is Unique ID
                            
                            acc_df = upload_payload[upload_payload['Account'] == acc]
                            new_rows = acc_df[~acc_df['Unique ID'].astype(str).isin(existing_ids)]
                            
                            skipped_count += (len(acc_df) - len(new_rows))
                            
                            if not new_rows.empty:
                                worksheet.append_rows(new_rows.values.tolist())
                                synced_count += len(new_rows)
                                
                    st.success(f"✅ Success! {synced_count} new transactions synced. {skipped_count} existing duplicates skipped.")
                except Exception as e:
                    st.error(f"Failed to sync: {e}")

# ---------------------------------------------------------
# TAB 3: RECONCILIATION
# ---------------------------------------------------------
with main_tab3:
    st.title("🔄 Receipt-to-Bank Reconciliation")
    st.markdown("Match your saved receipts against your bank transactions to automatically split out the line-item coding.")
    
    def parse_dt(d_str):
        try:
            return datetime.strptime(str(d_str).strip(), '%d/%m/%Y')
        except:
            return datetime.min

    if st.button("🔍 Scan for Matches", type="primary"):
        with st.spinner("Scanning Google Sheets for unreconciled records..."):
            try:
                client = get_gs_client()
                sheet_id = st.secrets["SPREADSHEET_ID"]
                spreadsheet = client.open_by_key(sheet_id)
                
                # 1. Fetch Receipts
                try:
                    receipts_ws = spreadsheet.worksheet("receipts")
                    receipts_df = pd.DataFrame(receipts_ws.get_all_records())
                except gspread.exceptions.WorksheetNotFound:
                    st.error("No 'receipts' tab found in Google Sheets. Have you synced any receipts yet?")
                    st.stop()
                    
                if receipts_df.empty or 'Receipt ID' not in receipts_df.columns:
                    st.warning("No receipts found or missing 'Receipt ID' column.")
                    st.stop()

                # 2. Determine Bank Tabs via routing (or fallback)
                bank_tabs = []
                try:
                    routing_ws = spreadsheet.worksheet("routing")
                    for row in routing_ws.get_all_records():
                        t_name = str(row.get("Tab Name", "")).strip()
                        if t_name and t_name not in bank_tabs:
                            bank_tabs.append(t_name)
                except:
                    bank_tabs = ["bank_transactions"] # Fallback if no routing
                    
                # 3. Combine Bank Data
                all_bank_rows = []
                tab_worksheets = {}
                for tab in bank_tabs:
                    try:
                        ws = spreadsheet.worksheet(tab)
                        tab_worksheets[tab] = ws
                        
                        records = ws.get_all_records()
                        for i, r in enumerate(records):
                            # Offset by 2 (1 for 0-index, 1 for header)
                            r['_sheet'] = tab
                            r['_row'] = i + 2 
                            all_bank_rows.append(r)
                    except:
                        pass
                
                bank_df = pd.DataFrame(all_bank_rows)
                
                if bank_df.empty:
                    st.warning("No bank transactions found to reconcile.")
                    st.stop()
                    
                # 4. Filter un-reconciled
                if 'Receipt ID' not in bank_df.columns:
                    bank_df['Receipt ID'] = ""
                    
                unreconciled_bank = bank_df[bank_df['Receipt ID'].astype(str).str.strip() == ""]
                
                used_receipt_ids = set(bank_df[bank_df['Receipt ID'].astype(str).str.strip() != ""]['Receipt ID'].astype(str))
                unreconciled_receipts = receipts_df[~receipts_df['Receipt ID'].astype(str).isin(used_receipt_ids)]
                
                if unreconciled_bank.empty:
                    st.success("All bank transactions are currently reconciled!")
                    st.stop()
                if unreconciled_receipts.empty:
                    st.success("No new receipts available to match.")
                    st.stop()

                # 5. Group Receipts by logical ID
                matches = []
                grouped_receipts = unreconciled_receipts.groupby('Receipt ID')
                
                for r_id, group in grouped_receipts:
                    r_date_str = str(group['Date'].iloc[0])
                    r_store = str(group['Store Name'].iloc[0])
                    r_total = float(group['Receipt Total'].iloc[0]) if str(group['Receipt Total'].iloc[0]).strip() else 0.0
                    r_date = parse_dt(r_date_str)
                    
                    if r_total == 0: continue
                    
                    # Find candidates in bank transactions
                    for _, b_row in unreconciled_bank.iterrows():
                        try:
                            # Keep original sign for later logic
                            b_amt_raw = float(str(b_row['Amount']).replace(',', ''))
                            b_amt_abs = abs(b_amt_raw)
                        except:
                            continue
                            
                        b_date = parse_dt(b_row['Date'])
                        
                        # Match Criteria: Exact absolute amount, within 4 days
                        if abs(b_amt_abs - r_total) < 0.01:
                            if abs((b_date - r_date).days) <= 4:
                                
                                # Generate Breakdown
                                breakdown = group.groupby('Code')['Amount'].sum().reset_index()
                                breakdown = breakdown.sort_values(by='Amount', ascending=False)
                                
                                codes = breakdown['Code'].tolist()
                                amounts = breakdown['Amount'].tolist()
                                
                                # Apply original bank sign to split amounts
                                sign = 1 if b_amt_raw > 0 else -1
                                amounts = [a * sign for a in amounts]
                                
                                match_data = {
                                    'Bank_Unique_ID': b_row['Unique ID'],
                                    'Bank_Sheet': b_row['_sheet'],
                                    'Bank_Row': b_row['_row'],
                                    'Bank_Date': b_row['Date'],
                                    'Bank_Payee': b_row['Payee / Description'],
                                    'Bank_Amount': b_row['Amount'],
                                    'Receipt_ID': r_id,
                                    'Receipt_Store': r_store,
                                    'Receipt_Date': r_date_str,
                                    'Receipt_Total': r_total,
                                    'New_Codes': codes,
                                    'New_Amounts': amounts
                                }
                                matches.append(match_data)
                                break # Stop looking for this receipt once matched

                if not matches:
                    st.info("No matching pairs found based on Amount and Date (+/- 4 days).")
                else:
                    st.session_state['pending_matches'] = matches
                    st.success(f"Found {len(matches)} proposed match(es)!")

            except Exception as e:
                st.error(f"Error during scan: {e}")

    # Display pending matches
    if 'pending_matches' in st.session_state and st.session_state['pending_matches']:
        st.write("### Review Suggested Matches")
        
        to_commit = []
        
        for idx, match in enumerate(st.session_state['pending_matches']):
            with st.expander(f"🤝 Match: {match['Receipt_Store']} (${match['Receipt_Total']:.2f}) -> {match['Bank_Payee']} on {match['Bank_Date']}", expanded=True):
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown("**🧾 Receipt Details**")
                    st.write(f"**Store:** {match['Receipt_Store']}")
                    st.write(f"**Date:** {match['Receipt_Date']}")
                    st.write(f"**Total:** ${match['Receipt_Total']:.2f}")
                    st.write(f"**Receipt ID:** `{match['Receipt_ID']}`")
                with col2:
                    st.markdown("**🏦 Bank Transaction**")
                    st.write(f"**Payee:** {match['Bank_Payee']}")
                    st.write(f"**Date:** {match['Bank_Date']}")
                    st.write(f"**Amount:** ${match['Bank_Amount']}")
                    st.write(f"**Unique ID:** `{match['Bank_Unique_ID']}`")
                    
                st.markdown("**Proposed Split Coding:**")
                split_df = pd.DataFrame({'Code': match['New_Codes'], 'Amount': match['New_Amounts']})
                st.dataframe(split_df, hide_index=True)
                
                if st.checkbox(f"Confirm Match #{idx+1}", key=f"conf_{idx}"):
                    to_commit.append(match)

        if to_commit:
            st.write("---")
            if st.button("💾 Execute Selected Reconciliations", type="primary"):
                try:
                    with st.spinner("Updating Google Sheets..."):
                        client = get_gs_client()
                        sheet_id = st.secrets["SPREADSHEET_ID"]
                        spreadsheet = client.open_by_key(sheet_id)
                        
                        for match in to_commit:
                            # 1. Fetch the exact sheet
                            ws = spreadsheet.worksheet(match['Bank_Sheet'])
                            row_idx = match['Bank_Row']
                            
                            headers = ws.row_values(1)
                            
                            updates = []
                            # Update Receipt ID column
                            if "Receipt ID" in headers:
                                col_letter = gspread.utils.rowcol_to_a1(1, headers.index("Receipt ID") + 1)[0]
                                updates.append({'range': f'{col_letter}{row_idx}', 'values': [[match['Receipt_ID']]]})
                            
                            # Codes and Amounts
                            for i in range(5):
                                c_val = match['New_Codes'][i] if i < len(match['New_Codes']) else ""
                                a_val = match['New_Amounts'][i] if i < len(match['New_Amounts']) else ""
                                
                                c_col = f"Code{i+1}"
                                a_col = f"Amount{i+1}"
                                
                                if c_col in headers:
                                    col_letter = gspread.utils.rowcol_to_a1(1, headers.index(c_col) + 1)[:-1]
                                    updates.append({'range': f'{col_letter}{row_idx}', 'values': [[c_val]]})
                                if a_col in headers:
                                    col_letter = gspread.utils.rowcol_to_a1(1, headers.index(a_col) + 1)[:-1]
                                    updates.append({'range': f'{col_letter}{row_idx}', 'values': [[a_val]]})
                            
                            if updates:
                                ws.batch_update(updates)
                                
                        st.success(f"✅ Successfully applied {len(to_commit)} reconciliations to the Bank tabs!")
                        # Clear state to force refresh
                        del st.session_state['pending_matches']
                        st.rerun()
                except Exception as e:
                    st.error(f"Failed to execute updates: {e}")
