pdf_ocr_setup() {
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
