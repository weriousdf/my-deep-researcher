# my-deep-researcher

컨텍스트 윈도우 하나에 다 들어가지 않는 자료를 코디네이터가 구역으로 나눠 맡기고, 리서처 에이전트들이 각자 읽은 자료만 들고 자기 절을 써서, 이를 이어 붙여 장문 보고서를 만드는 딥리서처 에이전트다(과정 실습 프로젝트).

- 주제: 외국 제출 서류의 인증 — 아포스티유, 영사확인, 공증, 번역 인증, 나라별 신분·학력·범죄경력 서류
- 코퍼스: 영어 위키백과 문서(CC BY-SA 4.0). 한국어 위키백과는 이 주제 문서가 적고 짧아(예: "아포스티유"는 넘겨주기 문서뿐) 영어판을 썼다.

> 작성 중이다. 실행 방법·화면 캡처·결과는 구현이 끝나면 채운다.

## 폴더

```
my-deep-researcher/
├── data/            corpus.json (docs + links) · questions.json (질문과 "왜 나눌 만한가")
├── config.json      절 수 · 절 예산 · 바퀴 상한 · 역할 명단 · 스위치
├── scripts/         collect_wiki.py (위키백과 코퍼스 수집)
├── graph.py         기획 → 배치 → 서브에이전트 → 점검 → 종합 (LangGraph)
├── metrics.py       정답표를 쓰지 않는 지표
├── ablation.py      스위치를 하나씩 끄고 평가
├── baseline.py      단일 에이전트 대조군 (같은 읽기 예산)
├── app.py           웹 데모
├── output/          runs.jsonl · ablation.json · reports/
└── REPORT.md
```

## 코퍼스 다시 모으기

```powershell
python -X utf8 -I scripts/collect_wiki.py
```

위키백과 API를 3초 간격으로 한 번에 하나씩 부르고, 429·403 응답이 오면 멈춘다. 받은 응답은 `data/_cache/`(저장소에 올리지 않음)에 남아, 다시 돌리면 이어서 받는다.
