import asyncio
import html
import json
import logging
import os
import random
import re
import tempfile
import threading
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("quizbot")

# ⚠️ الأفضل وضع التوكن في متغير بيئة
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8861598781:AAF11O1gRsIq8itEvJ8gw1wpC7ivBg_q5Vg")
DATA_FILE = Path("bank.json")

# ───────────────────────── النصوص ─────────────────────────
T = {
    "ar": {
        "q_n": "سؤال {i}/{n}",
        "choose": "اختر الإجابة الصحيحة 👇",
        "correct": "✅ إجابة صحيحة!",
        "wrong": "❌ إجابة خاطئة.",
        "next": "التالي ➡️",
        "fill": "أكمل الفراغ:",
        "done": "🏁 انتهى الاختبار!\nنتيجتك: <b>{s}/{n}</b>",
        "retry": "🔁 إعادة الاختبار",
    },
    "en": {
        "q_n": "Question {i}/{n}",
        "choose": "Choose the correct answer 👇",
        "correct": "✅ Correct!",
        "wrong": "❌ Wrong.",
        "next": "Next ➡️",
        "fill": "Fill in the blank:",
        "done": "🏁 Quiz finished!\nYour score: <b>{s}/{n}</b>",
        "retry": "🔁 Retry quiz",
    },
}
LETTERS = {"ar": ["أ", "ب", "ج"], "en": ["A", "B", "C"]}
FILLERS = {
    "ar": ["لا شيء مما سبق", "جميع ما سبق"],
    "en": ["None of the above", "All of the above"],
}

# ───────────────────────── التخزين ─────────────────────────
_lock = threading.Lock()


def load_bank() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.exception("failed to load bank")
    return {}


BANK: dict = load_bank()


def save_bank():
    with _lock:
        DATA_FILE.write_text(json.dumps(BANK, ensure_ascii=False, indent=1), encoding="utf-8")


def user_bank(uid: int) -> list:
    return BANK.setdefault(str(uid), [])


def add_item(uid: int, q: str, a: str, wrong=None, src="ocr") -> bool:
    bank = user_bank(uid)
    q, a = q.strip(), a.strip()
    if not q or not a or any(x["q"] == q for x in bank):
        return False
    nid = max([x["id"] for x in bank], default=0) + 1
    bank.append({
        "id": nid, "q": q, "a": a,
        "wrong": [w.strip() for w in (wrong or []) if w and w.strip()],
        "lang": detect_lang(q + " " + a), "src": src,
    })
    return True


# ───────────────────────── أدوات نصية ─────────────────────────
def detect_lang(text: str) -> str:
    ar = len(re.findall(r"[\u0600-\u06FF]", text))
    en = len(re.findall(r"[A-Za-z]", text))
    return "ar" if ar > en else "en"


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def clip(s: str, n: int = 600) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ───────────────────────── OCR ─────────────────────────
_reader = None
_reader_lock = threading.Lock()


def get_reader():
    global _reader
    with _reader_lock:
        if _reader is None:
            import easyocr
            log.info("Loading EasyOCR (ar+en)... first run downloads models")
            _reader = easyocr.Reader(["ar", "en"], gpu=False)
        return _reader


def ocr_lines(path: str) -> list:
    res = get_reader().readtext(path, detail=1, paragraph=False)
    boxes = []
    for bbox, text, conf in res:
        if conf < 0.2 or not text.strip():
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        boxes.append({"t": text.strip(), "x0": min(xs), "yc": sum(ys) / 4, "h": max(ys) - min(ys)})
    boxes.sort(key=lambda b: b["yc"])

    lines: list[dict] = []
    for b in boxes:
        if lines and abs(b["yc"] - lines[-1]["yc"]) < 0.6 * max(b["h"], lines[-1]["h"]):
            ln = lines[-1]
            ln["items"].append(b)
            ln["yc"] = sum(i["yc"] for i in ln["items"]) / len(ln["items"])
            ln["h"] = max(ln["h"], b["h"])
        else:
            lines.append({"yc": b["yc"], "h": b["h"], "items": [b]})

    out = []
    for ln in lines:
        its = ln["items"]
        rtl = detect_lang(" ".join(i["t"] for i in its)) == "ar"
        its.sort(key=lambda i: i["x0"], reverse=rtl)
        out.append(" ".join(i["t"] for i in its))
    return out


# ───────────────────────── استخراج الأسئلة ─────────────────────────
Q_PREFIX = re.compile(r"^\s*(?:(?:س|q(?:uestion)?)\s*\d*\s*[:\-\.\)]|\d+\s*[\.\-\)]\s*(?=.*[?؟]))\s*", re.I)
A_PREFIX = re.compile(r"^\s*(?:ج\s*\d*\s*[:\-\.\)]|(?:ال)?[إا]جابة\s*[:\-]|a(?:nswer|ns)?\s*\d*\s*[:\-\.\)])\s*", re.I)
Q_MARK = re.compile(r"[?؟]")


def is_question(line: str) -> bool:
    l = line.strip().rstrip(".:… ")
    if Q_MARK.search(l[-2:]):
        return True
    return bool(re.match(r"^\s*(?:س|q(?:uestion)?)\s*\d*\s*[:\-\.\)]", line, re.I))


def parse_qa(lines: list[str]) -> list:
    items, cur_q, cur_a = [], None, []

    def flush():
        if cur_q and cur_a:
            items.append((cur_q, " ".join(cur_a).strip()))

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if A_PREFIX.match(line) and cur_q:
            cur_a.append(A_PREFIX.sub("", line))
            continue
        if is_question(line):
            flush()
            q = Q_PREFIX.sub("", line).strip()
            cur_a = []  # ✅ تصفير إجابة السؤال السابق دائماً
            m = re.match(r"^(.*?[?؟])\s+(\S.{1,})$", q)
            if m:
                items.append((m.group(1).strip(), m.group(2).strip()))
                cur_q = None
            else:
                cur_q = q
        elif cur_q:
            cur_a.append(line)
    flush()
    return items


def make_cloze(lines: list[str]) -> list:
    text = " ".join(lines)
    if len(re.findall(r"[\.!?؟]", text)) >= 2:
        sentences = re.split(r"(?<=[\.\!\?؟])\s+", text)
    else:
        sentences = lines
    out = []
    for s in sentences:
        s = s.strip()
        words = re.findall(r"[\u0600-\u06FFA-Za-z0-9]+", s)
        if len(words) < 5:
            continue
        cands = [w for w in words if len(w) >= 4]
        if not cands:
            continue
        target = max(cands, key=len)
        blanked = re.sub(re.escape(target), "_____", s, count=1)
        lang = detect_lang(s)
        out.append((f"{T[lang]['fill']} {blanked}", target))
    return out


# ───────────────────────── توليد الخيارات ─────────────────────────
def build_options(item: dict, bank: list):
    lang, correct = item["lang"], item["a"]
    wrong = [w for w in item.get("wrong", []) if w and w != correct][:2]
    if len(wrong) < 2:
        pool = list({x["a"] for x in bank if x["lang"] == lang and x["a"] != correct and x["a"] not in wrong})
        random.shuffle(pool)
        pool.sort(key=lambda p: abs(len(p) - len(correct)))
        cands = pool[:6]
        random.shuffle(cands)
        for p in cands:
            if len(wrong) >= 2:
                break
            wrong.append(p)
    for f in FILLERS[lang]:
        if len(wrong) >= 2:
            break
        if f not in wrong and f != correct:
            wrong.append(f)
    opts = [correct] + wrong[:2]
    random.shuffle(opts)
    return opts, opts.index(correct)


def render_question(item, opts, i, n, chosen=None, correct_idx=None) -> str:
    lang = item["lang"]
    head = f"<b>{T[lang]['q_n'].format(i=i, n=n)}</b>\n\n{esc(clip(item['q']))}\n\n"
    lines = []
    for k, o in enumerate(opts):
        mark = ""
        if chosen is not None:
            if k == correct_idx:
                mark = " ✅"
            elif k == chosen:
                mark = " ❌"
        lines.append(f"<b>{LETTERS[lang][k]})</b> {esc(clip(o, 250))}{mark}")
    foot = f"\n\n{T[lang]['choose']}" if chosen is None else ""
    return head + "\n".join(lines) + foot


# ───────────────────────── الواجهة ─────────────────────────
def menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 اسألني / Quiz me", callback_data="start_quiz")]
    ])


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 أهلاً بك في بوت الاختبارات التفاعلية!\n\n"
        "📸 قم بإرسال أي صورة تحتوي على أسئلة أو نصوص، وسأقوم بتحليلها آلياً وتوليد اختبار تفاعلي لك بالكامل.",
        reply_markup=menu_kb()
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري قراءة الصورة وتدقيق السطور، انتظر قليلاً...")

    photo_file = await update.message.photo[-1].get_file()
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        temp_name = f.name

    await photo_file.download_to_drive(temp_name)

    try:
        lines = ocr_lines(temp_name)
        qa_pairs = parse_qa(lines)

        if not qa_pairs:
            qa_pairs = make_cloze(lines)

        if not qa_pairs:
            await update.message.reply_text(
                "❌ عذراً، لم أستطع العثور على سياق نصي كافٍ لتوليد اختبار من هذه الصورة."
            )
            return

        uid = update.effective_user.id
        added_count = 0
        for q, a in qa_pairs:
            if add_item(uid, q, a, src="ocr"):
                added_count += 1

        save_bank()
        lang = detect_lang(" ".join(lines))

        if lang == "ar":
            msg = f"✅ تم فحص الصورة بنجاح وتوليد {added_count} من الأسئلة في بنك الأسئلة الخاص بك!"
        else:
            msg = f"✅ Image scanned successfully! Added {added_count} questions to your quiz bank."

        await update.message.reply_text(msg, reply_markup=menu_kb())

    except Exception as e:
        log.exception("Error processing user image")
        await update.message.reply_text(f"❌ حدث خطأ فني أثناء قراءة الصورة: {e}")
    finally:
        if os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except OSError:
                pass


async def quiz_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    bank = user_bank(uid)

    if not bank:
        await query.message.reply_text(
            "🗂 بنك الأسئلة لديك فارغ حالياً. يرجى إرسال صورة أولاً لاستخراج الأسئلة منها."
        )
        return

    # بدء جلسة جديدة
    if query.data in ("start_quiz", "retry_quiz"):
        context.user_data["score"] = 0
        context.user_data["current_index"] = 0
        context.user_data["quiz_items"] = random.sample(bank, min(len(bank), 5))

    # عند الضغط على "التالي" نزيد المؤشر
    elif query.data == "next_question":
        context.user_data["current_index"] = context.user_data.get("current_index", 0) + 1

    idx = context.user_data.get("current_index", 0)
    items = context.user_data.get("quiz_items", [])

    # نهاية الاختبار
    if idx >= len(items):
        score = context.user_data.get("score", 0)
        lang = items[0]["lang"] if items else "ar"
        kb = [[InlineKeyboardButton(T[lang]["retry"], callback_data="retry_quiz")]]
        await query.message.reply_text(
            T[lang]["done"].format(s=score, n=len(items)),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    current_item = items[idx]
    opts, correct_idx = build_options(current_item, bank)
    context.user_data["current_opts"] = opts
    context.user_data["correct_idx"] = correct_idx

    text = render_question(current_item, opts, idx + 1, len(items))
    lang = current_item["lang"]
    buttons = []
    for k, o in enumerate(opts):
        buttons.append([InlineKeyboardButton(f"{LETTERS[lang][k]}) {o[:50]}", callback_data=f"ans_{k}")])
    await query.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def answer_check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chosen_idx = int(query.data.split("_")[1])

    idx = context.user_data.get("current_index", 0)
    items = context.user_data.get("quiz_items", [])
    if idx >= len(items):
        return

    current_item = items[idx]
    opts = context.user_data.get("current_opts", [])
    correct_idx = context.user_data.get("correct_idx", 0)
    lang = current_item["lang"]

    if chosen_idx == correct_idx:
        context.user_data["score"] = context.user_data.get("score", 0) + 1
        feedback = T[lang]["correct"]
    else:
        feedback = T[lang]["wrong"]

    updated_text = render_question(
        current_item, opts, idx + 1, len(items),
        chosen=chosen_idx, correct_idx=correct_idx,
    )
    await query.edit_message_text(text=updated_text, parse_mode="HTML")

    next_kb = [[InlineKeyboardButton(T[lang]["next"], callback_data="next_question")]]
    await query.message.reply_text(feedback, reply_markup=InlineKeyboardMarkup(next_kb))


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN غير محدد")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(
        quiz_handler,
        pattern="^(start_quiz|retry_quiz|next_question)$",
    ))
    app.add_handler(CallbackQueryHandler(answer_check_handler, pattern=r"^ans_\d+$"))
    print("🤖 [MyTestMaker120_Bot] يعمل الآن بكفاءة ويستمع لرسائلك في تليجرام...")
    app.run_polling()


if __name__ == "__main__":
    main()