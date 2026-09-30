import io
import json
import os
import re
import time
import difflib
import requests
import streamlit as st

from PIL import Image, ImageDraw
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader


# ============================================================
# FAN PROXY MAKER - STREAMLIT EDITION
# ============================================================

API_BASE = "https://api.lorcast.com/v0"
SETS_URL = f"{API_BASE}/sets"

CARD_WIDTH_MM = 63
CARD_HEIGHT_MM = 88
CARD_GAP_MM = 3

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".lorcana_proxy_maker")
CARD_CACHE_FILE = os.path.join(CACHE_DIR, "cards.json")
IMAGE_CACHE_DIR = os.path.join(CACHE_DIR, "images")
os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)
CACHE_MAX_AGE = 60 * 60 * 24


# ============================================================
# TEXT NORMALIZATION & PARSER
# ============================================================

def normalize_text(text):
    if not text:
        return ""
    text = str(text)
    replacements = {
        "–": "-", "—": "-", "-": "-",
        "’": "'", "‘": "'", "“": '"', "”": '"',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def card_full_name(card):
    name = card.get("name", "").strip()
    version = card.get("version", "")
    return f"{name} - {version}" if version else name


def parse_decklist(text):
    cards = []
    if not text:
        return cards

    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").replace("\u00a0", " ").replace("×", "x")

    ignored_headings = {
        "deck", "decklist", "deck list", "main deck", "characters", "character",
        "actions", "action", "items", "item", "songs", "song", "locations",
        "location", "sideboard", "side board", "ink", "cards", "cards:", "main",
        "main:", "deck:", "decklist:",
    }

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[•*▪◦\-]\s*", "", line.strip())
        if not line or normalize_text(line) in ignored_headings:
            continue

        match = re.match(r"^(\d+)\s+(.+)$", line) or re.match(r"^(\d+)\s*x\s+(.+)$", line, re.IGNORECASE)
        if match:
            qty, name = int(match.group(1)), match.group(2).strip()
            if 0 < qty <= 99 and name:
                cards.append({"quantity": qty, "name": name})
                continue

        match = re.match(r"^(.+?)\s+x\s*(\d+)$", line, re.IGNORECASE)
        if match:
            name, qty = match.group(1).strip(), int(match.group(2))
            if 0 < qty <= 99 and name:
                cards.append({"quantity": qty, "name": name})
                continue

        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2 and parts[0].isdigit():
                qty, name = int(parts[0]), parts[1]
                if 0 < qty <= 99 and name:
                    cards.append({"quantity": qty, "name": name})
                    continue
            if len(parts) >= 2 and parts[-1].isdigit():
                qty, name = int(parts[-1]), parts[0]
                if 0 < qty <= 99 and name:
                    cards.append({"quantity": qty, "name": name})
                    continue

    return cards


# ============================================================
# LORCAST DATABASE
# ============================================================

class LorcastDatabase:
    def __init__(self):
        self.cards = []

    def cache_is_valid(self):
        if not os.path.exists(CARD_CACHE_FILE):
            return False
        try:
            return (time.time() - os.path.getmtime(CARD_CACHE_FILE)) < CACHE_MAX_AGE
        except Exception:
            return False

    def load_cache(self):
        try:
            with open(CARD_CACHE_FILE, "r", encoding="utf-8") as f:
                self.cards = json.load(f)
            return bool(self.cards)
        except Exception:
            return False

    def save_cache(self):
        temp = CARD_CACHE_FILE + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(self.cards, f, ensure_ascii=False)
        os.replace(temp, CARD_CACHE_FILE)

    def download_database(self, status_container):
        status_container.info("Ophalen van set-lijst van Lorcast...")
        response = requests.get(SETS_URL, timeout=30)
        response.raise_for_status()
        sets = response.json().get("results", [])

        all_cards = []
        for i, s in enumerate(sets, 1):
            code = s.get("code")
            if not code:
                continue
            status_container.info(f"Set aan het downloaden ({i}/{len(sets)}): {s.get('name', code)}")
            url = f"{API_BASE}/sets/{requests.utils.quote(str(code), safe='')}/cards"
            try:
                r = requests.get(url, timeout=30)
                if r.ok:
                    all_cards.extend(r.json())
            except Exception:
                pass
            time.sleep(0.05)

        unique_cards = {c["id"]: c for c in all_cards if "id" in c}
        self.cards = list(unique_cards.values())
        self.save_cache()

    def load(self, status_container):
        if self.cache_is_valid() and self.load_cache():
            return
        self.download_database(status_container)

    def score(self, requested, card):
        wanted = normalize_text(requested)
        name = normalize_text(card.get("name", ""))
        version = normalize_text(card.get("version", ""))
        full = normalize_text(card_full_name(card))

        if wanted == full: return 100
        if wanted == name: return 90
        if version and wanted == version: return 80
        if wanted in full: return 75
        if full in wanted: return 70

        ratio = difflib.SequenceMatcher(None, wanted, full).ratio()
        if ratio >= 0.92: return 85
        if ratio >= 0.82: return 65
        if ratio >= 0.72: return 50

        w_tokens = set(wanted.split())
        f_tokens = set(full.split())
        if w_tokens:
            overlap = len(w_tokens & f_tokens) / len(w_tokens)
            if overlap >= 0.90: return 80
            if overlap >= 0.75: return 60

        return 0

    def find_matches(self, requested, limit=10):
        scored = [(self.score(requested, c), c) for c in self.cards if self.score(requested, c) > 0]
        scored.sort(key=lambda x: (-x[0], normalize_text(card_full_name(x[1]))))
        return scored[:limit]

    def match(self, requested):
        matches = self.find_matches(requested)
        if not matches:
            return {"selected": None, "confidence": "NOT FOUND", "matches": []}

        score, card = matches[0]
        confidence = "EXACT" if score >= 95 else "STRONG" if score >= 80 else "POSSIBLE" if score >= 60 else "NOT FOUND"
        return {"selected": card if confidence != "NOT FOUND" else None, "confidence": confidence, "matches": matches}


# ============================================================
# IMAGE DOWNLOAD & PROXY SAFETY PREPARATION
# ============================================================

def image_filename(card):
    card_id = card.get("id") or re.sub(r"[^A-Za-z0-9_-]", "_", card_full_name(card))
    return os.path.join(IMAGE_CACHE_DIR, f"{card_id}.img")

def get_image_url(card):
    try:
        return card["image_uris"]["digital"]["normal"]
    except (KeyError, TypeError):
        return None

def download_card_image(card):
    filename = image_filename(card)
    if os.path.exists(filename):
        return filename
    url = get_image_url(card)
    if not url:
        return None
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        with open(filename, "wb") as f:
            f.write(r.content)
        return filename
    except Exception:
        return None

def prepare_card_image(source_path):
    """
    Toepassen van anti-counterfeit maatregelen:
    1. Zwart-wit conversie
    2. Snijden naar kaartverhouding
    3. Diagonaal semi-transparant watermerk recht over het artwork
    4. Banners boven en onder
    """
    base_img = Image.open(source_path).convert("RGB")
    
    # 1. Omzetten naar Zwart-Wit (Grijswaarden)
    base_img = base_img.convert("L").convert("RGB")

    # 2. Bijsnijden naar Lorcana kaartverhouding (63mm x 88mm)
    target_ratio = CARD_WIDTH_MM / CARD_HEIGHT_MM
    current_ratio = base_img.width / base_img.height

    if current_ratio > target_ratio:
        new_width = int(base_img.height * target_ratio)
        left = (base_img.width - new_width) // 2
        base_img = base_img.crop((left, 0, left + new_width, base_img.height))
    else:
        new_height = int(base_img.width / target_ratio)
        top = (base_img.height - new_height) // 2
        base_img = base_img.crop((0, top, base_img.width, top + new_height))

    width, height = base_img.size

    # 3. Maken van een transparante laag over de volledige kaart
    wm_overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    
    # Maak een extra grote tegel om tekst in te tekenen en schuin te draaien
    diagonal_size = int((width**2 + height**2)**0.5)
    text_canvas = Image.new("RGBA", (diagonal_size, diagonal_size), (0, 0, 0, 0))
    tc_draw = ImageDraw.Draw(text_canvas)

    watermark_text = "PROXY - NOT FOR SALE"
    
    # Teken meerdere regels tekst schuin over het hele vlak
    line_spacing = int(diagonal_size / 6)
    for y in range(0, diagonal_size, line_spacing):
        # Rode semi-transparante tekst (R, G, B, Alpha)
        tc_draw.text((30, y), f"{watermark_text}   ---   {watermark_text}", fill=(220, 0, 0, 130))

    # Draai het tekstvlak 35 graden
    rotated_text = text_canvas.rotate(35, expand=False, resample=Image.BICUBIC)

    # Centreer en plak het gedraaide watermerk op de overlay
    crop_x = (diagonal_size - width) // 2
    crop_y = (diagonal_size - height) // 2
    wm_overlay.paste(rotated_text.crop((crop_x, crop_y, crop_x + width, crop_y + height)))

    # Voeg het watermerk samen met het artwork
    combined = Image.alpha_composite(base_img.convert("RGBA"), wm_overlay).convert("RGB")

    # 4. Zwarte Banners Boven en Onder
    final_draw = ImageDraw.Draw(combined)
    banner_label = "PROXY - NOT FOR SALE"
    banner_height = int(height * 0.05)

    # Bovenste banner
    final_draw.rectangle((0, 0, width, banner_height), fill="black")
    final_draw.text((int(width * 0.15), int(banner_height * 0.2)), banner_label, fill="white")

    # Onderste banner
    final_draw.rectangle((0, height - banner_height, width, height), fill="black")
    final_draw.text((int(width * 0.15), height - banner_height + int(banner_height * 0.2)), banner_label, fill="white")

    return combined

def build_pdf_bytes(matches, progress_bar):
    page_width, page_height = A4
    card_w = CARD_WIDTH_MM * mm
    card_h = CARD_HEIGHT_MM * mm
    gap = CARD_GAP_MM * mm

    cols = max(1, int((page_width + gap) / (card_w + gap)))
    rows = max(1, int((page_height + gap) / (card_h + gap)))
    cards_per_page = cols * rows

    total_cards = sum(item["quantity"] for item in matches)
    
    pdf_buffer = io.BytesIO()
    pdf = canvas.Canvas(pdf_buffer, pagesize=A4)
    pdf.setTitle("Lorcana Fan Proxy Cards")

    processed = 0
    position = 0

    for item in matches:
        card = item["selected"]
        source = download_card_image(card)
        if not source:
            continue

        image = prepare_card_image(source)
        img_buf = io.BytesIO()
        image.save(img_buf, format="PNG")
        img_buf.seek(0)
        img_reader = ImageReader(img_buf)

        for _ in range(item["quantity"]):
            if position >= cards_per_page:
                pdf.showPage()
                position = 0

            col = position % cols
            row = position // cols
            x = gap / 2 + col * (card_w + gap)
            y = page_height - gap / 2 - (row + 1) * card_h - row * gap

            pdf.drawImage(img_reader, x, y, width=card_w, height=card_h)
            pdf.setLineWidth(0.5)
            pdf.rect(x, y, card_w, card_h)

            position += 1
            processed += 1
            progress_bar.progress(processed / total_cards)

    pdf.save()
    pdf_buffer.seek(0)
    return pdf_buffer.getvalue()


# ============================================================
# STREAMLIT UI INTERFACE
# ============================================================

st.set_page_config(page_title="TCG Fan Proxy Generator", layout="wide")

st.title("🎴 Non-Official Fan Proxy Generator")
st.caption("Een gratis, niet-commerciële tool voor het testen van kaartspel-decks voor persoonlijk gebruik.")

# Database laden
@st.cache_resource
def get_database():
    db = LorcastDatabase()
    return db

db = get_database()

if not db.cards:
    status_box = st.empty()
    db.load(status_box)
    status_box.empty()

# Sidebar
with st.sidebar:
    st.header("Instellingen & Info")
    if st.button("Ververs Database"):
        if os.path.exists(CARD_CACHE_FILE):
            os.remove(CARD_CACHE_FILE)
        st.cache_resource.clear()
        st.rerun()

    uploaded_file = st.file_uploader("Upload .txt decklist", type=["txt"])
    
    st.divider()
    st.markdown("### ⚠️ Rechten & Fair Use")
    st.markdown(
        "Alle gegenereerde kaarten worden in **zwart-wit** afgedrukt met een **'PROXY - NOT FOR SALE'** watermerk recht over het artwork. "
        "Deze proxies zijn **niet** toegestaan op officiële toernooien en mogen **niet** worden verkocht."
    )

default_text = ""
if uploaded_file is not None:
    default_text = uploaded_file.read().decode("utf-8", errors="ignore")

decklist_text = st.text_area("Plak je decklist hier:", value=default_text, height=200, placeholder="4 Mickey Mouse - Wayward Wizard\n2 Elsa - Spirit of Winter")

if st.button("Zoek Kaarten", type="primary"):
    parsed = parse_decklist(decklist_text)
    if not parsed:
        st.warning("Geen geldige kaarten gevonden.")
    else:
        st.session_state["matches"] = []
        progress_text = st.empty()
        
        for i, item in enumerate(parsed, 1):
            progress_text.text(f"Matching ({i}/{len(parsed)}): {item['name']}")
            res = db.match(item["name"])
            st.session_state["matches"].append({
                "quantity": item["quantity"],
                "requested_name": item["name"],
                "selected": res["selected"],
                "confidence": res["confidence"],
                "matches": res["matches"]
            })
        progress_text.empty()
        st.success("Matching voltooid!")

if "matches" in st.session_state and st.session_state["matches"]:
    st.divider()
    st.subheader("Controleer & Genereer PDF")

    matches = st.session_state["matches"]
    col1, col2 = st.columns([2, 1])

    with col1:
        for idx, item in enumerate(matches):
            st.write(f"**{item['quantity']}x {item['requested_name']}** *(Match: {item['confidence']})*")
            options = [card_full_name(c) for _, c in item["matches"]]
            
            if options:
                current_idx = 0
                if item["selected"]:
                    for i, (_, c) in enumerate(item["matches"]):
                        if c.get("id") == item["selected"].get("id"):
                            current_idx = i
                            break
                
                selected_option = st.selectbox(
                    f"Selecteer versie voor {item['requested_name']}", 
                    options, 
                    index=current_idx, 
                    key=f"select_{idx}",
                    label_visibility="collapsed"
                )
                
                chosen_card = item["matches"][options.index(selected_option)][1]
                matches[idx]["selected"] = chosen_card

    with col2:
        st.subheader("PDF Genereren")
        total_cards = sum(m["quantity"] for m in matches)
        st.write(f"**Totaal aantal kaarten:** {total_cards}")
        
        if st.button("Genereer Proxy PDF", type="primary"):
            progress_bar = st.progress(0.0)
            status_text = st.empty()
            status_text.text("Bezig met verwerken...")
            
            pdf_data = build_pdf_bytes(matches, progress_bar)
            status_text.success("PDF Klaar!")
            
            st.download_button(
                label="📥 Download PDF",
                data=pdf_data,
                file_name="fan_proxies_not_for_sale.pdf",
                mime="application/pdf"
            )

# ============================================================
# JURIDISCHE FOOTER & DISCLAIMER
# ============================================================

st.divider()
st.markdown(
    """
    <style>
    .footer {
        font-size: 11px;
        color: #777;
        text-align: justify;
        line-height: 1.4;
    }
    </style>
    <div class="footer">
        <b>Disclaimer & Intellectual Property Notice:</b><br>
        Dit is een niet-commercieel, gratis fanproject. Deze tool is op geen enkele wijze gelieerd aan, gesponsord door, of goedgekeurd door Disney of Ravensburger.<br>
        Alle namen, handelsmerken, afbeeldingen en intellectuele eigendommen met betrekking tot <i>Disney Lorcana</i> zijn het exclusieve eigendom van Disney en Ravensburger.<br>
        Gegenereerde bestanden zijn uitsluitend bedoeld voor persoonlijk test- en speelgebruik (playtesting). Verkoop of commerciële verspreiding van deze materialen is strikt verboden.<br>
        <br>
        <b>Copyright/Takedown Notice:</b> If you are a copyright holder and wish to request removal of content, please contact us directly at <i>your-email@domain.com</i>.
    </div>
    """,
    unsafe_allow_html=True
)