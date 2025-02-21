import logging
import os
import re
import time
import requests
import fitz  # PyMuPDF
import docx
import openai

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.section import WD_ORIENT
from docx.shared import Pt, Inches
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# ---------------------------------------------------------
# 1. Витяг тексту (PDF, DOCX, HTML)
# ---------------------------------------------------------
def extract_text(source: str):
    """
    Визначає тип джерела (PDF, DOCX, HTML-URL)
    і повертає список абзаців (рядків).
    """
    if source.startswith("http"):
        return extract_text_from_html(source)
    elif source.endswith(".pdf"):
        return extract_text_from_pdf(source)
    elif source.endswith(".docx"):
        return extract_text_from_docx(source)
    else:
        raise ValueError("Потрібен PDF/DOCX або URL.")

def extract_text_from_html(url: str):
    r = requests.get(url)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
    paragraphs = soup.find_all("p")
    return [p.get_text().strip() for p in paragraphs if p.get_text().strip()]

def extract_text_from_pdf(file_path: str):
    doc = fitz.open(file_path)
    full_text = ""
    for page in doc:
        full_text += page.get_text("text") + "\n"
    return [line.strip() for line in full_text.splitlines() if line.strip()]

def extract_text_from_docx(file_path: str):
    d = docx.Document(file_path)
    return [p.text.strip() for p in d.paragraphs if p.text.strip()]


# ---------------------------------------------------------
# 2. Google: переклад кожного абзацу окремим запитом
# ---------------------------------------------------------
def translate_text_google(text: str, max_retries=3) -> str:
    for attempt in range(max_retries):
        try:
            return GoogleTranslator(source='en', target='uk').translate(text)
        except Exception as e:
            logging.warning(f"Google Translator Error (attempt {attempt+1}/{max_retries}): {e}")
            time.sleep(2**attempt)
    return "Помилка перекладу (Google)"


# ---------------------------------------------------------
# 3. OpenAI: Chunk-логіка
# ---------------------------------------------------------
def chunk_paragraphs(paragraphs, chunk_size=5):
    """
    Розбиває список абзаців на шматки (chunk) по chunk_size.
    """
    for i in range(0, len(paragraphs), chunk_size):
        yield paragraphs[i : i + chunk_size]

def translate_chunk_openai(paragraph_chunk):
    """
    Викликає OpenAI для кількох абзаців за раз (шматок).
    Повертає список перекладів у тому ж порядку.
    """
    if not paragraph_chunk:
        return []

    # Формуємо prompt з нумерацією
    prompt_text = ""
    for i, para in enumerate(paragraph_chunk, start=1):
        prompt_text += f"{i}) {para}\n"

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": "You are a translator from English to Ukrainian."
                },
                {
                    "role": "user",
                    "content": (
                        "Translate the following list of paragraphs from English to Ukrainian. "
                        "Keep the same numbering and order.\n\n" + prompt_text
                    )
                },
            ],
            temperature=0
        )
        result_text = response.choices[0].message["content"].strip()
    except Exception as e:
        logging.warning(f"OpenAI error in chunk: {e}")
        return ["Помилка перекладу (OpenAI)"] * len(paragraph_chunk)

    # GPT, наприклад, повертає:
    # 1) Переклад абзацу 1
    # 2) Переклад абзацу 2
    # ...
    lines = result_text.splitlines()
    chunk_result = []
    current_translation = ""
    found_count = 0

    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue
        match = re.match(r"^(\d+)\)\s*(.*)$", line_stripped)
        if match:
            # Якщо вже накопичено якийсь текст, додаємо його
            if current_translation:
                chunk_result.append(current_translation.strip())
            current_translation = match.group(2)  # починаємо новий абзац
            found_count += 1
        else:
            # продовження попереднього абзацу
            current_translation += " " + line_stripped

    if current_translation:
        chunk_result.append(current_translation.strip())

    # Якщо GPT повернув менше, ніж було в chunk
    while len(chunk_result) < len(paragraph_chunk):
        chunk_result.append("Помилка: не вдалося розпарсити відповідь GPT")

    return chunk_result[: len(paragraph_chunk)]


# ---------------------------------------------------------
# 4. Функції для створення DOCX-таблиці
# ---------------------------------------------------------
def create_shading_element(color: str):
    """Створює елемент заливки комірки в таблиці."""
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), color)
    return shading

def create_translation_table(doc: Document, paragraphs, google_trans, openai_trans):
    """
    Створює таблицю з 4-ма колонками:
      №, Оригінальний текст, Google Translate, OpenAI GPT
    """
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"

    headers = ["№", "Оригінальний текст", "Google Translate", "OpenAI GPT"]
    header_fill_color = "D9EAF7"
    row_number_fill_color = "E0E0E0"

    # Заголовки
    for idx, header in enumerate(headers):
        cell = table.rows[0].cells[idx]
        cell.text = header
        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        cell.paragraphs[0].runs[0].font.bold = True
        cell._element.get_or_add_tcPr().append(create_shading_element(header_fill_color))

    # Заповнення
    for i, (para, g, o) in enumerate(zip(paragraphs, google_trans, openai_trans)):
        row_cells = table.add_row().cells
        row_cells[0].text = str(i + 1)
        row_cells[1].text = para
        row_cells[2].text = g
        row_cells[3].text = o

        # Заливка першої колонки
        row_cells[0]._element.get_or_add_tcPr().append(create_shading_element(row_number_fill_color))

        # Вирівнювання та шрифт
        for cell in row_cells:
            for paragraph in cell.paragraphs:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                for run in paragraph.runs:
                    run.font.size = Pt(9)

    # Вузька перша колонка (5%)
    total_width = Inches(10)
    first_col_width = total_width * 0.05
    other_width = (total_width - first_col_width) / 3.0
    col_widths = [first_col_width, other_width, other_width, other_width]

    for i, column in enumerate(table.columns):
        for cell in column.cells:
            cell.width = col_widths[i]

    return doc

def setup_document_orientation(doc: Document):
    """Налаштовує горизонтальну орієнтацію документа та вузькі поля."""
    section = doc.sections[-1]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    section.top_margin = Inches(0.5)
    section.bottom_margin = Inches(0.5)
    section.left_margin = Inches(0.5)
    section.right_margin = Inches(0.5)

def add_title(doc: Document):
    """Додає заголовок у документ."""
    paragraph = doc.add_paragraph()
    run = paragraph.add_run("Документ створено за допомогою скрипта перекладу LegalTransUA")
    run.bold = True
    run.font.size = Pt(12)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER


def create_shading_element(color: str):
    """Створює елемент заливки комірки в таблиці."""
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), color)
    return shading


def create_translation_table(doc: Document, paragraphs, google_trans, openai_trans):
    """
    Створює таблицю з 4-ма колонками:
      №, Оригінальний текст, Google Translate, OpenAI GPT.
    """
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"

    headers = ["№", "Оригінальний текст", "Google Translate", "OpenAI GPT"]
    header_fill_color = "D9EAF7"  # Колір фону заголовка
    row_number_fill_color = "E0E0E0"  # Колір фону для колонки з номером

    # Створюємо заголовний рядок
    for idx, header in enumerate(headers):
        cell = table.rows[0].cells[idx]
        cell.text = header
        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        cell.paragraphs[0].runs[0].font.bold = True
        # Заливка фону комірки
        cell._element.get_or_add_tcPr().append(create_shading_element(header_fill_color))

    # Заповнюємо таблицю даними
    for i, (para, g, o) in enumerate(zip(paragraphs, google_trans, openai_trans)):
        row_cells = table.add_row().cells
        row_cells[0].text = str(i + 1)
        row_cells[1].text = para
        row_cells[2].text = g
        row_cells[3].text = o

        # Заливка фону для колонки з номером
        row_cells[0]._element.get_or_add_tcPr().append(create_shading_element(row_number_fill_color))

        # Вирівнювання та розмір шрифту
        for cell in row_cells:
            for paragraph in cell.paragraphs:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                for run in paragraph.runs:
                    run.font.size = Pt(9)

    # Далі встановлюємо ширину колонок.
    # total_width - загальна ширина всієї таблиці (10 дюймів = ~25.4 см)
    total_width = Inches(10)

    # Перша колонка буде вузькою (наприклад, 5% від загальної ширини),
    # а решта 3 колонки ділять залишок порівну.
    first_col_width = total_width * 0.05  # 5% на колонку з №
    other_width = (total_width - first_col_width) / 3.0  # решту ділимо на 3

    col_widths = [
        first_col_width,  # №
        other_width,      # Оригінальний текст
        other_width,      # Google Translate
        other_width       # OpenAI GPT
    ]

    # Застосовуємо встановлені ширини до всіх рядків таблиці
    for i, column in enumerate(table.columns):
        for cell in column.cells:
            cell.width = col_widths[i]

    return doc


def save_translation_document(source, paragraphs, google_trans, openai_trans):
    """Створює та зберігає DOCX із перекладами."""
    doc = Document()
    setup_document_orientation(doc)
    add_title(doc)
    # Додаємо рядок з датою/часом перекладу
    doc.add_paragraph(f"Дата та час перекладу: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    create_translation_table(doc, paragraphs, google_trans, openai_trans)

    # ----- Формуємо ім'я файлу -----
    timestamp_str = datetime.now().strftime("%Y-%m-%d")
    if source.startswith("http"):
        # Якщо це URL
        base_name = "Document From Internet"
    else:
        # Якщо це локальний файл
        # Витягаємо "чисту" назву файлу
        base_name = os.path.splitext(os.path.basename(source))[0]
        base_name = sanitize_filename(base_name)

    # Приклад формату: "MyFile (Translated by LTUA 2025-02-21).docx"
    output_filename = f"{base_name} (Translated by LTUA {timestamp_str}).docx"

    # Зберігаємо у папці "output"
    save_dir = "output"
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    output_path = os.path.join(save_dir, output_filename)
    doc.save(output_path)
    logging.info(f"Документ збережено: {output_path}")

    return output_path


def process_document(source: str, openai_chunk_size=5):
    """
    1) Витягає абзаци
    2) Перекладає Google (покабульно, в потоках)
    3) Перекладає OpenAI (пакетно, chunk)
    4) Зберігає DOCX
    """
    paragraphs = extract_text(source)
    if not paragraphs:
        logging.error("Документ не містить тексту або текст не вдалося витягти.")
        return None

    logging.info(f"Знайдено абзаців: {len(paragraphs)}")

    # GOOGLE: виконуємо паралельний переклад (кожен абзац — запит)
    google_translations = [""] * len(paragraphs)
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(translate_text_google, p): i for i, p in enumerate(paragraphs)}
        for future in tqdm(as_completed(futures), total=len(futures), desc="Google"):
            idx = futures[future]
            google_translations[idx] = future.result()

    # OPENAI: chunk-імо (менше звернень, швидше)
    openai_translations = [""] * len(paragraphs)

    all_chunks = list(chunk_paragraphs(paragraphs, chunk_size=openai_chunk_size))
    # Припустимо, робимо ОДНОПОТОЧНО, щоб уникнути rate-limit
    # Якщо хочемо паралель, можна теж додати ThreadPoolExecutor, але обережно з rate-limit
    processed_count = 0
    total_paras = len(paragraphs)

    for chunk_index, chunk in enumerate(all_chunks):
        # Переклад для цього chunk
        chunk_result = translate_chunk_openai(chunk)
        # chunk_result — список перекладів, довжиною як chunk
        # Визначимо, куди це вставити в openai_translations:
        start_idx = chunk_index * openai_chunk_size
        for i, translation in enumerate(chunk_result):
            openai_translations[start_idx + i] = translation

        processed_count += len(chunk)
        logging.info(f"OpenAI chunk {chunk_index+1}/{len(all_chunks)} готово. (всього {processed_count}/{total_paras} абзаців)")

    # Зберігаємо у DOCX
    output_file = save_translation_document(
        source,
        paragraphs,
        google_translations,
        openai_translations
    )
    logging.info(f"Успішно збережено: {output_file}")
    return output_file


if __name__ == "__main__":
    source_path = input("Введіть URL або шлях до PDF/DOCX-файлу: ").strip()
    process_document(source_path, openai_chunk_size=5)
