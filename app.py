import streamlit as st
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from docx import Document
import re

# Спочатку зчитаємо секрети і виставляємо openai.api_key
if "OPENAI_API_KEY" in st.secrets:
    os.environ["OPENAI_API_KEY"] = st.secrets["OPENAI_API_KEY"]

import openai
openai.api_key = os.environ.get("OPENAI_API_KEY", None)

# Тепер імпортуємо translate_script
from translate_script import (
    extract_text,
    translate_text_google,
    chunk_paragraphs,
    translate_chunk_openai,
    create_translation_table,
    setup_document_orientation,
    add_title
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

st.set_page_config(page_title="LegalTransUA", layout="wide")

TEMP_DIR = "temp"
if not os.path.exists(TEMP_DIR):
    os.makedirs(TEMP_DIR)


def sanitize_filename(filename: str) -> str:
    """Очищає ім'я файлу від недопустимих символів."""
    name, ext = os.path.splitext(filename)
    return re.sub(r'[<>:"/\\|?*]', '_', name) + ext


st.image("https://i.imgur.com/JmLIg6y.jpeg", use_container_width=True)

# Меню
st.sidebar.title("Меню навігації")
section = st.sidebar.radio(
    "Перейдіть до розділу:",
    ["Головна сторінка", "Про додаток", "Корисні посилання", "Допомога Україні", "Контакти"]
)

def save_uploaded_file(uploaded_file):
    """
    Зберігає файл у тимчасову папку та повертає шлях до нього.
    """
    file_path = os.path.join(TEMP_DIR, uploaded_file.name)
    with open(file_path, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return file_path

if section == "Головна сторінка":
    st.title("LegalTransUA (Google + OpenAI)")
    st.header("Переклад документів (DOCX, PDF, URL)")

    type_of_source = st.radio("Оберіть тип джерела:", ["Файл", "URL"])
    chunk_size = 5  # фіксовано

    if type_of_source == "Файл":
        uploaded_file = st.file_uploader("Завантажте файл (DOCX або PDF):", type=["docx","pdf"])
        if uploaded_file:
            file_path = save_uploaded_file(uploaded_file)
            st.success(f"Файл '{uploaded_file.name}' успішно завантажено.")

            if st.button("Розпочати переклад"):
                paragraphs = extract_text(file_path)
                if not paragraphs:
                    st.warning("Не вдалося знайти текст у документі.")
                else:
                    st.info(f"Знайдено {len(paragraphs)} абзаців.")

                    # Google переклад (кожен абзац окремо)
                    google_progress = st.progress(0, text="Google: 0%")
                    google_trans = ["" for _ in paragraphs]

                    with ThreadPoolExecutor(max_workers=5) as executor:
                        futures_g = {executor.submit(translate_text_google, p): i for i,p in enumerate(paragraphs)}
                        done_g = 0
                        for future in as_completed(futures_g):
                            idx = futures_g[future]
                            google_trans[idx] = future.result()
                            done_g += 1
                            frac_g = done_g / len(paragraphs)
                            google_progress.progress(frac_g, text=f"Google: {int(frac_g*100)}%")

                    # OpenAI GPT (chunk=5)
                    openai_progress = st.progress(0, text="OpenAI GPT: 0%")
                    openai_trans = ["" for _ in paragraphs]

                    all_chunks = list(chunk_paragraphs(paragraphs, chunk_size=chunk_size))
                    total_chunks = len(all_chunks)
                    done_c = 0
                    for c_i, chunk in enumerate(all_chunks):
                        chunk_res = translate_chunk_openai(chunk)
                        # запишемо в openai_trans
                        start_idx = c_i*chunk_size
                        for j, val in enumerate(chunk_res):
                            openai_trans[start_idx + j] = val
                        done_c += 1
                        frac_o = done_c / total_chunks
                        openai_progress.progress(frac_o, text=f"OpenAI GPT: {int(frac_o*100)}%")

                    # Збережемо DOCX
                    doc = Document()
                    setup_document_orientation(doc)
                    add_title(doc)
                    doc.add_paragraph(f"Дата та час перекладу: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

                    create_translation_table(doc, paragraphs, google_trans, openai_trans)

                    # Формуємо ім'я файлу: "MyFile (Translated by LTUA YYYY-MM-DD).docx"
                    timestamp_str = datetime.now().strftime("%Y-%m-%d")
                    base_name = os.path.splitext(uploaded_file.name)[0]
                    base_name = sanitize_filename(base_name)
                    output_filename = f"{base_name} (Translated by LTUA {timestamp_str}).docx"

                    output_path = os.path.join(TEMP_DIR, output_filename)
                    doc.save(output_path)

                    st.success("Переклад завершено!")
                    st.download_button(
                        label="Завантажити таблицю DOCX",
                        data=open(output_path, "rb").read(),
                        file_name=output_filename,
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    )

    elif type_of_source == "URL":
        url = st.text_input("Введіть URL:")
        if url and st.button("Розпочати переклад"):
            st.info(f"Завантаження тексту з {url}...")
            try:
                paragraphs = extract_text(url)
            except Exception as e:
                st.error(f"Помилка при завантаженні URL: {e}")
                paragraphs = []

            if not paragraphs:
                st.warning("Не вдалося знайти текст на сторінці.")
            else:
                st.success(f"Знайдено {len(paragraphs)} абзаців.")

                # Google
                google_progress = st.progress(0, text="Google: 0%")
                google_trans = ["" for _ in paragraphs]

                with ThreadPoolExecutor(max_workers=5) as executor:
                    futures_g = {executor.submit(translate_text_google, p): i for i,p in enumerate(paragraphs)}
                    done_g = 0
                    for future in as_completed(futures_g):
                        idx = futures_g[future]
                        google_trans[idx] = future.result()
                        done_g += 1
                        frac_g = done_g / len(paragraphs)
                        google_progress.progress(frac_g, text=f"Google: {int(frac_g*100)}%")

                # OpenAI GPT (chunk=5)
                openai_progress = st.progress(0, text="OpenAI GPT: 0%")
                openai_trans = ["" for _ in paragraphs]

                all_chunks = list(chunk_paragraphs(paragraphs, chunk_size=chunk_size))
                total_chunks = len(all_chunks)
                done_c = 0
                for c_i, chunk in enumerate(all_chunks):
                    chunk_res = translate_chunk_openai(chunk)
                    start_idx = c_i*chunk_size
                    for j, val in enumerate(chunk_res):
                        openai_trans[start_idx + j] = val
                    done_c += 1
                    frac_o = done_c / total_chunks
                    openai_progress.progress(frac_o, text=f"OpenAI GPT: {int(frac_o*100)}%")

                # Формуємо DOCX
                doc = Document()
                setup_document_orientation(doc)
                add_title(doc)
                doc.add_paragraph(f"Дата та час перекладу: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                create_translation_table(doc, paragraphs, google_trans, openai_trans)

                # "Document From Internet (Translated by LTUA YYYY-MM-DD).docx"
                timestamp_str = datetime.now().strftime("%Y-%m-%d")
                output_filename = f"Document From Internet (Translated by LTUA {timestamp_str}).docx"
                output_path = os.path.join(TEMP_DIR, output_filename)
                doc.save(output_path)

                st.success("Переклад завершено!")
                st.download_button(
                    label="Завантажити таблицю DOCX",
                    data=open(output_path, "rb").read(),
                    file_name=output_filename,
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )

elif section == "Про додаток":
    st.title("Про LegalTransUA")
    st.write("""
    **LegalTransUA** — інноваційний застосунок для перекладу юридичних документів.
    Підтримує переклад із англійської на українську, роботу з DOCX, PDF та URL,
    а також Google Translate і OpenAI GPT (chunk=5).
    """)

elif section == "Корисні посилання":
    st.title("Корисні посилання")
    st.markdown("""
    - [Європейське законодавство](https://eur-lex.europa.eu/)
    - [Законодавство України](https://zakon.rada.gov.ua/)
    - [Переклади документів ЄС](https://euractiv.com/)
    """)

elif section == "Допомога Україні":
    st.title("Допомога Україні")
    st.write("""
    Ви можете підтримати Україну, зробивши внесок у фонд [Повернись живим](https://savelife.in.ua/).
    """)

elif section == "Контакти":
    st.title("Контакти")
    st.write("""
    Якщо у вас є питання чи пропозиції, зв'яжіться зі мною:
    - **Email:** yevdokymenkodn@gmail.com
    - **Телефон:** +380 66 556 0001
    - **LinkedIn:** [Профіль](https://www.linkedin.com/in/yevdokymenko/)
    """)
