import os
from pypdf import PdfReader

def extract_pdf_text(file_path: str) -> str:
    """
    從 PDF 檔案中快速提取所有頁面的純文字，用於臨時知識庫 RAG 解析。
    """
    if not os.path.exists(file_path):
        return ""
    try:
        reader = PdfReader(file_path)
        text_list = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                text_list.append(t)
        return "\n".join(text_list)
    except Exception as e:
        print(f"Error parsing PDF: {e}")
        return ""
