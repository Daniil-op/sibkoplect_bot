# -*- coding: utf-8 -*-
"""
Индексатор ПУЭ — запусти ОДИН РАЗ:  python build_pue_index.py

Читает файлы ПУЭ из папки data/pue/ (.doc или .docx), режет текст на отдельные
пункты (например «4.2.98») и сохраняет data/pue/pue_index.json. Этот индекс потом
использует бот (core/pue_kb.py), чтобы искать нужные пункты и отвечать по нормам.

.doc читать в Python неудобно, поэтому для .doc скрипт пробует сконвертировать его
в .docx через Word (нужен установленный Word + пакет pywin32). Если не получится —
открой .doc в Word и «Сохранить как» .docx, затем запусти скрипт снова.
"""
import os
import re
import sys
import glob
import json

PUE_DIR = os.path.join("data", "pue")
OUT = os.path.join(PUE_DIR, "pue_index.json")


def read_docx(path: str) -> str:
    from docx import Document  # pip install python-docx
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs)


def doc_to_docx_via_word(path: str):
    """На Windows с установленным Word: .doc -> .docx. Возвращает путь .docx или None."""
    try:
        import win32com.client as win32  # pip install pywin32
    except ImportError:
        return None
    try:
        word = win32.Dispatch("Word.Application")
        word.Visible = False
        docx_path = path + "x"  # file.doc -> file.docx
        d = word.Documents.Open(os.path.abspath(path))
        d.SaveAs(os.path.abspath(docx_path), FileFormat=16)  # 16 = .docx
        d.Close()
        word.Quit()
        return docx_path
    except Exception as e:
        print(f"   win32com не смог сконвертировать: {e}")
        return None


def read_any(path: str) -> str:
    low = path.lower()
    if low.endswith(".docx"):
        return read_docx(path)
    if low.endswith(".doc"):
        if os.path.exists(path + "x"):            # уже конвертировали ранее
            return read_docx(path + "x")
        conv = doc_to_docx_via_word(path)
        if conv:
            return read_docx(conv)
        print(f"   ⚠ Не смог прочитать {os.path.basename(path)}. "
              f"Открой в Word → «Сохранить как» → .docx и запусти скрипт снова.")
        return ""
    return ""


# пункт ПУЭ в начале строки: 1.1.1 / 4.2.98 / 7.1.13
PUNKT_RE = re.compile(r"^\s*(\d{1,2}\.\d{1,2}\.\d{1,3})\.?\s")
CHAPTER_RE = re.compile(r"^\s*(глава|раздел)\s+\S", re.IGNORECASE)


def chunk_pue(text: str) -> list[dict]:
    chunks, chapter, cur_id, buf = [], "", None, []

    def flush():
        if cur_id and buf:
            body = " ".join(x.strip() for x in buf if x.strip())
            if len(body) > 10:
                chunks.append({"id": cur_id, "chapter": chapter, "text": body})

    for line in text.splitlines():
        if CHAPTER_RE.match(line):
            chapter = line.strip()
        m = PUNKT_RE.match(line)
        if m:
            flush()
            cur_id, buf = m.group(1), [line]
        elif cur_id:
            buf.append(line)
    flush()
    return chunks


def main():
    if not os.path.isdir(PUE_DIR):
        print(f"Нет папки {PUE_DIR}. Создай её и положи туда файлы ПУЭ.")
        sys.exit(1)

    files = sorted(glob.glob(os.path.join(PUE_DIR, "*.doc"))
                   + glob.glob(os.path.join(PUE_DIR, "*.docx")))
    files = [f for f in files if not os.path.basename(f).startswith("~$")]
    if not files:
        print(f"В {PUE_DIR} нет файлов .doc/.docx.")
        sys.exit(1)

    all_chunks = []
    for f in files:
        print(f"Читаю {os.path.basename(f)} ...")
        chunks = chunk_pue(read_any(f))
        print(f"   пунктов найдено: {len(chunks)}")
        all_chunks.extend(chunks)

    # если один пункт встретился в нескольких файлах — берём самый полный
    best = {}
    for c in all_chunks:
        if c["id"] not in best or len(c["text"]) > len(best[c["id"]]["text"]):
            best[c["id"]] = c
    result = list(best.values())

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
    print(f"\nГотово: {len(result)} пунктов ПУЭ сохранено в {OUT}")
    if not result:
        print("Пунктов не найдено — вероятно, .doc не прочитался. См. сообщение выше про .docx.")


if __name__ == "__main__":
    main()