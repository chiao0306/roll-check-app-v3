import streamlit as st
import streamlit.components.v1 as components
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeResult
import google.generativeai as genai
from openai import OpenAI
import json
import time
import concurrent.futures
import pandas as pd
from thefuzz import fuzz
from collections import Counter
import re

# --- 1. 頁面設定 ---
st.set_page_config(page_title="交貨單稽核(單一代理)", page_icon="🏭", layout="centered")

# --- CSS 樣式 ---
st.markdown("""
<style>
/* 1. 標題大小控制 */
h1 {
    font-size: 1.7rem !important; 
    white-space: nowrap !important;
    overflow: hidden !important; 
    text-overflow: ellipsis !important;
}

/* 2. 主功能按鈕 (紅色 Primary) -> 變大、變高 */
/* 這會影響「開始分析」和「照片清除」 */
button[kind="primary"] {
    height: 60px;               
    font-size: 20px !important; 
    font-weight: bold !important;
    border-radius: 10px !important;
    margin-top: 0px !important;    
    margin-bottom: 5px !important; 
    width: 100%;                
}

/* 3. 次要按鈕 (灰色 Secondary) -> 保持原狀 */
/* 這會影響每一張照片下面的「X」按鈕，讓它維持小小的 */
button[kind="secondary"] {
    height: auto !important;
    font-weight: normal !important;
}
</style>
""", unsafe_allow_html=True)
# --- 2. 秘密金鑰讀取 ---
try:
    DOC_ENDPOINT = st.secrets["DOC_ENDPOINT"]
    DOC_KEY = st.secrets["DOC_KEY"]
    GEMINI_KEY = st.secrets["GEMINI_KEY"]
    OPENAI_KEY = st.secrets.get("OPENAI_KEY", "")
except:
    st.error("找不到金鑰！請在 Streamlit Cloud 設定 Secrets。")
    st.stop()

# --- 3. 初始化 Session State ---
if 'photo_gallery' not in st.session_state: st.session_state.photo_gallery = []
if 'uploader_key' not in st.session_state: st.session_state.uploader_key = 0
if 'auto_start_analysis' not in st.session_state: st.session_state.auto_start_analysis = False

# --- 側邊欄模型設定 (合併為單一選擇) ---
with st.sidebar:
    st.header("模型設定")
    
    # 這裡加入最新的 Gemini 模型
    model_options = {
        "Gemini 3 Flash preview": "gemini-3-pro-image-preview",
        "Gemini 2.5 Flash": "models/gemini-2.5-flash",
        "Gemini 2.5 Pro": "models/gemini-2.5-pro",
        #"GPT-5(無效)": "models/gpt-5",
        #"GPT-5 Mini(無效)": "models/gpt-5-mini",
    }
    options_list = list(model_options.keys())
    
    st.subheader("🤖 總稽核 Agent")
    model_selection = st.selectbox(
        "負責：規格、製程、數量、統計全包", 
        options=options_list, 
        index=0, 
        key="main_model"
    )
    main_model_name = model_options[model_selection]
    
    st.divider()
    
    default_auto = st.query_params.get("auto", "true") == "true"
    def update_url_param():
        current_state = "true" if st.session_state.enable_auto_analysis else "false"
        st.query_params["auto"] = current_state

    st.toggle(
        "⚡ 上傳後自動分析", 
        value=default_auto, 
        key="enable_auto_analysis", 
        on_change=update_url_param
    )

# --- Excel 規則讀取函數 (單一代理整合版) ---
@st.cache_data
def get_dynamic_rules(ocr_text, debug_mode=False):
    try:
        df = pd.read_excel("rules.xlsx")
        df.columns = [c.strip() for c in df.columns]
        
        ocr_text_clean = str(ocr_text).upper().replace(" ", "").replace("\n", "")
        
        specific_rules = []
        general_rules = []
        match_log = []

        for index, row in df.iterrows():
            item_name = str(row.get('Item_Name', '')).strip()
            
            # --- 讀取工程欄位 ---
            spec = str(row.get('Standard_Spec', ''))
            if str(spec).lower() == 'nan': spec = ""
            
            category = str(row.get('Category', ''))
            if str(category).lower() == 'nan': category = ""
            
            logic = str(row.get('Logic_Prompt', ''))
            if str(logic).lower() == 'nan': logic = ""
            
            # --- 讀取會計三欄位 (新功能) ---
            # 1. 單項核對
            u_local = str(row.get('Unit_Rule_Local', ''))
            if u_local.lower() == 'nan': u_local = ""
            
            # 2. 聚合統計
            u_agg = str(row.get('Unit_Rule_Agg', ''))
            if u_agg.lower() == 'nan': u_agg = ""
            
            # 3. 運費計算
            u_freight = str(row.get('Unit_Rule_Freight', ''))
            if u_freight.lower() == 'nan': u_freight = ""
            
            keywords = str(row.get('Trigger_Keywords', ''))
            if str(keywords).lower() == 'nan': keywords = ""
            
            is_general_rule = "(通用)" in item_name
            
            # --- 情境 A: 通用規則 ---
            if is_general_rule:
                if not keywords:
                    rule_desc = f"- **[全域憲法] {item_name}**\n  - 指令: {logic}"
                    general_rules.append(rule_desc)
                    if debug_mode: match_log.append(f"⚖️ [憲法] {item_name} (強制載入)")
                
                elif keywords:
                    cleaned_kws = keywords.replace("，", ",").replace("、", ",").split(",")
                    cleaned_kws = [k.strip() for k in cleaned_kws if k.strip()]
                    formatted_keywords = str(cleaned_kws)

                    rule_desc = (
                        f"- **{item_name}**\n"
                        f"  - 觸發關鍵字: `{formatted_keywords}`\n"
                        f"  - 邏輯: {logic}"
                    )
                    general_rules.append(rule_desc)
                    if debug_mode: match_log.append(f"📚 [通用] {item_name} (關鍵字: {formatted_keywords})")
            
            # --- 情境 B: 特定專案規則 ---
            else:
                if not item_name: continue
                keyword_clean = item_name.upper().replace(" ", "")
                
                score = fuzz.partial_ratio(keyword_clean, ocr_text_clean)
                threshold = 85
                
                if debug_mode:
                    status_icon = "✅" if score >= threshold else "❌"
                    match_log.append(f"- {status_icon} **[特規] {item_name}** | 分數: `{score}`")
                
                if score >= threshold:
                    desc = f"- **[特定] {item_name}**"
                    # 工程資訊
                    if spec: desc += f"\n  - [工]規格標準: {spec}"
                    if logic: desc += f"\n  - [工]特殊指令: {logic}"
                    if category: desc += f"\n  - [工]分類: {category}"
                    
                    # 會計資訊 (分開列出，讓 AI 對號入座)
                    if u_local:   desc += f"\n  - [會]單項核對規則: **{u_local}**"
                    if u_agg:     desc += f"\n  - [會]聚合統計規則: **{u_agg}**"
                    if u_freight: desc += f"\n  - [會]運費計算規則: **{u_freight}**"
                    
                    specific_rules.append(desc)
        
        final_output = ""
        
        if specific_rules:
            final_output += "### 🎯 第一區：專案特定規則 (最高權限)\n" + "\n".join(specific_rules) + "\n\n"
            
        if general_rules:
            final_output += "### 📚 第二區：通用邏輯資料庫 (基礎邏輯)\n"
            final_output += "\n".join(general_rules)
            
        if not final_output:
            final_output = "無特定規則。"

        if debug_mode:
            final_output += "\n\n---\n### 🕵️‍♂️ 規則匹配日誌 (Match Log)\n" + "\n".join(match_log)
            
        return final_output

    except Exception as e:
        return f"讀取規則檔時發生錯誤: {e}"

# --- 4. 核心函數：Azure 神之眼 ---
def extract_layout_with_azure(file_obj, endpoint, key):
    client = DocumentIntelligenceClient(endpoint=endpoint, credential=AzureKeyCredential(key))
    file_content = file_obj.getvalue()
    
    poller = client.begin_analyze_document("prebuilt-layout", file_content, content_type="application/octet-stream")
    result: AnalyzeResult = poller.result()
    
    markdown_output = ""
    full_content_text = ""
    real_page_num = "Unknown"
    
    bottom_stop_keywords = ["注意事項", "中機品檢單位", "保存期限", "表單編號", "FORM NO", "簽章"]
    top_right_noise_keywords = [
        "檢驗類別", "尺寸檢驗", "依圖面標記", "材料檢驗", "成份分析", 
        "非破壞性", "正常化", "退火", "淬.回火", "表面硬化", "試車",
        "性能測試", "試壓試漏", "動.靜平衡試驗", ":selected:", ":unselected:",
        "抗拉", "硬度試驗", "UT", "PT", "MT"
    ]
    
    if result.tables:
        for idx, table in enumerate(result.tables):
            page_num = "Unknown"
            if table.bounding_regions: page_num = table.bounding_regions[0].page_number
            markdown_output += f"\n### Table {idx + 1} (Page {page_num}):\n"
            rows = {}
            stop_processing_table = False 
            
            for cell in table.cells:
                if stop_processing_table: break
                content = cell.content.replace("\n", " ").strip()
                
                for kw in bottom_stop_keywords:
                    if kw in content:
                        stop_processing_table = True
                        break
                if stop_processing_table: break
                
                is_noise = False
                for kw in top_right_noise_keywords:
                    if kw in content:
                        is_noise = True
                        break
                if is_noise: content = "" 

                r, c = cell.row_index, cell.column_index
                if r not in rows: rows[r] = {}
                rows[r][c] = content
            
            for r in sorted(rows.keys()):
                row_cells = []
                if rows[r]:
                    max_col = max(rows[r].keys())
                    for c in range(max_col + 1): 
                        row_cells.append(rows[r].get(c, ""))
                    markdown_output += "| " + " | ".join(row_cells) + " |\n"
    
    if result.content:
        match = re.search(r"(?:項次|Page|頁次|NO\.)[:\s]*(\d+)\s*[/／]\s*\d+", result.content, re.IGNORECASE)
        if match:
            real_page_num = match.group(1)

        cut_index = len(result.content)
        for keyword in bottom_stop_keywords:
            idx = result.content.find(keyword)
            if idx != -1 and idx < cut_index:
                cut_index = idx
        
        temp_text = result.content[:cut_index]
        for noise in top_right_noise_keywords:
            temp_text = temp_text.replace(noise, "")
            
        full_content_text = temp_text
        header_snippet = full_content_text[:800]
    else:
        full_content_text = ""
        header_snippet = ""

    return markdown_output, header_snippet, full_content_text, result.as_dict(), real_page_num

# --- Python 硬邏輯：表頭一致性檢查 (長度敏感版) ---
def python_header_check(photo_gallery):
    issues = []
    if not photo_gallery:
        return issues, []

    # 定義 Regex (針對 "去空白+去換行" 後的字串設計)
    patterns = {
        # 【修改點 1】工令 Regex 放寬：
        # 原本只抓 W 開頭，現在改抓 "編號" 後面接的 "任何英數字串"
        # 這樣就算它寫 WW363... 或是 12345... 都能整串抓出來比對
        "工令編號": r"[工土下][令冷今]編號[:\.]*([A-Za-z0-9\-\_]+)", 
        
        "預定交貨": r"[預预項頂][定交].*?(\d{2,4}[\.\-/]\d{1,2}[\.\-/]\d{1,2})",
        "實際交貨": r"[實真][際交].*?(\d{2,4}[\.\-/]\d{1,2}[\.\-/]\d{1,2})"
    }

    extracted_data = [] 
    all_values = {key: [] for key in patterns}

    for i, page in enumerate(photo_gallery):
        # 暴力清洗：去換行、去空格、轉大寫
        raw_text = page.get('header_text', '') + page.get('full_text', '')
        clean_text = raw_text.replace("\n", "").replace(" ", "").replace("\r", "").upper()
        
        # 【修改點 2】頁碼防呆：確保一定有值
        # 優先抓 real_page，抓不到就用 index
        r_page = page.get('real_page')
        if not r_page or r_page == "Unknown":
            page_label = f"P.{i + 1}"
        else:
            page_label = f"P.{r_page}"
            
        page_result = {"頁數": page_label}
        
        for key, pattern in patterns.items():
            match = re.search(pattern, clean_text)
            if match:
                val = match.group(1).strip()
                
                # 【修改點 3】針對工令的特殊處理 (如果太長可能就是重複打字)
                if key == "工令編號":
                    # 如果你確定工令只有 10 碼，但抓到了 11 碼以上 (如 WW...)
                    # 我們保留這個錯誤的值，讓後面的多數決去把它揪出來
                    pass 
                
                page_result[key] = val
                all_values[key].append(val)
            else:
                page_result[key] = "N/A"
        
        extracted_data.append(page_result)

    # 步驟 2: 決定「正確標準」 (使用多數決)
    standard_data = {}
    for key, values in all_values.items():
        if values:
            # 濾掉 N/A 後再投票
            valid_values = [v for v in values if v != "N/A"]
            if valid_values:
                most_common = Counter(valid_values).most_common(1)[0][0]
                standard_data[key] = most_common
            else:
                standard_data[key] = "N/A"
        else:
            standard_data[key] = "N/A"

    # 步驟 3: 比對每一頁
    for data in extracted_data:
        page_num = data['頁數']
        
        for key, standard_val in standard_data.items():
            current_val = data[key]
            
            if standard_val == "N/A": continue # 全卷都沒抓到就不比了

            # 開始比對 (字串不相等)
            if current_val != standard_val:
                
                # 判斷是否為長度異常 (針對工令)
                reason = "與全卷多數頁面不一致"
                if key == "工令編號" and len(current_val) != len(standard_val):
                    reason += f" (長度異常: {len(current_val)}碼 vs 標準{len(standard_val)}碼)"

                issue = {
                    "page": page_num.replace("P.", ""),
                    "item": f"表頭檢查-{key}",
                    "rule_used": "Python硬邏輯檢查",
                    "issue_type": "跨頁資訊不符",
                    "spec_logic": f"應為 {standard_val}",
                    "common_reason": reason,
                    "failures": [
                        {"id": "全卷基準", "val": standard_val, "calc": "多數決標準"},
                        {"id": f"本頁({page_num})", "val": current_val, "calc": "異常/漏抓"}
                    ],
                    "source": "🤖 系統自動"
                }
                issues.append(issue)
                
    return issues, extracted_data

# --- 5. 總稽核 Agent (整合版 - 強邏輯優化) ---
def agent_unified_check(combined_input, full_text_for_search, api_key, model_name):
    
    # 讀取所有規則
    dynamic_rules = get_dynamic_rules(full_text_for_search)

    system_prompt = f"""
    你是一位極度嚴謹的中鋼機械品管【總稽核官】。
    你的大腦運作必須像「電腦程式」一樣，嚴格遵守以下的「法律階級」與「執行流程」。
    完全依照規則，禁止自己解釋。

    ### 🧠 你的知識庫 (Knowledge Base)
    {dynamic_rules}
    
    ---

    ### ⚖️ 判決憲法 (Hierarchy of Authority)
    **請注意：判定標準分為「數據層」與「邏輯層」，兩者必須同時成立。**

    **第 1 階級：[第一區：專案特定規則] (Specific Data)**
    *   **權力**：定義該項目的 **「目標數值」**。若有數值，以此為準。
    *   **指令**：若 `特殊指令(Logic)` 為空，代表 **「完全遵守通用邏輯」**。

    **第 2 階級：[第二區：通用邏輯資料庫] (General Logic)**
    *   **權力**：定義全廠通用的 **「物理法則」** (如順序、依賴性)。
    *   **強制性**：**預設為開啟狀態**。除非第 1 階級明確寫出「豁免」，否則不可關閉。

    ---

    ### 🚀 執行程序 (Execution Procedure)

    #### ⚔️ 模組 A：工程規格稽核 (Engineering)
    **判定公式：PASS = (Step 1 數值合格) AND (Step 2 邏輯合格)**

    **Step 1: 特規指令與數值檢查**
    *   **讀取**：[第一區] 的 `Standard_Spec` 與 `Logic_Prompt`。
    *   **檢查指令**：若 `Logic_Prompt` 有內容，優先執行。
    *   **比對數值**：若有 `Standard_Spec`，以此為標準。

    **Step 2: 物理與通用邏輯檢查 (CRITICAL STEP)**
    *   **啟動條件**：
        *   `IF` Step 1 的 `Logic_Prompt` 是 **空白 (Empty)** -> **必須執行 Step 2**。
        *   `IF` Step 1 的 `Logic_Prompt` 寫了 "豁免" -> 只有這種情況才可跳過。
    *   **執行動作**：
        1.  **物理順序**：檢查 `未再生(小) < 研磨(中) < 再生(大) < 銲補(最大)`。若違反 -> **FAIL**。
        2.  **依賴性**：檢查前後製程是否存在。
        3.  **通用格式**：若通用規則要求兩位小數，實測值須符合。

    ### 🚀 執行模組 B：會計數量核對 (三階段獨立參數)
    **請注意：會計檢查分為三個獨立步驟，每個步驟必須參考 Excel 對應的規則欄位。**
    
    **Step 1: 單項數量計算 (Local Calculation)**
    *   **算法**：項目計數（目標數） = 列表的"編號"個數。
        例：規範標準：W3 #6 295（X） ROLL 本體未再生車修（12PC），此項目後要有12個編號。
        *   **本體 (Body)**: 使用 `Count Distinct` (去重計算獨立編號)。
        *   **軸頸/內孔**: 使用 `Count Total Rows` (計算總行數)，項目內每個獨立編號**不可重複超過2次**。
        *   **參數來源**：查看特規的 **`[會]單項核對規則`**。
        *   若有 (如 "1SET=4PCS")：以此為準計算 (Rows / 4)。
        *   若無：預設 `1 SET = 2 PCS`, `1 PC = 1 PC`。
      
    **Step 2: 總表核對 (Global Summary Check)**
    *   **目標**：核對左上角「實交數量」 vs 「跨頁內文項目加總」。
    *   **執行邏輯**：請先讀取左上角的「項目名稱」，依據下列規則決定哪些「內文項目」需要被加總：

    **A. 雙軌聚合模式 (Aggregated Mode)**
    *   **觸發條件**：當左上角項目名稱 **同時包含** 「ROLL」 與 「車修 / 銲補 / 拆裝」其中之一時。
        *   *(例如："W3 #1機 ROLL 車修", "ROLL 銲補")*
    *   **加總範圍 (預設)**：
        *   **車修** = Sum(本體未再生 + 本體再生 + 軸頸未再生 + 軸頸再生)
        *   **銲補** = Sum(本體銲補 + 軸頸銲補)
        *   **拆裝** = Sum(新品組裝 + 舊品拆裝)
    *   **例外過濾 (特規介入)**：
        *   在加總上述項目之前，**必須**檢查該項目的 **`[會]聚合統計規則`**。
        *   若寫 **"豁免"** 或 **"強制歸類為通用"**：❌ **嚴禁**將其加入上述總帳。
        *   若寫 **"1SET=1PC"**：⚠️ 僅加入 **1** 個單位 (而非內文的實際行數)。

    **B. 標準對應模式 (Standard Mode)**
    *   **觸發條件**：當左上角項目名稱 **不包含** 上述聚合關鍵字時 (例如「熱處理」、「Keyway」)。
    *   **加總範圍**：僅加總內文中 **「名稱完全對應」** 或 **「邏輯上屬於該項目」** 的子項目。
    *   **邏輯**：此模式下，**忽略** Excel 的 `[會]聚合統計規則`。只要名稱對上，就直接加總。

    **Step 3: 運費計算 (Freight Check)
    *   **任務**：計算全卷「本體未再生車修」總數，核對左上角運費項次總數。
    *   **參數來源**：查看特規的 **`[會]運費計算規則`**。
        *   若寫 **"豁免"**：**嚴禁**將此項目計入運費。
        *   若寫 **"1SET=1PC"**：以 1:1 累加至運費。
        *   若無：預設依據 Step 1 的結果累加。

    ---
    
    ### 📝 輸出規範 (Output Format)
    **請回傳單一 JSON 物件。**
    
    **【關鍵指令：統計不符時的強制格式】**
    若發生 **統計表格數量不符**，`failures` 列表 **嚴禁** 寫 "Unknown"。
    **必須** 生成以下兩行對照數據：
    1. `{{ "id": "內文項目加總", "val": "計算值", "calc": "計算" }}`
    2. `{{ "id": "統計表實交數量", "val": "目標值", "calc": "目標" }}`

    {{
      "job_no": "工令編號",
      "issues": [
         {{
           "page": "頁碼",
           "item": "項目名稱",
           "rule_used": "依據的規則 (請註明是 特規 還是 通用)",
           "issue_type": "數值超規 / 流程異常 / 數量不符 / 統計不符",
           "spec_logic": "判定標準",
           "common_reason": "簡短原因 (限15字)",
           "failures": [
              {{ "id": "滾輪編號/項目", "val": "實測值/計數", "target": "規格/備註", "calc": "差值(若有)" }}
           ]
         }}
      ]
    }}
    """
    
    generation_config = {"response_mime_type": "application/json", "temperature": 0.0, "top_k": 1, "top_p": 0.95}
    
    try:
        # === 分流 A: Google Gemini ===
        if "gemini" in model_name.lower():
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(model_name)
            response = model.generate_content([system_prompt, combined_input], generation_config=generation_config)
            
            raw_content = response.text
            usage_meta = response.usage_metadata
            usage_in = usage_meta.prompt_token_count if usage_meta else 0
            usage_out = usage_meta.candidates_token_count if usage_meta else 0

        # === 分流 B: OpenAI GPT ===
        else:
            if not OPENAI_KEY:
                return {"job_no": "Error", "issues": [{"item": "Error", "common_reason": "缺少 OpenAI Key"}], "_token_usage": {"input":0, "output":0}}
            
            client = OpenAI(api_key=OPENAI_KEY)
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": combined_input}
                ],
                temperature=0.0
            )
            raw_content = response.choices[0].message.content
            usage_in = response.usage.prompt_tokens
            usage_out = response.usage.completion_tokens

        # =========================================================
        # 🛡️ 絕對防禦：JSON 解析與結構重建
        # =========================================================
        
        # 1. 清洗 Markdown
        if "```json" in raw_content:
            raw_content = raw_content.replace("```json", "").replace("```", "")
        elif "```" in raw_content:
            raw_content = raw_content.replace("```", "")
            
        # 2. 嘗試解析
        try:
            parsed_data = json.loads(raw_content)
        except:
            parsed_data = {"job_no": "JSON Error", "issues": []}

        # 3. 建構最終回傳物件
        final_response = {}

        if isinstance(parsed_data, dict):
            final_response = parsed_data
        elif isinstance(parsed_data, list):
            final_response = {"job_no": "Unknown", "issues": parsed_data}
        else:
            final_response = {"job_no": "Unknown", "issues": []}

        # 4. 補全必要欄位
        if "issues" not in final_response:
            final_response["issues"] = []
        if "job_no" not in final_response:
            final_response["job_no"] = "Unknown"

        # 5. 【修改點】垃圾過濾器 (Garbage Collector) & 矛盾清洗
        valid_issues = []
        for i in final_response["issues"]:
            if isinstance(i, dict):
                item_name = i.get("item", "")
                reason = i.get("common_reason", "")
                i_type = i.get("issue_type", "")

                # 1. 基本防呆：沒有 item 名稱就踢掉
                if not item_name: 
                    continue

                # 2. 【關鍵修正】矛盾清洗
                # 如果 AI 說「合格」，但這又不是「未匹配規則」的強制回報 -> 代表這是 AI 多嘴，踢掉！
                if "合格" in reason and "未匹配" not in i_type:
                    continue
                
                # 3. 如果 AI 說「合格」，且是「未匹配」，但 issue_type 卻寫「數值超規」 -> 強制修正類型
                if "合格" in reason and "未匹配" in i_type:
                    i["issue_type"] = "⚠️未匹配規則" # 強制修正為黃色警告

                valid_issues.append(i)
        
        # 將清洗後的乾淨清單放回去
        final_response["issues"] = valid_issues

        # 6. 注入 Token 用量
        final_response["_token_usage"] = {"input": usage_in, "output": usage_out}
        
        return final_response

    except Exception as e:
        return {"job_no": "Error", "issues": [{"item": "System Error", "common_reason": str(e)}], "_token_usage": {"input": 0, "output": 0}}

# --- 6. 手機版 UI 與 核心執行邏輯 ---
st.title("🏭 交貨單稽核(單一代理)")

data_source = st.radio(
    "請選擇資料來源：", 
    ["📸 上傳照片", "📂 上傳 JSON 檔"], 
    horizontal=True
)

with st.container(border=True):
    if data_source == "📸 上傳照片":
        if st.session_state.get('source_mode') == 'json':
            st.session_state.photo_gallery = []
            st.session_state.source_mode = 'image'

        uploaded_files = st.file_uploader(
            "請選擇 JPG/PNG 照片...", 
            type=['jpg', 'png', 'jpeg'], 
            accept_multiple_files=True, 
            key=f"uploader_{st.session_state.uploader_key}"
        )
        
        if uploaded_files:
            for f in uploaded_files: 
                if not any(x['file'].name == f.name for x in st.session_state.photo_gallery if x['file']):
                    st.session_state.photo_gallery.append({
                        'file': f, 
                        'table_md': None, 
                        'header_text': None,
                        'full_text': None,
                        'raw_json': None
                    })
            st.session_state.uploader_key += 1
            if st.session_state.enable_auto_analysis:
                st.session_state.auto_start_analysis = True
            components.html("""<script>window.parent.document.body.scrollTo(0, window.parent.document.body.scrollHeight);</script>""", height=0)
            st.rerun()
            
    else: 
        st.info("💡 請點擊下方按鈕，從你的資料夾選擇之前下載的 `.json` 檔。")
        uploaded_json = st.file_uploader("上傳JSON檔", type=['json'], key="json_uploader")
        
        if uploaded_json:
            try:
                current_file_name = uploaded_json.name
                last_loaded_file = st.session_state.get('last_loaded_json_name')

                if current_file_name != last_loaded_file:
                    json_data = json.load(uploaded_json)
                    # ... (前段代碼) ...
                    
                    # 強制重置相簿
                    st.session_state.photo_gallery = []
                    st.session_state.source_mode = 'json'
                    st.session_state.last_loaded_json_name = current_file_name
                    
                    # 引入 regex 模組 (如果上面沒引用的話)
                    import re

                    # 還原資料
                    for page in json_data:
                        # 【修改點】嘗試從 full_text 重新抓取真實頁碼
                        real_page = "Unknown"
                        full_text = page.get('full_text', '')
                        
                        # 使用跟 Azure 一樣的 Regex 抓取 "項次: 3/4"
                        if full_text:
                            match = re.search(r"(?:項次|Page|頁次|NO\.)[:\s]*(\d+)\s*[/／]\s*\d+", full_text, re.IGNORECASE)
                            if match:
                                real_page = match.group(1)
                        
                        # 如果 JSON 裡原本就有存，也可以優先用存的
                        # 但重抓一次比較保險
                        
                        st.session_state.photo_gallery.append({
                            'file': None,
                            'table_md': page.get('table_md'),
                            'header_text': page.get('header_text'),
                            'full_text': full_text,
                            'raw_json': page.get('raw_json'),
                            'real_page': real_page # <--- 把抓到的頁碼存進去！
                        })
                    
                    # ... (後段代碼) ...
                    
                    st.toast(f"✅ 成功載入: {current_file_name}", icon="📂")
                    if st.session_state.enable_auto_analysis:
                        st.session_state.auto_start_analysis = True
                    st.rerun()
                else:
                    st.success(f"📂 目前載入檔案：**{uploaded_json.name}** (共 {len(st.session_state.photo_gallery)} 頁)")
            except Exception as e:
                st.error(f"JSON 檔案格式錯誤: {e}")

if st.session_state.photo_gallery:
    st.caption(f"已累積 {len(st.session_state.photo_gallery)} 頁文件")
    col_btn1, col_btn2 = st.columns([1, 1], gap="small")
    with col_btn1: start_btn = st.button("🚀 開始分析", type="primary", use_container_width=True)
    with col_btn2: 
        clear_btn = st.button("🗑️照片清除", help="清除", use_container_width=True)

    if clear_btn:
        st.session_state.photo_gallery = []
        st.session_state.analysis_result_cache = None
        if 'last_loaded_json_name' in st.session_state:
            del st.session_state.last_loaded_json_name 
        st.rerun()

    is_auto_start = st.session_state.auto_start_analysis
    if is_auto_start:
        st.session_state.auto_start_analysis = False

    if 'analysis_result_cache' not in st.session_state:
        st.session_state.analysis_result_cache = None

    trigger_analysis = start_btn or is_auto_start

    if trigger_analysis:
        total_start = time.time()
        status = st.empty()
        progress_bar = st.progress(0)
        
        extracted_data_list = [None] * len(st.session_state.photo_gallery)
        full_text_for_search = ""
        total_imgs = len(st.session_state.photo_gallery)
        
        ocr_start = time.time()
        
        def process_image_task(index, item):
            index = int(index)
            if item.get('table_md') and item.get('header_text') and item.get('full_text'):
                real_page = item.get('real_page', str(index + 1))
                return index, item['table_md'], item['header_text'], item['full_text'], item.get('raw_json'), real_page, None
            
            try:
                if item.get('file') is None:
                    return index, None, None, None, None, None, "無圖片檔案"
                
                item['file'].seek(0)
                table_md, header, full, raw, real_page = extract_layout_with_azure(item['file'], DOC_ENDPOINT, DOC_KEY)
                return index, table_md, header, full, raw, real_page, None
            except Exception as e:
                return index, None, None, None, None, None, f"OCR失敗: {str(e)}"

        status.text(f"Azure 正在平行掃描 {total_imgs} 頁文件...")

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = []
            for i, item in enumerate(st.session_state.photo_gallery):
                futures.append(executor.submit(process_image_task, i, item))
            
            completed_count = 0
            for future in concurrent.futures.as_completed(futures):
                idx, t_md, h_txt, f_txt, raw_j, r_page, err = future.result()
                idx = int(idx)
                
                if err:
                    st.error(f"第 {idx+1} 頁讀取失敗: {err}")
                    extracted_data_list[idx] = None
                else:
                    st.session_state.photo_gallery[idx]['table_md'] = t_md
                    st.session_state.photo_gallery[idx]['header_text'] = h_txt
                    st.session_state.photo_gallery[idx]['full_text'] = f_txt
                    st.session_state.photo_gallery[idx]['raw_json'] = raw_j
                    st.session_state.photo_gallery[idx]['real_page'] = r_page
                    
                    extracted_data_list[idx] = {
                        "page": r_page,
                        "table": t_md or "", 
                        "header_text": h_txt or ""
                    }
                
                completed_count += 1
                progress_bar.progress(completed_count / (total_imgs + 1))
        
        for i, data in enumerate(extracted_data_list):
            if data and isinstance(data, dict):
                page_idx = i
                if 0 <= page_idx < len(st.session_state.photo_gallery):
                    full_text_for_search += st.session_state.photo_gallery[page_idx].get('full_text', '')

        ocr_end = time.time()
        ocr_duration = ocr_end - ocr_start

        combined_input = "以下是各頁資料：\n"
        for i, data in enumerate(extracted_data_list):
            if data is None: continue
            page_num = data.get('page', i+1)
            table_text = data.get('table', '')
            header_text = data.get('header_text', '')
            combined_input += f"\n=== Page {page_num} ===\n【頁首】:\n{header_text}\n【表格】:\n{table_text}\n"
            
        status.text("總稽核 Agent 正在進行全方位分析...")
        
        # --- 單一代理執行 ---
        t0 = time.time()
        # 呼叫合併後的 Agent
        res_main = agent_unified_check(combined_input, full_text_for_search, GEMINI_KEY, main_model_name)
        t1 = time.time()
        time_main = t1 - t0
        
        progress_bar.progress(100)
        status.empty()
        
        total_end = time.time()
        
        # --- 成本計算 (單次呼叫) ---
        usage_main = res_main.get("_token_usage", {"input": 0, "output": 0})
        
        # 費率判斷
        def get_model_rate(model_name):
            name = model_name.lower()
            if "gpt" in name:
                if "mini" in name: return 0.15, 0.60
                elif "3.5" in name: return 0.50, 1.50
                else: return 2.50, 10.00
            else:
                # Gemini 費率
                if "flash" in name: return 0.075, 0.30
                else: return 1.25, 5.00 # Pro

        rate_in, rate_out = get_model_rate(main_model_name)
        
        cost_usd = (usage_main["input"] / 1_000_000 * rate_in) + (usage_main["output"] / 1_000_000 * rate_out)
        cost_twd = cost_usd * 32.5
        
        # --- Python 表頭檢查 ---
        python_header_issues, python_debug_data = python_header_check(st.session_state.photo_gallery)
        
        # --- 合併結果 ---
        ai_issues = res_main.get("issues", [])
        for i in ai_issues: 
            i['source'] = '🤖 總稽核 AI'
            
        all_issues = ai_issues + python_header_issues
        
        st.session_state.analysis_result_cache = {
            "job_no": res_main.get("job_no", "Unknown"),
            "all_issues": all_issues,
            "total_duration": total_end - total_start,
            "cost_twd": cost_twd,
            "total_in": usage_main["input"],
            "total_out": usage_main["output"],
            "ocr_duration": ocr_duration,
            "time_eng": time_main, # 這裡借用變數名，實為總時間
            "time_acc": 0,         # 單一代理無第二時間
            "full_text_for_search": full_text_for_search,
            "combined_input": combined_input,
            "python_debug_data": python_debug_data
        }

    if st.session_state.analysis_result_cache:
        cache = st.session_state.analysis_result_cache
        all_issues = cache['all_issues']
        
        st.success(f"工令: {cache['job_no']} | ⏱️ {cache['total_duration']:.1f}s")
        st.info(f"💰 本次成本: NT$ {cache['cost_twd']:.2f} (In: {cache['total_in']:,} / Out: {cache['total_out']:,})")
        st.caption(f"細節耗時: Azure OCR {cache['ocr_duration']:.1f}s | AI 分析 {cache['time_eng']:.1f}s")
        
        with st.expander("🔍 查看 AI 讀取到的 Excel 規則 (Debug)"):
            rules_text = get_dynamic_rules(cache['full_text_for_search'], debug_mode=True)
            if "無特定規則" in rules_text:
                st.caption("無匹配規則")
            else:
                st.markdown(rules_text)

        with st.expander("🐍 查看 Python 硬邏輯偵測結果 (Debug)", expanded=False):
            if cache.get('python_debug_data'):
                p_data = cache['python_debug_data']
                standard_data = {}
                all_values = {"工令編號": [], "預定交貨": [], "實際交貨": []}
                for page in p_data:
                    for k in all_values.keys():
                        if page.get(k) and page[k] != "N/A":
                            all_values[k].append(page[k])
                
                standard_row = {"頁碼": "🏆 判定標準"}
                for k, v in all_values.items():
                    if v:
                        standard_row[k] = Counter(v).most_common(1)[0][0]
                    else:
                        standard_row[k] = "N/A"
                
                final_df_data = [standard_row] + p_data
                st.dataframe(final_df_data, use_container_width=True, hide_index=True)
                st.info("💡 「判定標準」是依據多數決產生的。")
            else:
                st.caption("無偵測資料")

        real_errors = [i for i in all_issues if "未匹配" not in i.get('issue_type', '')]
        
        if not real_errors:
            st.balloons()
            if not all_issues:
                st.success("✅ 全數合格！")
            else:
                st.success(f"✅ 數值全數合格！ (但有 {len(all_issues)} 個項目未匹配規則，請檢查)")
        else:
            st.error(f"發現 {len(real_errors)} 類數值異常，另有 {len(all_issues) - len(real_errors)} 個項目未匹配規則")

        for item in all_issues:
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                
                source_label = item.get('source', '')
                rule_source = item.get('rule_used', '系統預設邏輯')
                issue_type = item.get('issue_type', '異常')
                common_reason = item.get('common_reason', '')
                
                c1.markdown(f"**P.{item.get('page', '?')} | {item.get('item')}**  `{source_label}`")
                
                if "Excel" in rule_source:
                    c1.caption(f"📜 判斷依據: :blue-background[{rule_source}]")
                elif "無對應" in rule_source or "盲測" in rule_source:
                    c1.caption(f"⚠️ 判斷依據: :grey-background[❓ 無對應規則 (盲測)]")
                else:
                    c1.caption(f"🤖 判斷依據: {rule_source}")
                
                if "未匹配" in issue_type:
                    if "合格" in common_reason:
                        c2.warning(f"⚠️ 未匹配") 
                    else:
                        c2.error(f"🛑 未匹配超規") 
                elif "流程" in issue_type or "尺寸" in issue_type or "統計" in issue_type:
                    c2.error(f"🛑 {issue_type}")
                else:
                    c2.warning(f"⚠️ {issue_type}")
                
                st.caption(f"原因: {common_reason}")
                
                spec = item.get('spec_logic') or item.get('target_spec')
                if spec: st.caption(f"標準: {spec}")
                
                if item.get('verification_logic'): st.caption(f"驗證: {item.get('verification_logic')}")
                
                failures = item.get('failures', [])
                if failures:
                    table_data = []
                    for f in failures:
                        if isinstance(f, dict):
                            row = {
                                "滾輪編號": f.get('id', '未知'), 
                                "實測/計數": f.get('val', 'N/A')
                            }
                            if f.get('calc'): row["差值/備註"] = f.get('calc')
                            if f.get('target'): row["規格/備註"] = f.get('target')
                            table_data.append(row)
                        elif isinstance(f, str):
                            table_data.append({"滾輪編號": "-", "內容": f})
                    if table_data:
                        st.dataframe(table_data, use_container_width=True, hide_index=True)
                
                elif 'roll_id' in item:
                    table_data = [{
                        "滾輪編號": item.get('roll_id'),
                        "實測值": item.get('raw_value'),
                        "規格": item.get('target_spec')
                    }]
                    st.dataframe(table_data, use_container_width=True, hide_index=True)
                else:
                    st.text(f"實測數據: {item.get('measured', 'N/A')}")
        
        st.divider()

        current_job_no = cache.get('job_no', 'Unknown')
        safe_job_no = current_job_no.replace("/", "_").replace("\\", "_").strip()
        file_name_str = f"{safe_job_no}_cleaned.json"

        # 準備匯出資料
        export_data = []
        for item in st.session_state.photo_gallery:
            export_data.append({
                "table_md": item.get('table_md'),
                "header_text": item.get('header_text'),
                "full_text": item.get('full_text'),
                "raw_json": item.get('raw_json')
            })
        json_str = json.dumps(export_data, indent=2, ensure_ascii=False)

        st.subheader("💾 測試資料存檔")
        st.caption(f"已識別工令：**{current_job_no}**。下載後可供下次測試使用。")
        
        st.download_button(
            label=f"⬇️ 下載測試資料 ({file_name_str})",
            data=json_str,
            file_name=file_name_str,
            mime="application/json",
            type="primary"
        )

        with st.expander("👀 查看傳給 AI 的最終文字 (Prompt Input)"):
            st.caption("這才是 AI 真正讀到的內容 (已過濾雜訊)：")
            st.code(cache['combined_input'], language='markdown')
            
        st.markdown("### 🔍 Azure OCR 原始資料 (Debug)")
        for i, item in enumerate(st.session_state.photo_gallery):
            with st.expander(f"📄 第 {i+1} 頁 Raw JSON", expanded=False):
                if item.get('raw_json'):
                    st.json(item.get('raw_json'))
                else:
                    st.caption("尚未取得資料")
    
    if st.session_state.photo_gallery and st.session_state.get('source_mode') != 'json':
        st.caption("已拍攝照片：")
        cols = st.columns(4)
        for idx, item in enumerate(st.session_state.photo_gallery):
            with cols[idx % 4]:
                if item.get('file'):
                    st.image(item['file'], caption=f"P.{idx+1}", use_container_width=True)
                if st.button("❌", key=f"del_{idx}"):
                    st.session_state.photo_gallery.pop(idx)
                    st.session_state.analysis_result_cache = None
                    st.rerun()
else:
    st.info("👆 請點擊上方按鈕開始新增照片")
