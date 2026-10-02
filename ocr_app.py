"""
PDF OCR 프로그램 (구글 Vision)

Copyright (C) 2026 supergangy
https://github.com/supergangy/pdf-ocr-korean

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

스캔한 PDF의 각 페이지를 고해상도 이미지로 바꿔 구글 Vision으로 OCR하고,
보이지 않는 글자 레이어를 입혀 검색/드래그/복사가 되는 PDF를 만든다.
원하면 지정한 페이지에서 여러 파일로 나누고, 텍스트 파일도 저장한다.

실행:  python ocr_app.py
"""
import json
import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk

import pymupdf

VERSION = "1.0.0"
APP_TITLE = f"PDF OCR (구글 Vision) v{VERSION}"
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".pdf_ocr_app.json")

FREE_PAGES_PER_MONTH = 1000
USD_PER_1000_PAGES = 1.5

# 단어 뒤에 붙는 구분자 종류
SPACE_BREAKS = {"SPACE", "SURE_SPACE", "EOL_SURE_SPACE"}
LINE_BREAKS = {"EOL_SURE_SPACE", "LINE_BREAK"}

LINKS = {
    "console": "https://console.cloud.google.com/",
    "cloudshell": "https://shell.cloud.google.com/?show=terminal",
    "billing": "https://console.cloud.google.com/billing",
    "free": "https://cloud.google.com/free",
    "keys": "https://console.cloud.google.com/apis/credentials",
    "usage": "https://console.cloud.google.com/apis/api/vision.googleapis.com/metrics",
    "reports": "https://console.cloud.google.com/billing/reports",
    "pricing": "https://cloud.google.com/vision/pricing",
    "github": "https://github.com/supergangy/pdf-ocr-korean",
    "releases": "https://github.com/supergangy/pdf-ocr-korean/releases",
    "license": "https://www.gnu.org/licenses/agpl-3.0.html",
}

# Cloud Shell에 붙여넣는 자동 설정 스크립트.
# 함수로 감싸야 붙여넣기 도중 read가 다음 줄을 입력으로 먹지 않고, 오류 시 셸 창이 닫히지 않는다.
SETUP_SCRIPT = r'''pdf_ocr_setup() {
  echo "===== PDF OCR 구글 클라우드 자동 설정 ====="

  # 1. 프로젝트 고르기 (없으면 새로 만든다)
  PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
  if [ -z "$PROJECT_ID" ]; then
    mapfile -t PROJECTS < <(gcloud projects list --format='value(projectId)' 2>/dev/null)
    CHOICE=0
    if [ ${#PROJECTS[@]} -gt 0 ]; then
      echo "사용할 프로젝트 번호를 입력하세요:"
      for i in "${!PROJECTS[@]}"; do echo "  $((i+1))) ${PROJECTS[$i]}"; done
      echo "  0) 새 프로젝트 만들기"
      read -r -p "번호: " CHOICE
    fi
    if [ "$CHOICE" = "0" ]; then
      PROJECT_ID="pdf-ocr-$(date +%s | tail -c 7)"
      echo "[1/4] 새 프로젝트 만드는 중: $PROJECT_ID"
      gcloud projects create "$PROJECT_ID" --name="PDF OCR" || return 1
    elif [[ "$CHOICE" =~ ^[0-9]+$ ]] && [ "$CHOICE" -le ${#PROJECTS[@]} ]; then
      PROJECT_ID=${PROJECTS[$((CHOICE-1))]}
    else
      echo "[오류] 잘못된 번호입니다. 다시 실행하세요."; return 1
    fi
    gcloud config set project "$PROJECT_ID" >/dev/null 2>&1
  fi
  echo "[1/4] 프로젝트: $PROJECT_ID"

  # 2. 결제 계정 연결 (이미 연결돼 있으면 건너뜀)
  if [ "$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)')" != "True" ]; then
    BILLING=$(gcloud billing accounts list --filter=open=true --format='value(name)' --limit=1)
    if [ -z "$BILLING" ]; then
      echo "[오류] 결제 계정이 없습니다. https://console.cloud.google.com/billing 에서"
      echo "       결제 계정(카드 등록)을 먼저 만든 뒤 다시 실행하세요."
      return 1
    fi
    echo "[2/4] 결제 계정 연결 중: $BILLING"
    gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING" >/dev/null || return 1
  else
    echo "[2/4] 결제 계정 이미 연결됨"
  fi

  # 3. 필요한 API 켜기
  echo "[3/4] Vision API 켜는 중... (1분 정도 걸릴 수 있음)"
  gcloud services enable vision.googleapis.com apikeys.googleapis.com || return 1

  # 4. Vision API만 쓸 수 있게 제한된 API 키 (이미 있으면 재사용)
  KEY_ID=$(gcloud services api-keys list --filter='displayName="PDF OCR"' --format='value(name)' --limit=1)
  if [ -z "$KEY_ID" ]; then
    echo "[4/4] API 키 만드는 중..."
    gcloud services api-keys create --display-name="PDF OCR" \
      --api-target=service=vision.googleapis.com >/dev/null 2>&1 || return 1
    KEY_ID=$(gcloud services api-keys list --filter='displayName="PDF OCR"' --format='value(name)' --limit=1)
  else
    echo "[4/4] 기존 API 키 사용"
  fi
  KEY=$(gcloud services api-keys get-key-string "$KEY_ID" --format='value(keyString)')

  echo ""
  echo "===== 완료! 아래 API 키를 복사해서 프로그램에 붙여넣으세요 ====="
  echo ""
  echo "    $KEY"
  echo ""
  echo "(이 키는 다른 사람에게 알려주지 마세요)"
}; pdf_ocr_setup
'''

# 사용법 탭 내용. [[이름|글자]] 는 LINKS[이름] 으로 가는 링크가 된다.
HELP_TEXT = """\
■ 처음 한 번만: 구글 클라우드 준비 (약 5분)

1) 구글 클라우드 가입 + 결제 계정 만들기
   [[free|구글 클라우드 무료 체험 가입]] → 카드 등록까지 하면 신규 300달러 크레딧을 줍니다.
   (이미 가입했다면 건너뛰세요. 결제 계정 확인: [[billing|결제 페이지]])

2) Cloud Shell(브라우저 속 터미널) 열기
   위의 [Cloud Shell 열기] 버튼을 누르세요.
   처음이면 약관 동의 / '승인(Authorize)' 창이 뜰 수 있는데, 동의/승인을 누르면 됩니다.
   ※ 콘솔 화면에서 직접 열려면: 위쪽 파란 바 오른쪽의 '>_' 아이콘(마우스를 올리면
     'Cloud Shell 활성화'). 브라우저 창이 좁으면 ⋮ 메뉴 안에 숨어 있으니 창을 넓혀 보세요.

3) 설정 스크립트 실행
   [설정 스크립트 복사] 버튼 → Cloud Shell 화면을 클릭 → Ctrl+V (또는 마우스 오른쪽 → 붙여넣기)
   → Enter. 브라우저가 '클립보드 접근 허용'을 물으면 허용하세요.
   프로젝트 번호를 물으면 사용할 번호를 입력하고 Enter.
   스크립트가 결제 연결, Vision API 켜기, API 키 발급까지 자동으로 합니다.

4) 마지막에 나온 'AIza...'로 시작하는 키를 복사해서
   'OCR' 탭의 [API 키] 칸에 붙여넣고 [연결 테스트]를 누르세요. '연결 성공'이면 준비 끝!
   (Cloud Shell에서 복사: 마우스로 드래그하면 자동 복사, 또는 Ctrl+C)


■ OCR 하기

1) [추가]로 PDF를 고릅니다. (여러 개 가능, 차례대로 처리)
2) 필요하면 옵션을 정합니다.
   · 해상도: 300dpi 권장 (①②, ㉠㉡ 같은 작은 기호도 잘 읽음)
   · OCR 페이지 범위: 비우면 전체. 예) 1-5 (처음엔 몇 쪽만 시험해 보세요), 1-10, 15
   · 분할 시작 페이지: 예) 327 → 1~326쪽 / 327쪽~끝 두 파일로 나눔. 여러 개는 쉼표.
   · 이미 글자가 있는 페이지 건너뛰기: 원래 글자가 들어 있는 페이지는 OCR하지 않음 (비용 절약)
   · 텍스트 파일도 저장: 인식한 글자를 .txt 로도 저장
3) [OCR 시작] → 페이지 수와 예상 요금을 확인하고 [예]
4) 결과는 원본 PDF와 같은 폴더에 저장됩니다.
   · 원본이름(OCR).pdf          검색/드래그/복사가 되는 PDF
   · 원본이름(OCR)_1-326쪽.pdf   분할한 파일 (분할 시)
   · 원본이름(OCR).txt          텍스트 파일 (선택 시)
   · 원본이름_ocr데이터_300dpi  OCR 결과 보관 폴더

※ 중간에 [중지]하거나 오류가 나도, 다시 [OCR 시작]하면 이미 한 페이지는 건너뛰고 이어서 합니다.
   (같은 페이지에 두 번 요금이 나가지 않음. 다 끝나고 필요 없으면 보관 폴더는 지워도 됩니다)
※ 이미 OCR된 PDF를 나누기만 하려면: 파일 추가 → 분할 시작 페이지 입력 → [분할만 하기]


■ 요금

· 매달 1,000페이지까지 무료, 넘으면 1,000페이지당 약 1.5달러 ([[pricing|요금 안내]])
· 신규 가입 300달러 크레딧이 있으면 거기서 먼저 빠집니다. (크레딧은 가입 후 90일간 유효)
· 사용량 확인: [[usage|Vision API 사용량]]  /  청구 금액 확인: [[reports|결제 보고서]] (하루 정도 늦게 반영)


■ 자주 생기는 문제

· 'API 키가 올바르지 않음' → 키를 다시 복사해 붙여넣으세요. 앞뒤 공백이 섞이지 않게.
· 'Vision API가 꺼져 있음' → 설정 스크립트를 다시 실행하거나, 켠 직후라면 몇 분 뒤 다시 시도.
· '결제가 연결되지 않음' → [[billing|결제 페이지]]에서 프로젝트에 결제 계정을 연결하세요.
· 'json 인증키를 만들 수 없다(조직 정책)' → json 대신 API 키를 쓰면 이 문제가 없습니다.
· API 키 관리/삭제: [[keys|사용자 인증 정보 페이지]]
· 처음 실행할 때 윈도우 'PC 보호' 경고 → [추가 정보] → [실행]
· 그 밖의 문제나 건의: [[github|GitHub 페이지]]의 Issues에 남겨 주세요.


■ 프로그램 정보

· PDF OCR v{version}   Copyright (C) 2026 supergangy
· 소스 코드: [[github|github.com/supergangy/pdf-ocr-korean]]   새 버전: [[releases|Releases]]
· 라이선스: [[license|GNU AGPL v3]] — 자유롭게 사용·수정·재배포할 수 있으며, 아무런 보증이 없습니다.
· 이 프로그램은 구글의 공식 프로그램이 아니며, 구글 Cloud Vision API를 이용합니다.
· 사용한 오픈소스: PyMuPDF (AGPL), Google Cloud 클라이언트 라이브러리 (Apache 2.0) 외
""".replace("{version}", VERSION)


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- 구글 연결

def make_client(credential):
    """credential: 서비스 계정 json 파일 경로 또는 API 키 문자열"""
    from google.cloud import vision
    from google.oauth2 import service_account

    if os.path.isfile(credential):
        credentials = service_account.Credentials.from_service_account_file(credential)
        return vision.ImageAnnotatorClient(credentials=credentials)
    return vision.ImageAnnotatorClient(client_options={"api_key": credential})


def friendly_error(error):
    """구글 오류 메시지를 이해하기 쉬운 한글 설명으로 바꾼다."""
    message = str(error)
    hints = [
        ("API_KEY_INVALID", "API 키가 올바르지 않습니다. 키를 다시 복사해 붙여넣으세요."),
        ("API key not valid", "API 키가 올바르지 않습니다. 키를 다시 복사해 붙여넣으세요."),
        ("SERVICE_DISABLED", "이 프로젝트에서 Vision API가 꺼져 있습니다. 설정 스크립트를 다시 실행하거나, "
                             "방금 켰다면 몇 분 뒤 다시 시도하세요."),
        ("has not been used", "이 프로젝트에서 Vision API가 꺼져 있습니다. 설정 스크립트를 다시 실행하거나, "
                              "방금 켰다면 몇 분 뒤 다시 시도하세요."),
        ("BILLING_DISABLED", "프로젝트에 결제 계정이 연결되지 않았습니다. 사용법 탭의 '결제 페이지'에서 연결하세요."),
        ("billing", "프로젝트에 결제 계정이 연결되지 않았습니다. 사용법 탭의 '결제 페이지'에서 연결하세요."),
        ("API_KEY_SERVICE_BLOCKED", "이 API 키는 Vision API를 쓸 수 없도록 제한돼 있습니다. "
                                    "키 제한에 Cloud Vision API를 추가하세요."),
        ("PERMISSION_DENIED", "권한이 없습니다. 인증키가 이 프로젝트용인지 확인하세요."),
        ("RESOURCE_EXHAUSTED", "사용 한도를 넘었습니다. 잠시 후 다시 시도하세요."),
        ("Failed to resolve", "인터넷에 연결되어 있지 않습니다."),
        ("UNAVAILABLE", "구글 서버에 연결할 수 없습니다. 인터넷 연결을 확인하고 다시 시도하세요."),
    ]
    for keyword, hint in hints:
        if keyword.lower() in message.lower():
            return f"{hint}\n\n(원본 메시지: {message[:300]})"
    return message[:600]


def test_connection(credential):
    """작은 빈 이미지를 보내서 인증/API/결제가 정상인지 확인 (1페이지 사용량)."""
    from google.cloud import vision

    blank = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
    blank.clear_with(255)
    response = make_client(credential).document_text_detection(
        image=vision.Image(content=blank.tobytes("png")))
    if response.error.message:
        raise RuntimeError(response.error.message)


# ---------------------------------------------------------------- OCR

def cache_dir_for(pdf_path, dpi):
    return f"{os.path.splitext(pdf_path)[0]}_ocr데이터_{dpi}dpi"


def cache_path(cache_dir, page_number):
    return os.path.join(cache_dir, f"page-{page_number:03d}.json")


def parse_page_ranges(text, page_count):
    """'1-5, 10' → [1, 2, 3, 4, 5, 10]. 비어 있으면 전체."""
    text = text.replace(" ", "")
    if not text:
        return list(range(1, page_count + 1))
    pages = set()
    for part in text.split(","):
        if not part:
            continue
        first, _, last = part.partition("-")
        try:
            first, last = int(first), int(last or first)
        except ValueError:
            raise ValueError(f"'{part}'는 올바른 페이지 범위가 아닙니다. 예) 1-5, 10")
        if not 1 <= first <= last <= page_count:
            raise ValueError(f"페이지 범위 '{part}'가 1~{page_count}쪽을 벗어납니다.")
        pages.update(range(first, last + 1))
    return sorted(pages)


def plan_ocr(pdf_path, dpi, range_text, skip_text_pages):
    """실제로 OCR할 페이지 목록과 건너뛰는 이유별 개수를 계산한다."""
    doc = pymupdf.open(pdf_path)
    cache_dir = cache_dir_for(pdf_path, dpi)
    todo, cached, has_text = [], 0, 0
    for n in parse_page_ranges(range_text, doc.page_count):
        if os.path.exists(cache_path(cache_dir, n)):
            cached += 1
        elif skip_text_pages and len(doc[n - 1].get_text().strip()) > 30:
            has_text += 1
        else:
            todo.append(n)
    return {"todo": todo, "cached": cached, "has_text": has_text, "page_count": doc.page_count}


def run_ocr(pdf_path, client, cache_dir, dpi, todo, progress, stop):
    """todo 페이지들을 OCR해서 cache_dir/page-XXX.json 으로 저장한다."""
    from google.cloud import vision

    def ocr_page(page_number, png_bytes):
        # 구글 서버가 잠깐 응답을 안 할 때가 있어서 몇 번 재시도한다
        for attempt in range(5):
            try:
                response = client.document_text_detection(
                    image=vision.Image(content=png_bytes),
                    image_context=vision.ImageContext(language_hints=["ko"]),
                )
            except Exception as e:
                if attempt == 4 or "UNAVAILABLE" not in str(e).upper():
                    raise
                time.sleep(2 ** attempt)
                continue
            if not response.error.message:
                break
            time.sleep(2 ** attempt)
        else:
            raise RuntimeError(f"{page_number}페이지 OCR 실패: {response.error.message}")
        data = json.loads(vision.AnnotateImageResponse.to_json(
            response, preserving_proto_field_name=False, use_integers_for_enums=False))
        with open(cache_path(cache_dir, page_number), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    os.makedirs(cache_dir, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    progress(0, len(todo), "OCR")
    # 이미지를 한꺼번에 만들면 메모리를 많이 먹으므로 16장씩 나눠서 처리
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i in range(0, len(todo), 16):
            if stop.is_set():
                raise Cancelled()
            chunk = todo[i:i + 16]
            futures = [pool.submit(ocr_page, n, doc[n - 1].get_pixmap(dpi=dpi).tobytes("png"))
                       for n in chunk]
            for future in futures:
                future.result()
            progress(i + len(chunk), len(todo), "OCR")


def load_annotation(cache_dir, page_number):
    path = cache_path(cache_dir, page_number)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("fullTextAnnotation", {})


# ---------------------------------------------------------------- 글자 레이어

def box_to_rect(bounding_box, sx, sy):
    verts = bounding_box.get("vertices", [])
    xs = [v.get("x", 0) * sx for v in verts]
    ys = [v.get("y", 0) * sy for v in verts]
    return pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))


def extract_lines(annotation, page_w, page_h):
    """Vision의 block > paragraph > word > symbol 구조를 '줄' 단위로 묶는다."""
    lines = []
    for page in annotation.get("pages", []):
        # 이미지 픽셀 좌표 → PDF 좌표 배율
        sx, sy = page_w / page.get("width", page_w), page_h / page.get("height", page_h)
        for block in page.get("blocks", []):
            for paragraph in block.get("paragraphs", []):
                text, rect = "", None
                for word in paragraph.get("words", []):
                    brk = None
                    for symbol in word.get("symbols", []):
                        text += symbol.get("text", "")
                        brk = symbol.get("property", {}).get("detectedBreak", {}).get("type")
                        if brk in SPACE_BREAKS:
                            text += " "
                    word_rect = box_to_rect(word["boundingBox"], sx, sy)
                    rect = word_rect if rect is None else rect | word_rect
                    if brk in LINE_BREAKS:
                        lines.append((text.strip(), rect))
                        text, rect = "", None
                if text.strip() and rect is not None:
                    lines.append((text.strip(), rect))
    return lines


def build_searchable_pdf(pdf_path, cache_dir, output_path, log, progress, stop):
    doc = pymupdf.open(pdf_path)
    font = pymupdf.Font("korea")  # PyMuPDF 내장 한글 폰트

    for page in doc:
        if stop.is_set():
            raise Cancelled()
        annotation = load_annotation(cache_dir, page.number + 1)
        if not annotation:
            continue
        page.insert_font(fontname="kr", fontbuffer=font.buffer)

        for text, rect in extract_lines(annotation, page.rect.width, page.rect.height):
            if not text or rect.is_empty or rect.width < 1:
                continue
            fontsize = rect.height * 0.85
            natural_width = font.text_length(text, fontsize=fontsize)
            if natural_width <= 0:
                continue
            # 글자 폭을 실제 이미지 속 줄 길이에 맞게 가로로 늘이거나 줄인다
            origin = pymupdf.Point(rect.x0, rect.y1 - rect.height * 0.15)
            page.insert_text(
                origin, text, fontname="kr", fontsize=fontsize,
                render_mode=3,  # 보이지 않는 글자 (검색/드래그만 됨)
                morph=(origin, pymupdf.Matrix(rect.width / natural_width, 1)),
            )
        progress(page.number + 1, doc.page_count, "PDF 만들기")

    log("PDF 저장 중... (용량이 크면 시간이 걸립니다)")
    doc.subset_fonts()
    doc.save(output_path, garbage=3, deflate=True)
    log(f"저장: {os.path.basename(output_path)}")


def save_text(pdf_path, cache_dir, txt_path, log):
    """OCR 결과를 페이지 구분선과 함께 텍스트 파일로 저장한다. 원래 글자가 있던 페이지는 그 글자를 쓴다."""
    doc = pymupdf.open(pdf_path)
    with open(txt_path, "w", encoding="utf-8") as f:
        for page in doc:
            annotation = load_annotation(cache_dir, page.number + 1)
            text = annotation.get("text", "") if annotation else page.get_text()
            f.write(f"===== {page.number + 1}쪽 =====\n{text.strip()}\n\n")
    log(f"저장: {os.path.basename(txt_path)}")


# ---------------------------------------------------------------- 분할

def parse_split_pages(text, page_count):
    """'327' 또는 '100, 327' → [(1, 99), (100, 326), (327, 끝)]"""
    try:
        starts = sorted({int(s) for s in text.replace(" ", "").split(",") if s})
    except ValueError:
        raise ValueError("분할 시작 페이지는 숫자로 입력하세요. 예) 327 또는 100, 327")
    for s in starts:
        if not 2 <= s <= page_count:
            raise ValueError(f"분할 페이지 {s}는 2~{page_count} 사이여야 합니다.")
    bounds = [1] + starts + [page_count + 1]
    return [(bounds[i], bounds[i + 1] - 1) for i in range(len(bounds) - 1)]


def split_pdf(pdf_path, split_text, log):
    src = pymupdf.open(pdf_path)
    base = os.path.splitext(pdf_path)[0]
    for first, last in parse_split_pages(split_text, src.page_count):
        out = pymupdf.open()
        out.insert_pdf(src, from_page=first - 1, to_page=last - 1)
        out_path = f"{base}_{first}-{last}쪽.pdf"
        out.save(out_path, garbage=3, deflate=True)
        log(f"분할 저장: {os.path.basename(out_path)} ({last - first + 1}쪽)")


# ---------------------------------------------------------------- 화면

class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.last_output_dir = None
        self.stage_started = None

        root.title(APP_TITLE)
        root.minsize(760, 600)

        settings = self.load_settings()
        self.cred_var = tk.StringVar(value=settings.get("credentials", ""))
        self.dpi_var = tk.StringVar(value=settings.get("dpi", "300"))
        self.range_var = tk.StringVar()
        self.split_var = tk.StringVar()
        self.skip_text_var = tk.BooleanVar(value=settings.get("skip_text", True))
        self.save_txt_var = tk.BooleanVar(value=settings.get("save_txt", False))
        self.show_key_var = tk.BooleanVar(value=False)

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)
        ocr_tab = ttk.Frame(notebook, padding=12)
        help_tab = ttk.Frame(notebook, padding=12)
        notebook.add(ocr_tab, text="  OCR  ")
        notebook.add(help_tab, text="  처음 설정 / 사용법  ")
        self.build_ocr_tab(ocr_tab)
        self.build_help_tab(help_tab)

        # 인증 정보가 없으면 사용법 탭부터 보여준다
        if not self.cred_var.get():
            notebook.select(help_tab)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll_events)

    # ------------------------------------------------ OCR 탭
    def build_ocr_tab(self, tab):
        tab.columnconfigure(1, weight=1)

        # 인증
        ttk.Label(tab, text="API 키 / json").grid(row=0, column=0, sticky="w")
        self.cred_entry = ttk.Entry(tab, textvariable=self.cred_var, show="•")
        self.cred_entry.grid(row=0, column=1, sticky="ew", padx=6)
        auth_buttons = ttk.Frame(tab)
        auth_buttons.grid(row=0, column=2, sticky="e")
        ttk.Checkbutton(auth_buttons, text="보기", variable=self.show_key_var,
                        command=self.toggle_key).pack(side="left")
        ttk.Button(auth_buttons, text="json 찾기", command=self.pick_credentials).pack(side="left", padx=4)
        self.test_btn = ttk.Button(auth_buttons, text="연결 테스트", command=self.start_test)
        self.test_btn.pack(side="left")

        # 파일 목록
        ttk.Label(tab, text="PDF 파일").grid(row=1, column=0, sticky="nw", pady=(10, 0))
        self.file_list = tk.Listbox(tab, height=5, selectmode="extended", activestyle="none")
        self.file_list.grid(row=1, column=1, sticky="ew", padx=6, pady=(10, 0))
        file_buttons = ttk.Frame(tab)
        file_buttons.grid(row=1, column=2, sticky="n", pady=(10, 0))
        ttk.Button(file_buttons, text="추가", command=self.add_files).pack(fill="x")
        ttk.Button(file_buttons, text="선택 빼기", command=self.remove_files).pack(fill="x", pady=4)
        ttk.Button(file_buttons, text="전부 비우기",
                   command=lambda: self.file_list.delete(0, "end")).pack(fill="x")

        # 옵션
        options = ttk.LabelFrame(tab, text="옵션", padding=10)
        options.grid(row=2, column=0, columnspan=3, sticky="ew", pady=10)
        options.columnconfigure(1, weight=1)

        ttk.Label(options, text="해상도").grid(row=0, column=0, sticky="w")
        dpi_frame = ttk.Frame(options)
        dpi_frame.grid(row=0, column=1, sticky="w", padx=6)
        for value, label in (("200", "200dpi (빠름)"), ("300", "300dpi (권장, 작은 기호 인식 좋음)")):
            ttk.Radiobutton(dpi_frame, text=label, value=value,
                            variable=self.dpi_var).pack(side="left", padx=(0, 14))

        ttk.Label(options, text="OCR 페이지 범위").grid(row=1, column=0, sticky="w", pady=6)
        range_frame = ttk.Frame(options)
        range_frame.grid(row=1, column=1, sticky="w", padx=6)
        ttk.Entry(range_frame, textvariable=self.range_var, width=18).pack(side="left")
        ttk.Label(range_frame, text="비우면 전체   예) 1-5   1-10, 15",
                  foreground="gray").pack(side="left", padx=8)

        ttk.Label(options, text="분할 시작 페이지").grid(row=2, column=0, sticky="w")
        split_frame = ttk.Frame(options)
        split_frame.grid(row=2, column=1, sticky="w", padx=6)
        ttk.Entry(split_frame, textvariable=self.split_var, width=18).pack(side="left")
        ttk.Label(split_frame, text="비우면 분할 안 함   예) 327 → 1~326쪽 / 327쪽~끝",
                  foreground="gray").pack(side="left", padx=8)

        checks = ttk.Frame(options)
        checks.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(checks, text="이미 글자가 있는 페이지 건너뛰기",
                        variable=self.skip_text_var).pack(side="left")
        ttk.Checkbutton(checks, text="텍스트 파일(.txt)도 저장",
                        variable=self.save_txt_var).pack(side="left", padx=16)

        # 실행 버튼
        buttons = ttk.Frame(tab)
        buttons.grid(row=3, column=0, columnspan=3, sticky="w")
        self.run_btn = ttk.Button(buttons, text="OCR 시작", command=self.start_ocr)
        self.run_btn.pack(side="left")
        self.split_btn = ttk.Button(buttons, text="분할만 하기", command=self.start_split_only)
        self.split_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(buttons, text="중지", command=self.request_stop, state="disabled")
        self.stop_btn.pack(side="left")
        self.open_btn = ttk.Button(buttons, text="결과 폴더 열기", command=self.open_output_dir,
                                   state="disabled")
        self.open_btn.pack(side="left", padx=6)

        # 진행 상황
        self.status_var = tk.StringVar(value="대기 중")
        ttk.Label(tab, textvariable=self.status_var).grid(row=4, column=0, columnspan=3,
                                                          sticky="w", pady=(10, 0))
        self.bar = ttk.Progressbar(tab, mode="determinate")
        self.bar.grid(row=5, column=0, columnspan=3, sticky="ew", pady=4)

        log_frame = ttk.Frame(tab)
        log_frame.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(4, 0))
        tab.rowconfigure(6, weight=1)
        self.log_box = tk.Text(log_frame, height=10, state="disabled", wrap="word", font=("맑은 고딕", 9))
        scroll = ttk.Scrollbar(log_frame, command=self.log_box.yview)
        self.log_box.config(yscrollcommand=scroll.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.log("처음이라면 '처음 설정 / 사용법' 탭을 먼저 보세요.")

    # ------------------------------------------------ 사용법 탭
    def build_help_tab(self, tab):
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(0, 8))
        ttk.Button(buttons, text="Cloud Shell 열기",
                   command=lambda: webbrowser.open(LINKS["cloudshell"])).pack(side="left")
        ttk.Button(buttons, text="설정 스크립트 복사", command=self.copy_script).pack(side="left", padx=6)
        ttk.Button(buttons, text="구글 클라우드 콘솔",
                   command=lambda: webbrowser.open(LINKS["console"])).pack(side="left")
        self.copy_status = ttk.Label(buttons, text="", foreground="green")
        self.copy_status.pack(side="left", padx=10)

        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="word", padx=12, pady=8, relief="flat", font=("맑은 고딕", 10),
                       spacing1=2, spacing3=2, background=self.root.cget("background"), cursor="arrow")
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.config(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # [[이름|글자]] 를 클릭 가능한 링크로 바꿔서 넣는다
        link_count = 0
        rest = HELP_TEXT
        while "[[" in rest:
            before, _, rest = rest.partition("[[")
            inside, _, rest = rest.partition("]]")
            name, _, label = inside.partition("|")
            self.insert_help_text(text, before)
            tag = f"link{link_count}"
            link_count += 1
            text.insert("end", label, ("link", tag))
            text.tag_bind(tag, "<Button-1>", lambda e, url=LINKS[name]: webbrowser.open(url))
        self.insert_help_text(text, rest)

        text.tag_config("link", foreground="#1a73e8", underline=True)
        text.tag_config("heading", font=("맑은 고딕", 12, "bold"), spacing1=10, spacing3=6)
        text.tag_bind("link", "<Enter>", lambda e: text.config(cursor="hand2"))
        text.tag_bind("link", "<Leave>", lambda e: text.config(cursor="arrow"))
        text.config(state="disabled")

    @staticmethod
    def insert_help_text(text, chunk):
        """'■'로 시작하는 줄은 제목 스타일로 넣는다."""
        for line in chunk.splitlines(keepends=True):
            text.insert("end", line, ("heading",) if line.startswith("■") else ())

    def copy_script(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(SETUP_SCRIPT)
        self.root.update()
        self.copy_status.config(text="복사됨! Cloud Shell에 붙여넣고 Enter")

    # ------------------------------------------------ 설정 저장
    def load_settings(self):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_settings(self):
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump({
                    "credentials": self.cred_var.get().strip(),
                    "dpi": self.dpi_var.get(),
                    "skip_text": self.skip_text_var.get(),
                    "save_txt": self.save_txt_var.get(),
                }, f, ensure_ascii=False)
        except OSError:
            pass

    # ------------------------------------------------ 입력
    def toggle_key(self):
        self.cred_entry.config(show="" if self.show_key_var.get() else "•")

    def pick_credentials(self):
        path = filedialog.askopenfilename(title="구글 서비스 계정 키 선택",
                                          filetypes=[("JSON", "*.json")])
        if path:
            self.cred_var.set(path)

    def add_files(self):
        paths = filedialog.askopenfilenames(title="PDF 선택", filetypes=[("PDF", "*.pdf")])
        existing = set(self.file_list.get(0, "end"))
        for path in paths:
            path = os.path.normpath(path)
            if path in existing:
                continue
            try:
                count = pymupdf.open(path).page_count
            except Exception as e:
                self.log(f"PDF를 열 수 없습니다: {os.path.basename(path)} ({e})")
                continue
            self.file_list.insert("end", path)
            self.log(f"추가: {os.path.basename(path)} ({count}쪽)")

    def remove_files(self):
        for index in reversed(self.file_list.curselection()):
            self.file_list.delete(index)

    def get_credential(self):
        cred = self.cred_var.get().strip()
        if not (os.path.isfile(cred) or cred.startswith("AIza")):
            messagebox.showwarning("확인", "API 키(AIza...)를 붙여넣거나 'json 찾기'로 인증키 파일을 선택하세요.\n\n"
                                           "아직 없다면 '처음 설정 / 사용법' 탭을 보세요.")
            return None
        return cred

    def get_files(self):
        files = list(self.file_list.get(0, "end"))
        if not files:
            messagebox.showwarning("확인", "[추가] 버튼으로 PDF 파일을 추가하세요.")
        return files

    def check_split(self, files):
        split_text = self.split_var.get().strip()
        if not split_text:
            return True
        for path in files:
            try:
                parse_split_pages(split_text, pymupdf.open(path).page_count)
            except ValueError as e:
                messagebox.showwarning("확인", f"{os.path.basename(path)}\n분할 페이지를 확인하세요.\n\n{e}")
                return False
        return True

    # ------------------------------------------------ 실행
    def start_test(self):
        cred = self.get_credential()
        if not cred:
            return
        self.save_settings()
        self.log("연결 테스트 중...")
        self.run_in_background(self.test_job, cred)

    def test_job(self, cred):
        test_connection(cred)
        self.events.put(("info", "연결 성공! 이제 OCR을 할 수 있습니다."))

    def start_ocr(self):
        cred = self.get_credential()
        files = self.get_files() if cred else None
        if not files or not self.check_split(files):
            return
        dpi = int(self.dpi_var.get())

        plans, total_todo, notes = [], 0, []
        for path in files:
            try:
                plan = plan_ocr(path, dpi, self.range_var.get(), self.skip_text_var.get())
            except ValueError as e:
                messagebox.showwarning("확인", f"{os.path.basename(path)}\n{e}")
                return
            plans.append((path, plan))
            total_todo += len(plan["todo"])
            extra = []
            if plan["cached"]:
                extra.append(f"이전에 한 {plan['cached']}쪽 재사용")
            if plan["has_text"]:
                extra.append(f"글자 있는 {plan['has_text']}쪽 건너뜀")
            notes.append(f"· {os.path.basename(path)}: {len(plan['todo'])}쪽 OCR"
                         + (f" ({', '.join(extra)})" if extra else ""))

        paid = max(0, total_todo - FREE_PAGES_PER_MONTH)
        message = (f"OCR할 페이지: 총 {total_todo}쪽\n\n" + "\n".join(notes) +
                   f"\n\n요금: 매달 {FREE_PAGES_PER_MONTH:,}쪽까지 무료, 넘으면 1,000쪽당 약 "
                   f"{USD_PER_1000_PAGES}달러입니다.\n")
        if paid:
            message += f"이번 작업만으로 무료 한도를 넘어 약 {paid * USD_PER_1000_PAGES / 1000:.2f}달러가 나올 수 있습니다.\n"
        else:
            message += (f"이번 달 다른 사용량이 없다면 무료입니다. (이번 달에 이미 썼다면 "
                        f"최대 약 {total_todo * USD_PER_1000_PAGES / 1000:.2f}달러)\n")
        message += "\n시작할까요?"
        if not messagebox.askyesno("OCR 시작 확인", message):
            return

        self.save_settings()
        self.run_in_background(self.ocr_job, cred, plans, dpi, self.split_var.get().strip(),
                               self.save_txt_var.get())

    def ocr_job(self, cred, plans, dpi, split_text, save_txt):
        client = None
        for index, (pdf, plan) in enumerate(plans, 1):
            name = os.path.basename(pdf)
            self.log_threadsafe(f"── [{index}/{len(plans)}] {name}")
            cache_dir = cache_dir_for(pdf, dpi)
            base = os.path.splitext(pdf)[0]
            output = f"{base}(OCR).pdf"
            if plan["todo"]:
                client = client or make_client(cred)
                self.log_threadsafe(f"OCR {len(plan['todo'])}쪽 ({dpi}dpi)")
                run_ocr(pdf, client, cache_dir, dpi, plan["todo"], self.progress_threadsafe, self.stop)
            build_searchable_pdf(pdf, cache_dir, output, self.log_threadsafe,
                                 self.progress_threadsafe, self.stop)
            if save_txt:
                save_text(pdf, cache_dir, f"{base}(OCR).txt", self.log_threadsafe)
            if split_text:
                split_pdf(output, split_text, self.log_threadsafe)
            self.events.put(("output_dir", os.path.dirname(pdf)))
        self.log_threadsafe("OCR 데이터 폴더(_ocr데이터_)는 다시 만들 때 재사용됩니다. 필요 없으면 지워도 됩니다.")

    def start_split_only(self):
        files = self.get_files()
        if not files:
            return
        if not self.split_var.get().strip():
            messagebox.showwarning("확인", "분할 시작 페이지를 입력하세요.")
            return
        if self.check_split(files):
            self.run_in_background(self.split_job, files, self.split_var.get().strip())

    def split_job(self, files, split_text):
        for path in files:
            split_pdf(path, split_text, self.log_threadsafe)
            self.events.put(("output_dir", os.path.dirname(path)))

    def run_in_background(self, func, *args):
        self.stop.clear()
        self.set_running(True)

        def target():
            try:
                func(*args)
                self.events.put(("done", "완료"))
            except Cancelled:
                self.events.put(("done", "중지됨 (다시 [OCR 시작]하면 이어서 진행합니다)"))
            except Exception as e:
                self.events.put(("error", friendly_error(e)))

        threading.Thread(target=target, daemon=True).start()

    def request_stop(self):
        self.stop.set()
        self.status_var.set("중지하는 중... (진행 중인 페이지까지 마치고 멈춥니다)")

    def set_running(self, running):
        state = "disabled" if running else "normal"
        for button in (self.run_btn, self.split_btn, self.test_btn):
            button.config(state=state)
        self.stop_btn.config(state="normal" if running else "disabled")

    def open_output_dir(self):
        if self.last_output_dir and os.path.isdir(self.last_output_dir):
            os.startfile(self.last_output_dir)

    def on_close(self):
        if self.stop_btn.instate(["!disabled"]):
            if not messagebox.askyesno("종료", "작업 중입니다. 종료할까요?\n(다시 시작하면 이어서 할 수 있습니다)"):
                return
        self.save_settings()
        self.root.destroy()

    # ------------------------------------------------ 스레드 → 화면 전달
    def log_threadsafe(self, message):
        self.events.put(("log", message))

    def progress_threadsafe(self, done, total, stage):
        self.events.put(("progress", (done, total, stage)))

    def poll_events(self):
        while not self.events.empty():
            kind, value = self.events.get()
            if kind == "log":
                self.log(value)
            elif kind == "progress":
                self.show_progress(*value)
            elif kind == "output_dir":
                self.last_output_dir = value
                self.open_btn.config(state="normal")
            elif kind == "info":
                self.log(value)
                messagebox.showinfo("연결 테스트", value)
            elif kind == "done":
                self.set_running(False)
                self.status_var.set(value)
                self.log(value)
            elif kind == "error":
                self.set_running(False)
                self.status_var.set("오류")
                self.log(f"[오류] {value}")
                messagebox.showerror("오류", value)
        self.root.after(100, self.poll_events)

    def show_progress(self, done, total, stage):
        now = time.time()
        if done == 0 or self.stage_started is None or self.stage_started[0] != (stage, total):
            self.stage_started = ((stage, total), now, done)
        _, started, start_done = self.stage_started
        status = f"{stage}: {done}/{total}"
        if done > start_done and done < total:
            remaining = (now - started) / (done - start_done) * (total - done)
            status += f"   (남은 시간 약 {int(remaining // 60)}분 {int(remaining % 60)}초)"
        self.bar.config(maximum=max(total, 1), value=done)
        self.status_var.set(status)

    def log(self, message):
        self.log_box.config(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
