@echo off
rem Builds dist\PDF_OCR.exe
python -m PyInstaller --noconfirm --onefile --windowed --name PDF_OCR ^
  --collect-data grpc --collect-submodules google.cloud.vision ^
  --copy-metadata google-cloud-vision --copy-metadata google-api-core --copy-metadata grpcio ^
  ocr_app.py
