# data/

## corpus.json

- 출처: 영어 위키백과(English Wikipedia) 문서 본문, MediaWiki API의 평문 추출(TextExtracts). 2026-11-09 수집.
- 라이선스: 위키백과 본문은 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)을 따른다. 문서마다 원문 주소(`url`)를 함께 저장했고, 이 파일을 다시 배포할 때도 같은 라이선스를 따른다. 본문 끝의 "See also / References / External links" 절은 지웠다.
- 만든 방법: `scripts/collect_wiki.py` (시드 31건 → 링크 1단계 확장 → 첫 문단으로 주제 거르기).

형식

```json
{
  "meta":  { "n_docs": 148, "total_chars": 2430094, "total_tokens_o200k": 480551,
             "context_window_tokens": 400000, "exceeds_window": true, "zero_inlink_docs": ["d029"], ... },
  "docs":  [ { "id": "d001", "title": "Apostille Convention", "url": "...", "source": "seed|hop1",
               "relevance": "core|peripheral", "chars": 12379, "tokens": 2404,
               "out_links": ["d002", ...], "text": "..." } ],
  "links": [ { "src": "d001", "dst": "d002" } ]
}
```

- `links`는 코퍼스 안 문서끼리의 링크만 남긴 것이다(위키백과 본문 링크 기준, 방향 있음).
- `relevance`는 수집 뒤에 붙인 표시다. 앞 3000자에 인증 관련 낱말(notar, apostil, legaliz, certif, consul, registr, sworn …)이 2종류 이상이면 `core`, 시드는 모두 `core`. 정답표가 아니라 "주변 문서를 얼마나 읽었나"를 볼 때만 쓴다.

## questions.json

질문 10건. 정답표는 없고, 질문마다 성격(`single` 한 건으로 답이 나옴 / `scattered` 여러 갈래에 흩어짐 / `follow` 한 대상을 따라감)과 "왜 나눌 만한가" 한 줄을 둔다.
