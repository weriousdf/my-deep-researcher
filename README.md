# my-deep-researcher

컨텍스트 윈도우 하나에 다 들어가지 않는 자료를 코디네이터가 내용 단위 구역으로 나눠 맡기고, 리서처 에이전트들이 각자 읽은 원문만 컨텍스트에 둔 채 자기 절을 써서, 그 원고를 이어 붙여 장문 보고서를 만드는 딥리서처 에이전트다. 과정 실습 프로젝트 "딥리서처 에이전트 만들기"의 제출물이다.

- **주제:** 외국 제출 서류의 인증. 아포스티유, 영사확인, 공증, 번역 인증, 나라별 신분·학력·범죄경력·법인 서류를 다룬다.
- **코퍼스:** 영어 위키백과 문서(CC BY-SA 4.0). 건수와 글자 수는 [data/README.md](data/README.md)에 있다. 총 토큰은 사용 모델의 컨텍스트 윈도우(1,050,000 토큰)를 넘는다.
- **모델:** gpt-5.6-luna 하나를 기획·리서처·종합과 단일 에이전트 대조군에 똑같이 쓴다. `.env`에서 바꿀 수 있다.
- 설계, 지표, 결과, 회고는 [REPORT.md](REPORT.md)에 있다.

## 실행 방법 (Windows PowerShell)

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env      # .env 를 열어 OPENAI_API_KEY 를 채운다(.env 는 저장소에 올라가지 않는다)

python -X utf8 graph.py --q q3                 # 딥리서처로 질문 하나 (output/runs.jsonl, output/reports/)
python -X utf8 baseline.py --q q3              # 같은 질문을 단일 에이전트 대조군으로
python -X utf8 ablation.py                     # 스위치를 하나씩 끄고 여러 번 → output/ablation.json
python -X utf8 metrics.py                      # 저장된 시행의 지표를 다시 계산해 표로
streamlit run app.py                           # 웹 데모 → http://localhost:8501
```

키 없이 배관만 시험하려면 `$env:DR_FAKE="1"`을 켜고 돌린다. 가짜 모델의 결과는 `output/_fake/`에만 저장된다.

코퍼스를 다시 모으려면 `python -X utf8 -I scripts/collect_wiki.py`를 돌린다. 위키백과 API를 6초 간격으로 하나씩 부르고, 429·403이 오면 멈춘다.

## 폴더

```
my-deep-researcher/
├── data/            corpus.json (docs + links) · questions.json (질문과 "왜 나눌 만한가") · README.md(출처·라이선스)
├── config.json      절 수 상한 · 총 읽기 예산 · 바퀴 상한 · 역할 명단 · 스위치
├── common.py        문단 나누기 · BM25 검색 · 예산 지키는 Reader · 인용 대조 · 모델 호출
├── graph.py         기획 → 배치(동시 파견) → 리서처 → 점검(부족 신고 절만 재파견) → 종합 (LangGraph)
├── metrics.py       정답표·판정 모델을 쓰지 않는 지표(신호/경보/스위치 증거)
├── ablation.py      스위치를 하나씩 끄고 여러 번 돌려 평균·범위
├── baseline.py      단일 에이전트 대조군(같은 자료·모델·도구·총 읽기 예산)
├── app.py           streamlit 데모
├── fake_llm.py      키 없이 배관 시험용 가짜 모델
├── scripts/         collect_wiki.py (위키백과 코퍼스 수집)
├── docs/screenshots 데모 화면 캡처
├── output/          runs.jsonl · ablation.json · reports/ · collect_log.txt
└── REPORT.md
```

## 데모 화면

(실행 뒤 캡처를 넣는다)
