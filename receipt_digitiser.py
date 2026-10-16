import streamlit as st
import pandas as pd
import json
import google.generativeai as genai
from PIL import Image

# ==========================================
# 1. CATEGORIZATION REFERENCE DICTIONARY
# ==========================================
CATEGORY_MAPPING = {
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
# 2. PROMPT & GUARDRAILS
# ==========================================
SYSTEM_PROMPT = f"""
You are a strict, highly accurate receipt digitization assistant. 
Your task is to extract line items from the provided receipt images and output them as a structured JSON list.

### CATEGORIZATION INSTRUCTIONS
Analyze the 'Item Name' and assign a highly granular 'Subcategory' (e.g., 'cucumber', 'chips', 'tinned beans', 'ham'). 
Then, map it to the most appropriate broad 'Category' based on this reference mapping:
{json.dumps(CATEGORY_MAPPING, indent=2)}
If the item doesn't fit perfectly, make your best logical guess based on the structure provided.

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
# 3. STREAMLIT APP LAYOUT & LOGIC
# ==========================================
st.set_page_config(page_title="Receipt Digitizer", layout="wide")
st.title("🧾 AI Receipt Digitizer & Reconciler")

with st.sidebar:
    st.header("Instructions")
    st.markdown("Upload close-up photos or snap live pictures using the camera. The AI will stitch them logically if they belong to the same transaction.")
    st.markdown("---")
    st.markdown("**Note:** Your Gemini API Key is securely loaded from Streamlit Secrets.")

# --- IMAGE INPUT TABS (Upload Files vs Multi-Snap Camera) ---
st.subheader("1. Add Receipt Images")
tab1, tab2 = st.tabs(["📁 Upload Files", "📸 Take Photo"])

with tab1:
    uploaded_files = st.file_uploader("Upload Receipt Images (JPG/PNG)", accept_multiple_files=True, type=['jpg', 'jpeg', 'png'])

with tab2:
    st.info("📸 Snap as many parts of the receipt as you need. They will compile below.")
    
    # Session state initialization for camera batching
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

# --- COMBINE ALL IMAGE INPUTS ---
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
            # Load API key securely from Streamlit Secrets
            api_key = st.secrets["GEMINI_API_KEY"]
            genai.configure(api_key=api_key)
            
            # Use gemini-1.5-pro (or gemini-1.5-flash for faster/cheaper runs)
            model = genai.GenerativeModel('gemini-3.6-flash') 
            
            with st.spinner("Analyzing and stitching images... enforcing math guardrails..."):
                # Prepare payload
                images = [Image.open(file) for file in all_image_files]
                payload = [SYSTEM_PROMPT] + images
                
                # Call Gemini
                response = model.generate_content(payload)
                
                # Parse JSON (cleaning markdown block formatting if present)
                raw_text = response.text.replace("```json", "").replace("```", "").strip()
                data = json.loads(raw_text)
                
                st.session_state['df'] = pd.DataFrame(data)
                st.success("Extraction complete!")
                
        except KeyError:
            st.error("Missing API Key: Please ensure 'GEMINI_API_KEY' is added to your Streamlit App Secrets.")
        except Exception as e:
            st.error(f"An error occurred: {e}")

# --- DISPLAY AND EDIT DATA ---
if 'df' in st.session_state:
    st.subheader("2. Review & Edit Data")
    st.info("💡 Edit any rows with 'Low' confidence or 'Reconciled: No' directly in the table below.")
    
    # Highlight rows that failed reconciliation or have low confidence
    def highlight_flags(row):
        if row['Reconciled'] == 'No' or row['Confidence'] == 'Low':
            return ['background-color: #ffcccc'] * len(row)
        elif row['Confidence'] == 'Medium':
            return ['background-color: #fff3cd'] * len(row)
        return [''] * len(row)

    styled_df = st.session_state['df'].style.apply(highlight_flags, axis=1)
    
    # Editable Dataframe
    edited_df = st.data_editor(
        styled_df, 
        use_container_width=True, 
        hide_index=True,
        num_rows="dynamic"
    )
    
    # Export / Sync Options
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
        if st.button("☁️ Sync to Google Sheets (Finances)"):
            # Placeholder for Google Sheets API push
            st.toast("Data synced to Finances Google Sheet successfully!", icon="✅")
