# AI 블로그 노출 분석기 v3.2

Streamlit Cloud GitHub 업로드용 소스입니다. API 키는 포함되어 있지 않습니다.

## Streamlit Cloud 배포
1. ZIP을 압축 해제한 뒤 app.py, crawler.py, requirements.txt, .gitignore, README.md를 저장소 **최상위**에 업로드합니다. ZIP 자체는 업로드하지 않습니다.
2. Streamlit Community Cloud에서 해당 저장소 / main / app.py를 선택합니다.
3. OpenAI, Serper, 네이버 검색광고 API 키는 화면에서 각 사용자가 직접 입력해야 합니다.

## 주의
- 공개 웹앱에 API 키를 자동으로 제공하려면 인증 및 사용량 제한, 보안 점검이 선행되어야 합니다.
- 이 ZIP은 **비밀키를 제거한 업로드용 소스**이며, 불특정 다수에게 공개해도 안전하다는 보안 인증은 아닙니다.
- Python 패키지 설치 및 실제 외부 API 연결은 배포 환경에서 별도 확인이 필요합니다.
