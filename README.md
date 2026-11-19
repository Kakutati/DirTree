# DirTree

Q-Dir 스타일 탭/분할 탐색기 + TreeSize 스타일 폴더 용량 트리 + Everything 스타일 파일 검색.

```
pip install PySide6                    # Python 3.12 이상
python setup.py build_ext --inplace    # 검색 인덱스 C 확장 빌드 (Visual Studio C++ 빌드 도구 필요)
python dirtree.py [시작 폴더]
python test_dirtree.py                 # 셀프 체크
```

| 기능 | 사용법 |
|---|---|
| 중심 폴더 펼치기 | 주소창 입력 / `…` / `↑`. 기본 10단계까지 펼침, `펼침 깊이`로 조절 |
| 삭제 | 탐색기와 동일: `Del` = 휴지통, `Shift+Del` = 즉시 삭제(확인 창). 우클릭 메뉴에도 있음 |
| 관리자 권한 | 툴바 `관리자 권한으로 실행` → 검색 인덱스를 MFT에서 직접 읽고(수 초), USN 저널로 1초마다 실시간 반영 |
| 탭 기억 | 종료 시 탭·분할 배치·폴더·펼침 깊이·창 크기를 저장, 다음 실행 때 복원 |
| 용량 관리 | 크기·비율 막대·파일 수, 크기순 정렬, 하단에 드라이브 사용량 |
| 탭 | `Ctrl+T` 새 탭, `Ctrl+W` 닫기, 폴더 우클릭 → 새 탭에서 열기 |
| 분할 창 | `Ctrl+1` 1창, `Ctrl+2` 좌우, `Ctrl+3` 상하, `Ctrl+4` 4창 |
| 파일 검색 | `Ctrl+F`. 공백 = AND, `*.pdf`, `\경로\일부`. 모든 고정 드라이브를 시작할 때 인덱싱 |
