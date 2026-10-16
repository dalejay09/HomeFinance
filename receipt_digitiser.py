import streamlit as st
import pandas as pd
import json
import google.generativeai as genai
from PIL import Image
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# 1. DEFAULT FALLBACK CATEGORY DICTIONARY
# ==========================================
# Used only if the "coding" tab in Google Sheets cannot be accessed.
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
# 2. DYNAMIC PROMPT BUILDER
# ==========================================
def build_system_prompt(mapping_dictionary):
    """Generates the prompt dynamically injected with the latest category mappings."""
    return f"""
You are a strict, highly accurate receipt digitization assistant. 
Your task is to extract line items from the provided receipt images and output them as a structured JSON list.

### CATEGORIZATION INSTRUCTIONS
Analyze the 'Item Name' and assign a highly granular 'Subcategory' (e.g., 'cucumber', 'chips', 'tinned beans', 'ham'). 
Then, map it to the most appropriate broad 'Category' based on this reference mapping:
{json.dumps(mapping_dictionary, indent=2)}
If the item doesn't fit perfectly, make your best logical guess based on the structure provided.

**Discount Rule:** If a line item is a discount or promo reduction (e.g., a negative amount or labeled 'Discount'), you MUST assign it the exact same 'Category' and 'Subcategory' as the item it applies to (this is almost always the line item immediately preceding it on the receipt).

### STRICT PROCESSING GUARDRAILS
1. No Mathematical Inference (Semantic Bleed Protection): Never auto-fill, infer, or guess a missing price based on past receipts or item names.
2. Blanking: If a value is physically erased, chopped off, or illegible, extract it exactly as 'BLANK'.
3. Confidence Scoring: 
   - 'High': Text is crisp and perfectly aligned.
   - 'Medium': Minor visual artifacts or slight fading, but highly probable.
   - 'Low': Obscured, heavy crinkles, misaligned, or partially destroyed. 'BLANK' values must always be 'Low'.
4. Strict Reconciliation: Calculate the sum of all item Amounts (including negative discounts). If the calculated sum matches the printed Receipt Total exactly, mark Reconciled as 'Yes'. If there is ANY discrepancy, or if any value is 'BLANK', mark Reconciled as 'No' for every row on that receipt.

Output ONLY a raw JSON array of objects with the following keys exactly:
"Store Name", "Date", "Item Name", "Quantity", "Amount", "Invoice Number", "Receipt Total", "Reconciled", "Confidence", "Category", "Subcategory"
"""

# ==========================================
# 3. HELPER FUNCTIONS
# ==========================================
def resize_image(image, max_size=(1024, 1024)):
    """Resizes an image before sending it to Gemini to save bandwidth and API limits."""
    image.thumbnail(max_size, Image.Resampling.LANCZOS)
    return image

def fetch_dynamic_categories():
    """Attempts to fetch the 'coding' tab from Google Sheets to use as the brain."""
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
            if cat: # Only add if Category isn't blank
                dynamic_mapping[cat] = examples
                
        if dynamic_mapping:
            return dynamic_mapping
    except Exception as e:
        print(f"Could not load dynamic categories, falling back to default. {e}")
        pass
    
    # Return None if it fails so the app knows to use the fallback
    return None


# ==========================================
# 4. STREAMLIT APP LAYOUT & LOGIC
# ==========================================
st.set_page_config(page_title="Receipt Digitizer", layout="wide")
st.title("🧾 AI Receipt Digitizer & Reconciler")

with st.sidebar:
    st.header("Instructions")
    st.markdown("Upload close-up photos or snap live pictures using the camera. The AI will stitch them logically if they belong to the same transaction.")
    st.markdown("---")
    st.markdown("**Note:** Your Gemini API Key and Google Sheets credentials are securely loaded from Streamlit Secrets.")

# --- IMAGE INPUT TABS ---
st.subheader("1. Add Receipt Images")
tab1, tab2 = st.tabs(["📁 Upload Files", "📸 Take Photo"])

with tab1:
    uploaded_files = st.file_uploader("Upload Receipt Images (JPG/PNG)", accept_multiple_files=True, type=['jpg', 'jpeg', 'png'])

with tab2:
    st.info("📸 Snap as many parts of the receipt as you need. They will compile below.")
    if 'camera_snaps' not in st.session_state:
        st.session_state.camera_snaps = []
        
    new_snap = st.camera_input("Snap a live photo")
    
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
        st.warning("Please upload or snap at least one image.")
    else:
        try:
            api_key = st.secrets["GEMINI_API_KEY"]
            genai.configure(api_key=api_key)
            
            # 🚀 Updated to the latest 3.8 Flash model!
            model = genai.GenerativeModel('gemini-3.8-flash') 
            
            with st.spinner("Fetching category rules and analyzing images..."):
                
                # 1. Try to get categories from the 'coding' sheet, otherwise use fallback
                mapping_dict = fetch_dynamic_categories()
                if not mapping_dict:
                    mapping_dict = DEFAULT_CATEGORY_MAPPING
                
                # 2. Build the final prompt
                final_prompt = build_system_prompt(mapping_dict)
                
                # 3. Process images
                images = [resize_image(Image.open(file)) for file in all_image_files]
                payload = [final_prompt] + images
                
                response = model.generate_content(payload)
                
                raw_text = response.text.replace("```json", "").replace("```", "").strip()
                data = json.loads(raw_text)
                
                st.session_state['df'] = pd.DataFrame(data)
                st.success("Extraction complete!")
                
        except KeyError as e:
            st.error(f"Missing Secret: Please ensure {e} is added to your Streamlit App Secrets.")
        except Exception as e:
            st.error(f"An error occurred: {e}")

# --- DISPLAY AND EDIT DATA ---
if 'df' in st.session_state:
    st.subheader("2. Review & Edit Data")
    st.info("💡 Edit any rows with 'Low' confidence or 'Reconciled: No' directly in the table below.")
    
    def highlight_flags(row):
        if row['Reconciled'] == 'No' or row['Confidence'] == 'Low':
            return ['background-color: #ffcccc'] * len(row)
        elif row['Confidence'] == 'Medium':
            return ['background-color: #fff3cd'] * len(row)
        return [''] * len(row)

    styled_df = st.session_state['df'].style.apply(highlight_flags, axis=1)
    
    edited_df = st.data_editor(
        styled_df, 
        use_container_width=True, 
        hide_index=True,
        num_rows="dynamic"
    )
    
    # --- EXPORT / SYNC OPTIONS ---
    st.write("---")
    col1, col2 = st.columns(2)
    with col1:
        csv = edited_df.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="⬇️ Download CSV",
            data=csv,
            file_name="reconciled_receipts.csv",
            mime="text/csv",
        )
    with col2:
        if st.button("☁️ Sync to Google Sheets"):
            try:
                with st.spinner("Connecting to Google Sheets..."):
                    creds_dict = json.loads(st.secrets["GCP_JSON_STRING"])
                    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
                    creds = Credentials.from_service_account_info(
                        creds_dict, scopes=scopes
                    )
                    client = gspread.authorize(creds)
                    
                    sheet_id = st.secrets["SPREADSHEET_ID"]
                    spreadsheet = client.open_by_key(sheet_id)
                    worksheet = spreadsheet.worksheet("receipts") 
                    
                    df_upload = edited_df.fillna("")
                    worksheet.append_rows(df_upload.values.tolist())
                    
                st.success("✅ Data securely synced to your Google Sheet!")
                
            except KeyError as e:
                st.error(f"Missing Secret: Ensure {e} is added to Streamlit Secrets.")
            except json.JSONDecodeError:
                st.error("Failed to parse JSON. Please ensure you pasted the exact contents of the JSON file between the ''' quotes in Streamlit Secrets.")
            except Exception as e:
                st.error(f"Failed to sync: {e}")